#!/bin/bash
cd ~/a/celex-facts
source .venv/bin/activate
export PYTHONPATH="~/a/celex-facts/src"

.venv/bin/python src/pipelines/utils/search.py "$@"
