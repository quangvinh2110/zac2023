#!/usr/bin/env python
# coding=utf-8

# Apache v2 license
# Copyright (C) 2022 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import copy
import logging
import math
import os
import random
import sys
from dataclasses import dataclass, field
from typing import List, Optional

import datasets
import torch
import evaluate
from functools import partial
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from datasets import load_dataset
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import transformers
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    SchedulerType,
    DataCollatorForSeq2Seq,
    get_scheduler,
    DataCollatorForLanguageModeling,
    HfArgumentParser,
)
try:
    from peft import LoraConfig, TaskType, get_peft_model
except:
    pass

from transformers.modeling_utils import unwrap_model
from transformers.trainer_utils import is_main_process

import torch
import torch.nn as nn
from transformers import pytorch_utils as pu

from optimum.habana import GaudiConfig, GaudiTrainer, GaudiTrainingArguments
from optimum.habana.utils import set_seed


try:
    from optimum.habana.utils import check_optimum_habana_min_version
except ImportError:

    def check_optimum_habana_min_version(*a, **b):
        return ()


IGNORE_INDEX = -100

os.environ["WANDB_DISABLED"] = "true"

# Will error if the minimal version of Optimum Habana is not installed. Remove at your own risks.
# check_optimum_habana_min_version("1.8.1")

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Finetune a transformers model on a causal language modeling task")
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help="The name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The configuration name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--train_file", type=str, default=None, help="A csv or a json file containing the training data."
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        required=False,
    )
    parser.add_argument(
        "--config_name",
        type=str,
        default=None,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--use_lora",
        action="store_true",
        help="If passed, will use LORA (low-rank parameter-efficient training) to train the model.",
    )
    parser.add_argument(
        "--lora_rank",
        type=int,
        default=64,
        help="The rank of lora.",
    )
    parser.add_argument(
        "--lora_alpha",
        type=float,
        default=16,
        help="The alpha parameter of lora.",
    )
    parser.add_argument(
        "--lora_dropout",
        type=float,
        default=0.1,
        help="The dropout rate of lora modules.",
    )
    parser.add_argument(
        "--save_merged_lora_model",
        action="store_true",
        help="If passed, will merge the lora modules and save the entire model.",
    )
    parser.add_argument(
        "--use_flash_attn",
        action="store_true",
        help="If passed, will use flash attention to train the model.",
    )
    parser.add_argument(
        "--gradient_checkpointing",
        action="store_true",
    )
    parser.add_argument(
        "--tokenizer_name",
        type=str,
        default=None,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",
    )
    parser.add_argument(
        "--max_seq_length",
        type=int,
        default=512,
        help="The maximum total sequence length (prompt+completion) of each training example.",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    parser.add_argument("--num_train_epochs", type=int, default=3, help="Total number of training epochs to perform.")
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--warmup_ratio", type=float, default=0, help="Ratio of total training steps used for warmup."
    )
    parser.add_argument("--output_dir", type=str, default=None, help="Where to store the final model.")
    parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument(
        "--preprocessing_num_workers",
        type=int,
        default=None,
        help="The number of processes to use for the preprocessing.",
    )
    parser.add_argument(
        "--overwrite_cache", action="store_true", help="Overwrite the cached training and evaluation sets"
    )
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default=None,
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--logging_steps",
        type=int,
        default=None,
        help="Log the training loss and learning rate every logging_steps steps.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        action="store_true",
        help="Whether to enable experiment trackers for logging.",
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="all",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`,'
            ' `"wandb"`, `"comet_ml"` and `"clearml"`. Use `"all"` (default) to report to all integrations.'
            "Only applicable when `--with_tracking` is passed."
        ),
    )
    parser.add_argument(
        "--low_cpu_mem_usage",
        action="store_true",
        help=(
            "It is an option to create the model as an empty shell, then only materialize its parameters when the pretrained weights are loaded."
            "If passed, LLM loading time and RAM consumption will be benefited."
        ),
    )
    parser.add_argument(
        "--mask_prompt",
        action="store_true",
    )
    args = parser.parse_args()

    # Sanity checks
    if args.dataset_name is None and args.train_file is None:
        raise ValueError("Need either a dataset name or a training file.")
    else:
        if args.train_file is not None:
            extension = args.train_file.split(".")[-1]
            assert extension in ["json", "jsonl"], "`train_file` should be a json/jsonl file."
    return args


def encode_with_prompt_completion_format(example, tokenizer, max_seq_length, mask_prompt=True):
    '''
    Here we assume each example has 'prompt' and 'completion' fields.
    We concatenate prompt and completion and tokenize them together because otherwise prompt will be padded/trancated 
    and it doesn't make sense to follow directly with the completion.
    '''
    # if prompt doesn't end with space and completion doesn't start with space, add space
    if not example['prompt'].endswith((' ', '\n', '\t')) and not example['completion'].startswith((' ', '\n', '\t')):
        example_text = example['prompt'] + ' ' + example['completion']
    else:
        example_text = example['prompt'] + example['completion']
    example_text = example_text + tokenizer.eos_token
    tokenized_example = tokenizer(example_text, return_tensors='pt', max_length=max_seq_length, truncation=True)
    input_ids = tokenized_example.input_ids
    labels = input_ids.clone()
    tokenized_prompt = tokenizer(example['prompt'], return_tensors='pt', max_length=max_seq_length, truncation=True)
    # mask the prompt part for avoiding loss
    if mask_prompt:
        labels[:, :tokenized_prompt.input_ids.shape[1]] = -100
    attention_mask = torch.ones_like(input_ids)
    return {
        'input_ids': input_ids.flatten(),
        'labels': labels.flatten(),
        'attention_mask': attention_mask.flatten(),
    }


def encode_with_messages_format(example, tokenizer, max_seq_length, mask_prompt=True):
    '''
    Here we assume each example has a 'messages' field Each message is a dict with 'role' and 'content' fields.
    We concatenate all messages with the roles as delimiters and tokenize them together.
    '''
    messages = example['messages']
    if len(messages) == 0:
        raise ValueError('messages field is empty.')
    
    def _concat_messages(messages):
        message_text = ""
        for message in messages:
            if message["role"] == "system":
                message_text += "<|system|>\n" + message["content"].strip() + "\n"
            elif message["role"] == "user":
                message_text += "<|user|>\n" + message["content"].strip() + "\n"
            elif message["role"] == "assistant":
                message_text += "<|assistant|>\n" + message["content"].strip() + tokenizer.eos_token + "\n"
            else:
                raise ValueError("Invalid role: {}".format(message["role"]))
        return message_text
        
    example_text = _concat_messages(messages).strip()
    tokenized_example = tokenizer(example_text, return_tensors='pt', max_length=max_seq_length, truncation=True)
    input_ids = tokenized_example.input_ids
    labels = input_ids.clone()

    # mask the non-assistant part for avoiding loss
    if mask_prompt:
        for message_idx, message in enumerate(messages):
            if message["role"] != "assistant":
                if message_idx == 0:
                    message_start_idx = 0
                else:
                    message_start_idx = tokenizer(
                        _concat_messages(messages[:message_idx]), return_tensors='pt', max_length=max_seq_length, truncation=True
                    ).input_ids.shape[1]
                if message_idx < len(messages) - 1 and messages[message_idx+1]["role"] == "assistant":
                    # if not last message and next message is assistant, we also ignore the role of the assistant
                    messages_so_far = _concat_messages(messages[:message_idx+1]) + "<|assistant|>\n"
                else:
                    messages_so_far = _concat_messages(messages[:message_idx+1])

                message_end_idx = tokenizer(
                    messages_so_far,
                    return_tensors='pt', 
                    max_length=max_seq_length, 
                    truncation=True
                ).input_ids.shape[1]
                labels[:, message_start_idx:message_end_idx] = -100

                if message_end_idx >= max_seq_length:
                    break

    attention_mask = torch.ones_like(input_ids)
    return {
        'input_ids': input_ids.flatten(),
        'labels': labels.flatten(),
        'attention_mask': attention_mask.flatten(),
    }


def find_all_linear_names(model):
    classes = (nn.Linear, pu.Conv1D)
    lora_module_names = set()
    for name, module in model.named_modules():
        if any(isinstance(module, cls) for cls in classes):
            names = name.split('.')
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])

    if 'lm_head' in lora_module_names: # needed for 16-bit
        lora_module_names.remove('lm_head')
    return list(lora_module_names)

def print_trainable_parameters(model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    # if args.bits == 4: trainable_params /= 2
    print(
        f"trainable params: {trainable_params} || "
        f"all params: {all_param} || "
        f"trainable: {100 * trainable_params / all_param}"
    )

@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune, or train from scratch.
    """

    model_name_or_path: Optional[str] = field(
        default=None,
        metadata={
            "help": "The model checkpoint for weights initialization."
            "Don't set if you want to train a model from scratch."
        },
    )
    config_name: Optional[str] = field(
        default=None,
        metadata={"help": "Pretrained config name or path if not the same as model_name"},
    )
    tokenizer_name: Optional[str] = field(
        default=None,
        metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"},
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": "The `use_auth_token` argument is deprecated and will be removed in v4.34. Please use `token` instead."
        },
    )
    trust_remote_code: bool = field(
        default=False,
        metadata={
            "help": "should enable when using custom model architecture that is not yet part of the Hugging Face transformers package like MPT)."
        },
    )
    use_cache: bool = field(
        default=True,
        metadata={
            "help": (
                "Whether or not the model should return the last key/values attentions (not used by all models)."
                "Only relevant if `config.is_decoder=True`."
            )
        },
    )
    low_cpu_mem_usage: bool = field(
        default=False,
        metadata={
            "help": (
                "It is an option to create the model as an empty shell, then only materialize its parameters when the pretrained weights are loaded."
                "When set to True, it will benefit LLM loading time and RAM consumption."
            )
        },
    )


@dataclass
class DataArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.
    """

    dataset_name: Optional[str] = field(
        default=None,
        metadata={"help": "The name of the dataset to use (via the datasets library)."},
    )
    dataset_config_name: Optional[str] = field(
        default=None,
        metadata={"help": "The configuration name of the dataset to use (via the datasets library)."},
    )
    train_file: Optional[str] = field(default=None, metadata={"help": "The input training data file (a text file)."})
    validation_file: Optional[str] = field(
        default=None,
        metadata={"help": "An optional input evaluation data file to evaluate the perplexity on (a text file)."},
    )
    max_seq_length: Optional[int] = field(
        default=512,
        metadata={
            "help": "The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated."
        },
    )
    validation_split_percentage: Optional[int] = field(
        default=0,
        metadata={
            "help": "The percentage of the train set used as validation set in case there's no validation split"
        },
    )
    overwrite_cache: bool = field(
        default=False,
        metadata={"help": "Overwrite the cached preprocessed datasets or not."},
    )
    pad_to_max_length: bool = field(
        default=False,
        metadata={
            "help": "Whether to pad all samples to `max_seq_length`. "
            "If False, will pad the samples dynamically when batching to the maximum length in the batch."
        },
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        },
    )
    keep_in_memory: bool = field(
        default=False,
        metadata={"help": "Whether to keep in memory the loaded dataset. Defaults to False."},
    )
    dataset_seed: int = field(
        default=None,
        metadata={
            "help": "Seed to use in dataset processing, different seeds might yield different datasets. This seed and the seed in training arguments are not related"
        },
    )
    dataset_cache_directory: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to directory where the processed dataset will be saved. If path exists, try to load processed dataset from this path."
        },
    )
    dataset_concatenation: Optional[bool] = field(
        default=False,
        metadata={"help": "Whether to concatenate the sentence for more efficient training."},
    )


@dataclass
class FinetuneArguments:
    """
    Arguments of finetune we are going to apply on the model.
    """

    lora_rank: int = field(
        default=8,
        metadata={"help": "Rank parameter in the LoRA method."},
    )
    lora_alpha: int = field(
        default=16,
        metadata={"help": "Alpha parameter in the LoRA method."},
    )
    lora_dropout: float = field(
        default=0.05,
        metadata={"help": "Dropout parameter in the LoRA method."},
    )
    lora_target_modules: List[str] = field(
        default_factory=lambda: None,
        metadata={"help": "Target modules for the LoRA method."},
    )
    train_on_inputs: bool = field(
        default=True,
        metadata={"help": "if False, masks out inputs in loss"},
    )
    full_finetune: bool = field(
        default=False,
        metadata={"help": "if True, go full finetune"}
    )


## NOTE: formating prompt
PROMPT_DICT = {
    "prompt_with_input": (
        "Below is an instruction that describes a task, paired with an input that provides further context. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n### Response:"
    ),
    "prompt_without_input": (
        "Below is an instruction that describes a task. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Response:"
    ),
}

MY_PROMPT_DICT = {
    "prompt_with_input_en": (
        "Below is an instruction that describes a task, paired with an input that provides further context. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n### Response:\n"
    ),
    "prompt_without_input_en": (
        "Below is an instruction that describes a task. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Response:\n"
    ),
    "prompt_with_input_vi": (
        "Dưới đây là chỉ dẫn mô tả một nhiệm vụ, kèm theo đầu vào để cung cấp thêm ngữ cảnh. "
        "Đưa ra một phản hồi thích hợp để hoàn thành nhiệm vụ đó.\n\n"
        "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n### Response:\n"
    ),
    "prompt_without_input_vi": (
        "Dưới đây là chỉ dẫn mô tả một nhiệm vụ. "
        "Đưa ra một phản hồi thích hợp để hoàn thành nhiệm vụ đó.\n\n"
        "### Instruction:\n{instruction}\n\n### Response:\n"
    ),
}

def create_prompts(examples):
    prompts = {}
    prompts["source"] = []
    prompts["target"] = []
    for example in examples:
        prompt_template = (
            PROMPT_DICT["prompt_with_input"] if example["input"] != "" else PROMPT_DICT["prompt_without_input"]
        )
        source = prompt_template.format_map(example)
        prompts["source"].append(source)
        prompts["target"].append(example["output"])
    return prompts

def create_prompts_v2(examples):
    prompts = {}
    prompts["source"] = []
    prompts["target"] = []
    for example in examples:
        prompt_template = PROMPT_DICT["prompt_without_input"]
        if example.get("input"):
            prompt_template = PROMPT_DICT["prompt_with_input"]

        source = prompt_template.format_map(example)
        prompts["source"].append(source)
        prompts["target"].append(example["output"])
    return prompts

def create_prompts_v3(examples):
    prompts = {}
    prompts["source"] = []
    prompts["target"] = []
    for example in examples:
        if example.get("lang") == "vi":
            prompt_template = MY_PROMPT_DICT["prompt_without_input_vi"]
            assert len(example.get("input")) == 0
            if example.get("input"):
                prompt_template = MY_PROMPT_DICT["prompt_with_input_vi"]
        elif example.get("lang") == "en":
            prompt_template = MY_PROMPT_DICT["prompt_without_input_en"]
            assert len(example.get("input")) == 0
            if example.get("input"):
                prompt_template = MY_PROMPT_DICT["prompt_with_input_en"]
        else:
            # raise NotImplementedError()
            prompt_template = MY_PROMPT_DICT["prompt_without_input_en"]
            
        source = prompt_template.format_map(example)
        prompts["source"].append(source)
        prompts["target"].append(example["output"])
    return prompts

def create_prompts_v3(examples):
    prompts = {}
    prompts["source"] = []
    prompts["target"] = []
    for example in examples:
        assert len(example.get("input")) == 0
        prompt_template = "{instruction}"
        source = prompt_template.format_map(example)
        prompts["source"].append(source)
        prompts["target"].append(example["output"])
    return prompts
## END NOTE


def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    parser = HfArgumentParser((ModelArguments, DataArguments, GaudiTrainingArguments, FinetuneArguments))

    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args, finetune_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1])
        )
    else:
        (
            model_args,
            data_args,
            training_args,
            finetune_args,
        ) = parser.parse_args_into_dataclasses()

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger.setLevel(logging.INFO if is_main_process(training_args.local_rank) else logging.WARN)

    # Log on each process the small summary
    b16 = training_args.fp16 or training_args.bf16
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, "
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {b16}"
    )
    # Set the verbosity to info of the Transformers logger (on main process only):
    if is_main_process(training_args.local_rank):
        transformers.utils.logging.set_verbosity_info()
        transformers.utils.logging.enable_default_handler()
        transformers.utils.logging.enable_explicit_format()
    logger.info(f"Training/evaluation parameters {training_args}")

    # Set seed before initializing model.
    set_seed(training_args.seed)

    # Load pretrained model and tokenizer
    #
    # Distributed training:
    # The .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    config_kwargs = {
        "cache_dir": model_args.cache_dir,
        "revision": model_args.model_revision,
        "use_auth_token": True if model_args.use_auth_token else None,
        "trust_remote_code": True if model_args.trust_remote_code else None,
        "use_cache": False if training_args.gradient_checkpointing else model_args.use_cache,
    }
    if model_args.config_name:
        config = AutoConfig.from_pretrained(model_args.config_name, **config_kwargs)
    elif model_args.model_name_or_path:
        config = AutoConfig.from_pretrained(model_args.model_name_or_path, **config_kwargs)
    else:
        raise ValueError("Please provide value for model_name_or_path or config_name.")

    tokenizer_kwargs = {
        "cache_dir": model_args.cache_dir,
        "use_fast": model_args.use_fast_tokenizer,
        "revision": model_args.model_revision,
        "use_auth_token": True if model_args.use_auth_token else None,
    }
    if model_args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(model_args.tokenizer_name, **tokenizer_kwargs)
    elif model_args.model_name_or_path:
        tokenizer = AutoTokenizer.from_pretrained(model_args.model_name_or_path, **tokenizer_kwargs)
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported by this script."
            "You can do it from another script, save it, and load it from here, using --tokenizer_name."
        )

    # Get the datasets: you can either provide your own CSV/JSON/TXT training and evaluation files (see below)
    # or just provide the name of one of the public datasets available on the hub at https://huggingface.co/datasets/
    # (the dataset will be downloaded automatically from the datasets Hub).
    #
    # For CSV/JSON files, this script will use the column called 'text' or the first column if no column called
    # 'text' is found. You can easily tweak this behavior (see below).
    #
    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    if data_args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        raw_datasets = load_dataset(
            data_args.dataset_name,
            data_args.dataset_config_name,
            cache_dir=model_args.cache_dir,
            use_auth_token=True if model_args.use_auth_token else None,
        )

        if "validation" not in raw_datasets.keys() and training_args.do_eval:
            raw_datasets["validation"] = load_dataset(
                data_args.dataset_name,
                data_args.dataset_config_name,
                split=f"train[:{data_args.validation_split_percentage}%]",
                cache_dir=model_args.cache_dir,
                use_auth_token=True if model_args.use_auth_token else None,
            )
            raw_datasets["train"] = load_dataset(
                data_args.dataset_name,
                data_args.dataset_config_name,
                split=f"train[{data_args.validation_split_percentage}%:]",
                cache_dir=model_args.cache_dir,
                use_auth_token=True if model_args.use_auth_token else None,
            )
    else:
        data_files = {}
        dataset_args = {}
        if data_args.train_file is not None:
            data_files["train"] = data_args.train_file
        if data_args.validation_file is not None:
            data_files["validation"] = data_args.validation_file
        extension = (
            data_args.train_file.split(".")[-1]
            if data_args.train_file is not None
            else data_args.validation_file.split(".")[-1]
        )
        ### NOTE: 
        if extension == "jsonl":
            extension = "json"
        ### END NOTE
        if extension == "txt":
            extension = "text"
            dataset_args["keep_linebreaks"] = data_args.keep_linebreaks
        raw_datasets = load_dataset(
            extension,
            data_files=data_files,
            cache_dir=model_args.cache_dir,
            use_auth_token=True if model_args.use_auth_token else None,
            **dataset_args,
        )
        if data_args.dataset_seed:
            logger.info(f"Shuffling data with seed = {data_args.dataset_seed}")
            raw_datasets = raw_datasets.shuffle(seed=data_args.dataset_seed)

        # If no validation data is there, validation_split_percentage will be used to divide the dataset.
        if "validation" not in raw_datasets.keys() and training_args.do_eval:
            raw_datasets["validation"] = load_dataset(
                extension,
                data_files=data_files,
                split=f"train[:{data_args.validation_split_percentage}%]",
                cache_dir=model_args.cache_dir,
                use_auth_token=True if model_args.use_auth_token else None,
                **dataset_args,
            )
            raw_datasets["train"] = load_dataset(
                extension,
                data_files=data_files,
                split=f"train[{data_args.validation_split_percentage}%:]",
                cache_dir=model_args.cache_dir,
                use_auth_token=True if model_args.use_auth_token else None,
                **dataset_args,
            )
    if data_args.dataset_name == "tatsu-lab/alpaca":
        # Preprocessing the datasets.
        for key in raw_datasets:
            prompts = create_prompts(raw_datasets[key])
            columns_to_be_removed = list(raw_datasets[key].features.keys())
            raw_datasets[key] = raw_datasets[key].add_column("prompt_sources", prompts["source"])
            raw_datasets[key] = raw_datasets[key].add_column("prompt_targets", prompts["target"])
            raw_datasets[key] = raw_datasets[key].remove_columns(columns_to_be_removed)
    elif (
        data_args.dataset_name == "timdettmers/openassistant-guanaco"
    ):  # from https://github.com/artidoro/qlora/blob/main/qlora.py#L621
        raw_datasets = raw_datasets.map(
            lambda x: {
                "input": "",
                "output": x["text"],
            }
        )
        # Remove unused columns.
        raw_datasets = raw_datasets.remove_columns(
            [col for col in raw_datasets.column_names["train"] if col not in ["input", "output"]]
        )
    else:
        ## NOTE: read local file
        print(raw_datasets)
        for key in raw_datasets:
            prompts = create_prompts_v3(raw_datasets[key])
            columns_to_be_removed = list(raw_datasets[key].features.keys())
            raw_datasets[key] = raw_datasets[key].add_column("prompt_sources", prompts["source"])
            raw_datasets[key] = raw_datasets[key].add_column("prompt_targets", prompts["target"])
            raw_datasets[key] = raw_datasets[key].remove_columns(columns_to_be_removed)
        print(raw_datasets)
        logger.info("Data examples")
        print(raw_datasets["train"][42])
        ## END NOTE.

    # Load model
    if model_args.model_name_or_path:
        model_dtype = torch.bfloat16 if training_args.bf16 else None
        model = AutoModelForCausalLM.from_pretrained(
            model_args.model_name_or_path,
            from_tf=bool(".ckpt" in model_args.model_name_or_path),
            config=config,
            cache_dir=model_args.cache_dir,
            revision=model_args.model_revision,
            use_auth_token=True if model_args.use_auth_token else None,
            trust_remote_code=True if model_args.trust_remote_code else None,
            torch_dtype=model_dtype,
            low_cpu_mem_usage=model_args.low_cpu_mem_usage,
        )
    else:
        raise ValueError("Must provide model_name_or_path to load a pretrained CausalLM model.")

    if model.config.model_type == "llama":
        # unwind broken decapoda-research config
        model.generation_config.pad_token_id = 0
        model.generation_config.bos_token_id = 1
        model.generation_config.eos_token_id = 2

    if hasattr(model.generation_config, "pad_token_id") and model.generation_config.pad_token_id is not None:
        tokenizer.pad_token_id = model.generation_config.pad_token_id
    if hasattr(model.generation_config, "eos_token_id") and model.generation_config.eos_token_id is not None:
        tokenizer.eos_token_id = model.generation_config.eos_token_id
    if hasattr(model.generation_config, "bos_token_id") and model.generation_config.bos_token_id is not None:
        tokenizer.bos_token_id = model.generation_config.bos_token_id

    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id

    def tokenize(prompt, add_eos_token=True):
        results = tokenizer(
            prompt,
            truncation=True,
            max_length=data_args.max_seq_length,
            padding=False,
            return_tensors=None,
        )
        for i in range(len(results["input_ids"])):
            if (
                results["input_ids"][i][-1] != tokenizer.eos_token_id
                and len(results["input_ids"][i]) < data_args.max_seq_length
                and add_eos_token
            ):
                results["input_ids"][i].append(tokenizer.eos_token_id)
                results["attention_mask"][i].append(1)

        results["labels"] = copy.deepcopy(results["input_ids"])
        results["input_id_len"] = [len(result) for result in results["input_ids"]]
        return results

    def preprocess_function(examples):
        keys = list(examples.data.keys())
        if len(keys) != 2:
            raise ValueError("Unsupported dataset format")

        st = [s + t for s, t in zip(examples[keys[0]], examples[keys[1]])]

        examples_tokenized = tokenize(st)
        input_ids = examples_tokenized["input_ids"]
        labels = examples_tokenized["labels"]
        if not finetune_args.train_on_inputs:
            sources_tokenized = tokenize(examples[keys[0]], add_eos_token=False)
            for label, source_len in zip(labels, sources_tokenized["input_id_len"]):
                label[:source_len] = [IGNORE_INDEX] * source_len
        return {
            "input_ids": input_ids,
            "labels": labels,
            "attention_mask": examples_tokenized["attention_mask"],
        }

    with training_args.main_process_first(desc="dataset map pre-processing"):
        tokenized_datasets = raw_datasets.map(
            preprocess_function,
            batched=True,
            load_from_cache_file=not data_args.overwrite_cache,
        )
        ## NOTE: logging #tokens taking loss
        train_split = tokenized_datasets["train"]
        total_tokens = 0
        train_tokens = 0
        for label in train_split["labels"]:
            total_tokens += len(label)
            train_tokens += len([token for token in label if token != IGNORE_INDEX])
        logger.info(f">>> Train on inputs: {finetune_args.train_on_inputs}")
        logger.info(f">>> Total number of tokens: {total_tokens}")
        logger.info(f">>> Number of tokens taking loss: {train_tokens}")
        
        print(tokenized_datasets)
        print(">>> Full example:")
        print(tokenizer.decode(tokenized_datasets["train"][0]["input_ids"]))
        print(">>> Part of the example that takes gradients:")
        print(tokenizer.decode([i for i in tokenized_datasets["train"][0]["labels"] if i >= 0]))
        ### END NOTE

    if data_args.dataset_concatenation:

        def concatenate_data(dataset, max_seq_length):
            concatenated_dataset = {}
            for column in dataset.features:
                concatenated_data = [item for sample in dataset[column] for item in sample]
                reshaped_data = [
                    concatenated_data[i * max_seq_length : (i + 1) * max_seq_length]
                    for i in range(len(concatenated_data) // max_seq_length)
                ]
                concatenated_dataset[column] = reshaped_data
            return datasets.Dataset.from_dict(concatenated_dataset)

        if data_args.dataset_name == "tatsu-lab/alpaca":
            tokenized_datasets_ = tokenized_datasets["train"].remove_columns(["prompt_sources", "prompt_targets"])
            if training_args.do_eval:
                tokenized_datasets_eval_ = tokenized_datasets["validation"].remove_columns(
                    ["prompt_sources", "prompt_targets"]
                )
        elif data_args.dataset_name == "timdettmers/openassistant-guanaco":
            tokenized_datasets_ = tokenized_datasets["train"].remove_columns(["input", "output"])
            if training_args.do_eval:
                tokenized_datasets_eval_ = tokenized_datasets["test"].remove_columns(["input", "output"])
        else:
            ## NOTE: read local file
            tokenized_datasets_ = tokenized_datasets["train"].remove_columns(["prompt_sources", "prompt_targets"])
            if training_args.do_eval:
                tokenized_datasets_eval_ = tokenized_datasets["test"].remove_columns(
                    ["prompt_sources", "prompt_targets"]
                )
            # raise ValueError("Unsupported dataset")
            ## END NOTE
            
        tokenized_datasets["train"] = concatenate_data(tokenized_datasets_, data_args.max_seq_length)
        if training_args.do_eval:
            tokenized_datasets["validation"] = concatenate_data(tokenized_datasets_eval_, data_args.max_seq_length)
    if training_args.do_train:
        if "train" not in tokenized_datasets:
            raise ValueError("--do_train requires a train dataset")
        train_dataset = tokenized_datasets["train"]
        if data_args.max_train_samples is not None:
            train_dataset = train_dataset.select(range(data_args.max_train_samples))

    if training_args.do_eval:
        if "validation" not in tokenized_datasets:
            raise ValueError("--do_eval requires a validation dataset")
        eval_dataset = tokenized_datasets["validation"]
        if data_args.max_eval_samples is not None:
            eval_dataset = eval_dataset.select(range(data_args.max_eval_samples))

        def preprocess_logits_for_metrics(logits, labels):
            if isinstance(logits, tuple):
                # Depending on the model and config, logits may contain extra tensors,
                # like past_key_values, but logits always come first
                logits = logits[0]
            return logits.argmax(dim=-1)

        metric = evaluate.load("accuracy")

        def compute_metrics(eval_preds):
            preds, labels = eval_preds
            # preds have the same shape as the labels, after the argmax(-1) has been calculated
            # by preprocess_logits_for_metrics but we need to shift the labels
            labels = labels[:, 1:].reshape(-1)
            preds = preds[:, :-1].reshape(-1)
            return metric.compute(predictions=preds, references=labels)

    # Data collator
    # This one will take care of randomly masking the tokens.
    data_collator = DataCollatorForLanguageModeling(tokenizer, pad_to_multiple_of=8, return_tensors="pt", mlm=False)
    logger.info("Using data collator of type {}".format(data_collator.__class__.__name__))

    if training_args.do_train or training_args.do_eval:
        # PEFT settings
        ### NOTE: added code
        from peft.utils import TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING as MAP
        if finetune_args.lora_target_modules == ["from_mapping"]:
            if MAP.get(model.config.model_type):
                finetune_args.lora_target_modules = MAP.get(model.config.model_type)
            else:
                logger.info(f"Cannot find predefined target lora modules for model type {model.config.model_type}.")
                finetune_args.lora_target_modules = find_all_linear_names(model)
        elif finetune_args.lora_target_modules == "all":
            finetune_args.lora_target_modules = find_all_linear_names(model)
        logger.info(f">>> LoRA target modules: {finetune_args.lora_target_modules}")
        ### END NOTE

        ### NOTE: add full finetune branch
        if training_args.gradient_checkpointing:
            model.enable_input_require_grads()
        if not finetune_args.full_finetune:
            peft_config = LoraConfig(
                r=finetune_args.lora_rank,
                lora_alpha=finetune_args.lora_alpha,
                lora_dropout=finetune_args.lora_dropout,
                target_modules=finetune_args.lora_target_modules,
                bias="none",
                task_type=TaskType.CAUSAL_LM,
            )
            model = get_peft_model(model, peft_config)
            
        if training_args.bf16:
            model = model.to(torch.bfloat16)
        print_trainable_parameters(model)
        
        gaudi_config = GaudiConfig()
        gaudi_config.use_fused_adam = True
        gaudi_config.use_fused_clip_norm = True

        # Initialize our Trainer
        trainer = GaudiTrainer(
            # model=lora_model, 
            model=model, ## NOTE: change model=lora_model to model=model ## END NOTE
            gaudi_config=gaudi_config,
            args=training_args,
            train_dataset=train_dataset if training_args.do_train else None,
            eval_dataset=eval_dataset if training_args.do_eval else None,
            tokenizer=tokenizer,
            data_collator=data_collator,
            compute_metrics=compute_metrics if training_args.do_eval else None,
            preprocess_logits_for_metrics=preprocess_logits_for_metrics if training_args.do_eval else None,
        )

    if training_args.do_train:
        train_result = trainer.train(resume_from_checkpoint=training_args.resume_from_checkpoint)

        with training_args.main_process_first(desc="save model"):
            if is_main_process(training_args.local_rank):
                # unwrapped_model = unwrap_model(lora_model)
                # unwrapped_model = unwrap_model(model)  ## NOTE: change model=lora_model to model=model ## END NOTE
                # unwrapped_model.save_pretrained(training_args.output_dir, state_dict=unwrapped_model.state_dict())
                trainer.save_model()

        metrics = train_result.metrics
        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)

        # Evaluation
    if training_args.do_eval:
        logger.info("*** Evaluate ***")
        metrics = trainer.evaluate()

        max_eval_samples = data_args.max_eval_samples if data_args.max_eval_samples is not None else len(eval_dataset)
        metrics["eval_samples"] = min(max_eval_samples, len(eval_dataset))

        try:
            perplexity = math.exp(metrics["eval_loss"])
        except OverflowError:
            perplexity = float("inf")
        metrics["perplexity"] = perplexity

        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)


if __name__ == "__main__":
    main()