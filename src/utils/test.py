import os
print('Get current working directory : ', os.getcwd())

print("This file full path (following symlinks)")
full_path = os.path.realpath(__file__)
print(full_path + "\n")

print("/".join(full_path.split("/")[:-3])+"/data/zalo/train/zalo_code_filterd.jsonl")