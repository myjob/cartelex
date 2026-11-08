# CUDA_HOME=$PWD/.venv/lib/python3.12/site-packages/nvidia/cu13 .venv/bin/mineru-api --tier advanced
cd "$(dirname "$0")"
# exec systemd-run --user --scope -p MemoryMax=18G -p MemorySwapMax=0 \
#  env VLLM_USE_FLASHINFER_SAMPLER=0 .venv/bin/mineru-api --tier advanced

cd ~/a/mineru/Mineru
systemd-run --user --scope -p MemoryMax=22G -p MemorySwapMax=0 \
  env VLLM_USE_FLASHINFER_SAMPLER=0 MINERU_MALLOC_TRIM=1 MALLOC_ARENA_MAX=2 \
  .venv/bin/mineru-api --tier advanced
