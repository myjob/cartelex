#!/bin/bash
# Stage the GPU clock guard on the DGX Spark and print the one privileged
# command needed to finish the install.
#
# This script does everything that does not need root: it copies the wrapper and
# the sudoers rule into the SSH user's home on the Spark. The final step needs a
# password on that box, so it is printed for you to run rather than attempted
# here.
#
# See section 7.5.3 of docs/infra/INSTALL celex-facts.md.
set -euo pipefail

cd ~/a/celex-facts
export PYTHONPATH="~/a/celex-facts/src"

# Importing config.settings configures logging, which announces itself on
# stdout; only the last line of each call is the value we asked for.
SSH_USER=$(.venv/bin/python -c 'from config.settings import settings; print(settings.DGX_SSH_USER)' | tail -n 1)
SSH_HOST=$(.venv/bin/python -c 'from config.ollama_health_gate import dgx_ssh_host; print(dgx_ssh_host())' | tail -n 1)
GUARD_PATH=$(.venv/bin/python -c 'from config.settings import settings; print(settings.DGX_CLOCK_GUARD_PATH)' | tail -n 1)

TARGET="${SSH_USER}@${SSH_HOST}"
SUDOERS_FILE="/etc/sudoers.d/gpu-clock-guard"

echo "Staging the clock guard on ${TARGET}"

scp -o BatchMode=yes scripts/dgx_gpu_clock_guard.sh "${TARGET}:~/gpu-clock-guard"

# The sudoers rule names the wrapper and nothing else. No wildcard: sudo matches
# arguments as one concatenated string, so a wildcard would also permit extra
# nvidia-smi flags such as -r (GPU reset). The wrapper validates its own
# arguments instead.
ssh -o BatchMode=yes "${TARGET}" "cat > ~/gpu-clock-guard.sudoers" <<EOF
# Installed by celex-facts scripts/install_dgx_clock_guard.sh
# Allows the thermal guard to cap and restore the GPU boost clock only.
${SSH_USER} ALL=(root) NOPASSWD: ${GUARD_PATH}
EOF

cat <<EOF

Staged. Now run this one command -- it will ask for your password on the Spark:

  ssh -t ${TARGET} '
    sudo visudo -cf ~/gpu-clock-guard.sudoers &&
    sudo install -o root -g root -m 0755 ~/gpu-clock-guard ${GUARD_PATH} &&
    sudo install -o root -g root -m 0440 ~/gpu-clock-guard.sudoers ${SUDOERS_FILE} &&
    rm -f ~/gpu-clock-guard ~/gpu-clock-guard.sudoers &&
    echo "installed" && sudo -n ${GUARD_PATH} reset && echo "clock reset OK"'

The sudoers file is syntax-checked with 'visudo -cf' before it is installed, so
a malformed rule cannot lock you out of sudo.

Then verify from here (no password should be needed):

  .venv/bin/python -m config.dgx_clock_control --reset

EOF
