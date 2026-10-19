#!/bin/bash
# GPU boost-clock guard for the NVIDIA DGX Spark. Installed on the Spark as
# /usr/local/sbin/gpu-clock-guard, owned by root, and invoked through a
# NOPASSWD sudoers rule by config.dgx_clock_control on the workstation.
#
# Why a wrapper instead of granting nvidia-smi directly: sudo matches command
# line arguments as one concatenated string, and its own manual warns that a
# wildcard there matches multiple words. A rule such as
# "nvidia-smi -lgc *" would therefore also permit
# "nvidia-smi -lgc 300,2200 -i 0 -r" -- a GPU reset. This script is the entire
# allowed surface: two verbs, numeric arguments only, nothing passed through to
# a shell.
#
# Exit codes:
#   0   the requested change was applied
#   64  the arguments were not understood or were out of range (EX_USAGE)
#   other   whatever nvidia-smi returned
set -euo pipefail

# Fixed, absolute, and not inherited: this runs as root.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
readonly NVIDIA_SMI=/usr/bin/nvidia-smi

# Widest range the caller may ask for. The GB10 reports a 3003 MHz maximum. The
# floor is 100 rather than something tighter because the documented cap for this
# box is "-lgc 300,2200", so 300 must be a legal minimum; the check exists to
# reject a typo or a garbage argument, not to second-guess the configuration.
readonly CLOCK_FLOOR_MHZ=100
readonly CLOCK_CEILING_MHZ=3003

usage() {
    echo "usage: gpu-clock-guard reset | cap <min_mhz> <max_mhz>" >&2
    exit 64
}

# Run nvidia-smi and fail loudly when it only *says* it failed.
#
# Measured: "nvidia-smi -lgc 300,2200" on a device that does not support locked
# clocks prints "Setting locked GPU clocks is not supported for GPU ..." and
# still exits 0. Passing that back as success would let the caller believe a cap
# is in force when the clock is untouched -- the worst possible lie to tell a
# thermal guard. Exit 70 (EX_SOFTWARE) on that case instead.
run_nvidia_smi() {
    local output status
    set +e
    output=$("$NVIDIA_SMI" "$@" 2>&1)
    status=$?
    set -e

    [[ -n "$output" ]] && echo "$output"
    if (( status != 0 )); then
        exit "$status"
    fi
    if grep -qiE 'not supported|unable to|insufficient permission|failed' \
        <<<"$output"; then
        exit 70
    fi
    exit 0
}

is_valid_clock() {
    # One to four digits, then an explicit range check. Rejects anything with a
    # sign, decimal point, whitespace or shell metacharacter.
    [[ "$1" =~ ^[0-9]{1,4}$ ]] || return 1
    (( $1 >= CLOCK_FLOOR_MHZ && $1 <= CLOCK_CEILING_MHZ )) || return 1
}

case "${1:-}" in
    reset)
        [[ $# -eq 1 ]] || usage
        run_nvidia_smi -rgc
        ;;
    cap)
        [[ $# -eq 3 ]] || usage
        min_mhz="$2"
        max_mhz="$3"
        is_valid_clock "$min_mhz" || usage
        is_valid_clock "$max_mhz" || usage
        (( min_mhz <= max_mhz )) || usage
        run_nvidia_smi -lgc "${min_mhz},${max_mhz}"
        ;;
    *)
        usage
        ;;
esac
