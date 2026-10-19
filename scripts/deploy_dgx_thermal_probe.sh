#!/bin/bash
# Deploy the thermal probe to the DGX Spark and prove it runs there.
#
# The probe is stdlib-only by design, so the box needs no venv and no copy of
# this repository -- a single file plus the system python3 is enough.
#
# The target host is derived from settings.OLLAMA_HOST so the machine being
# measured is always the machine serving inference.
set -euo pipefail

cd ~/a/celex-facts
export PYTHONPATH="~/a/celex-facts/src"

# Importing config.settings configures logging, which announces itself on
# stdout; only the last line of each call is the value we asked for.
SSH_USER=$(.venv/bin/python -c 'from config.settings import settings; print(settings.DGX_SSH_USER)' | tail -n 1)
SSH_HOST=$(.venv/bin/python -c 'from config.ollama_health_gate import dgx_ssh_host; print(dgx_ssh_host())' | tail -n 1)
PROBE_PATH=$(.venv/bin/python -c 'from config.settings import settings; print(settings.DGX_SSH_PROBE_PATH)' | tail -n 1)

TARGET="${SSH_USER}@${SSH_HOST}"
REMOTE_DIR=$(dirname "${PROBE_PATH}")

echo "Deploying src/config/dgx_thermal_probe.py to ${TARGET}:${PROBE_PATH}"

ssh -o BatchMode=yes "${TARGET}" "mkdir -p ${REMOTE_DIR}"
scp -o BatchMode=yes src/config/dgx_thermal_probe.py "${TARGET}:${PROBE_PATH}"
ssh -o BatchMode=yes "${TARGET}" "chmod +x ${PROBE_PATH}"

echo "Verifying:"
ssh -o BatchMode=yes "${TARGET}" "python3 ${PROBE_PATH} --pretty"
