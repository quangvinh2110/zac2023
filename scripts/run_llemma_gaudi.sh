#!/bin/bash
python3 run_clm_lora_local.py \
    --model_name_or_path ../model_hub/llemma_7b \
    --train_file ../data/thanhdath_dont-v1.jsonl \
    --bf16 True \
    --output_dir ./output/llemma_dont-v1 \
    --num_train_epochs 2 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 16 \
    --evaluation_strategy "no" \
    --save_strategy "steps" \
    --save_steps 1000 \
    --learning_rate 1e-4 \
    --warmup_ratio  0.03 \
    --lr_scheduler_type "constant" \
    --max_grad_norm  0.3 \
    --logging_steps 10 \
    --do_train \
    --use_habana \
    --use_lazy_mode \
    --throughput_warmup_steps 3 \
    --lora_rank=64 \
    --lora_alpha=16 \
    --lora_dropout=0.05 \
    --lora_target_modules "from_mapping" \
    --dataset_concatenation \
    --max_seq_length 1024 \
    --ddp_bucket_cap_mb 50 \
    --low_cpu_mem_usage True \
    --train_on_inputs False \
    --full_finetune True 