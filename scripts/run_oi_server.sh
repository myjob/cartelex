#!/bin/bash

# set -e
# 
# if ! command -v uvicorn &> /dev/null; then
#     echo "uvicorn not found. Installing it in the venv..."
#     source .venv/bin/activate
#     python -m pip install uvicorn
#     deactivate
# fi
cd ~/a/celex-facts
source deactivate
source .venv/bin/activate 
cd ~/a/celex-facts/src/ui/backend_server
PYTHONPATH=~/a/celex-facts/src uvicorn oi_dspy:app --host 0.0.0.0 --port 7777
