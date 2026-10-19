#!/bin/bash
cd ~/a/celex-facts
source .venv/bin/activate
export PYTHONPATH="~/a/celex-facts/src"

.venv/bin/python src/batch/Main_ingest_generic_facts.py