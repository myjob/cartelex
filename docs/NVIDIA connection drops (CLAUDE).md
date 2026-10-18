## Diagnosis Summary: DGX Spark Bi-Daily Connection Drops

### What's Actually Happening

The onboard **Realtek RTL8127** NIC has a hard 1024-descriptor RX ring buffer that saturates under sustained Ollama inference traffic. When it fills, the hardware silently discards frames (`rx_mac_missed: 3366` confirmed this). The OS stays alive — you lose the _connection_, not the machine. The SBSA watchdog would recover a true freeze; it never fires because there's nothing to recover from.

### Hardware Tuning: All Doors Closed

- RX ring buffer: already at hardware maximum (1024)
- Interrupt coalescing: driver doesn't support it
- The r8127 on aarch64 is essentially untunable

### What Was Applied / Still To Do

|Fix|Status|
|---|---|
|sysctl netdev_budget + rmem|✅ Apply now|
|Ollama queue + parallel limits|✅ Apply now|
|Nginx/Caddy rate-limiting proxy|Recommended next step|
|USB4/Thunderbolt Intel NIC|Real long-term fix|

### One Number to Watch

After applying sysctl and Ollama limits, monitor `rx_mac_missed` during load. If it stops climbing, the mitigations are holding. If it keeps climbing at the same rate, skip straight to the proxy or the external NIC.

## Driver Doesn't Support Coalescing — We've Hit All Hardware Limits

The r8127 driver simply doesn't implement coalescing control. That's three strikes on hardware-level fixes:

|Tuning Lever|Result|
|---|---|
|RX ring buffer expand|❌ Hardware max already at 1024|
|Interrupt coalescing|❌ Driver doesn't support it|
|Coalescing timing|❌ Same — not implemented|

The RTL8127 + r8127 driver on aarch64 is essentially untunable beyond defaults.

---

### What's Left: The Software Side Only

**Apply the sysctl tuning now — this still helps:**

```bash
sudo tee /etc/sysctl.d/99-nic-tuning.conf << EOF
net.core.netdev_budget=1000
net.core.netdev_budget_usecs=10000
net.core.netdev_max_backlog=10000
net.core.rmem_max=134217728
net.core.rmem_default=134217728
net.ipv4.tcp_rmem=4096 87380 134217728
net.ipv4.tcp_wmem=4096 65536 134217728
EOF

sudo sysctl -p /etc/sysctl.d/99-nic-tuning.conf
```

**Apply Ollama limits now:**

```bash
sudo systemctl edit ollama
```

```ini
[Service]
Environment="OLLAMA_MAX_QUEUE=4"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_KEEP_ALIVE=5m"
Environment="OLLAMA_DEBUG=0"
MemoryMax=180G
MemorySwapMax=0
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

---

### The Real Fix: Put a Proxy In Front

Since we can't fix the NIC, throttle _before_ traffic reaches it. Caddy is the simplest option:

```bash
sudo apt install caddy

sudo tee /etc/caddy/Caddyfile << 'EOF'
:11435 {
    reverse_proxy localhost:11434 {
        transport http {
            keepalive 30s
            keepalive_idle_conns 10
        }
    }
    rate_limit {
        zone ollama_zone {
            match {
                path /*
            }
            key static
            events 10
            window 1s
        }
    }
}
EOF
```

Or nginx if you prefer:

```bash
sudo apt install nginx

sudo tee /etc/nginx/conf.d/ollama.conf << 'EOF'
upstream ollama {
    server 127.0.0.1:11434;
    keepalive 8;
}

server {
    listen 11435;
    location / {
        limit_conn_zone $binary_remote_addr zone=ollama_conn:10m;
        limit_conn ollama_conn 4;
        limit_req_zone $binary_remote_addr zone=ollama_req:10m rate=5r/s;
        limit_req zone=ollama_req burst=10 nodelay;
        proxy_pass http://ollama;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
EOF

sudo systemctl restart nginx
```

Then change `OLLAMA_HOST` to `127.0.0.1:11434` so Ollama only listens locally, and point clients at port `11435` instead.

---

### Honest Bottom Line

The DGX Spark's onboard NIC has a 1024-descriptor ceiling with no driver tuning support. For a machine of this GPU caliber running inference under load, **a USB4/Thunderbolt Intel NIC is the correct long-term fix** — the software mitigations will reduce drop frequency but won't eliminate it. The `rx_mac_missed` counter will tell you after your next load session whether the sysctl changes bought meaningful headroom.



# next day

Every 30.0s: 
```
ethtool -S enP7s7 | grep missed 
spark-niki: Tue Apr 21 05:22:56 2026 
	rx_missed: 0 
	rx_mac_missed: 16001
```

on usb4, 21-Apr =  rx_mac_missed: 21051
## That's Bad and Getting Worse Fast

### The Numbers

|Time|rx_mac_missed|Delta|
|---|---|---|
|Yesterday (first check)|3,366|baseline|
|Now (Apr 21, 05:22)|16,001|**+12,635 since yesterday**|

That's roughly **1,000+ dropped frames per hour** accumulating on an already-maxed ring buffer. The sysctl tuning hasn't been enough — you are actively losing frames right now, likely while Ollama is idle or under light load. Under real inference load it will be significantly worse.

### The sysctl changes have not been applied yet, or aren't helping enough

Check if they took:

```bash
sysctl net.core.netdev_budget
sysctl net.core.netdev_max_backlog
```

If those still show defaults (`netdev_budget=300`, `netdev_max_backlog=1000`), the conf file wasn't loaded. Run:

```bash
sudo sysctl -p /etc/sysctl.d/99-nic-tuning.conf
```

### Stop the bleeding today — rate-limit proxy is no longer optional

At this accumulation rate you will hit your next drop within hours. Do the nginx proxy now:

```bash
sudo apt install nginx -y

sudo tee /etc/nginx/conf.d/ollama.conf << 'EOF'
limit_conn_zone $binary_remote_addr zone=ollama_conn:10m;
limit_req_zone $binary_remote_addr zone=ollama_req:10m rate=5r/s;

upstream ollama_backend {
    server 127.0.0.1:11434;
    keepalive 8;
}

server {
    listen 11435;
    location / {
        limit_conn ollama_conn 4;
        limit_req zone=ollama_req burst=10 nodelay;
        proxy_pass http://ollama_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 600s;
    }
}
EOF

# Lock Ollama to localhost only
sudo systemctl edit ollama
```

Change `OLLAMA_HOST` to `127.0.0.1:11434`, then:

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
sudo systemctl restart nginx
```

Point all clients at port `11435` instead of `11434`.

### Order an Intel NIC today

The software mitigations are managing a hardware deficiency. This counter will keep climbing regardless. The DGX Spark has USB4 ports — a **USB4/Thunderbolt Intel I225 or I226-based adapter** is €30-50 and will give you a proper tunable NIC with a 4096-descriptor ring and full coalescing support. That permanently closes this issue.


# next day crash

```
journalctl -k --since "1 hour ago" | grep -E "oom|kill|memory"
```
## Found Two Problems

### Problem 1: Embedding Model is Loading with 262K Context

From your earlier logs:

```
runner.name=qwen3-embedding:8b
runner.num_ctx=262144    ← this is wrong
runner.size=14.3 GiB     ← inflated because of it
```

Ollama is applying your global context length to the embedding model too. An embedding model never needs 262K context — it processes short chunks by design. That 14.3 GiB should be closer to 5-6 GiB with a sane context limit.

### Problem 2: The Client Code Is Not Chunking

```python
client.embed(model=model, input=text)  # what is 'text' here?
```

The log showed `prompt=213,883 tokens` being sent to the second model. If `text` is a raw document being fed whole into the embedding model, that's both the memory killer and semantically wrong — embedding models are designed for **chunked** text (typically 512–2048 tokens per chunk), not entire documents.

---

### Fix 1: Cap Embedding Model Context via Modelfile

```bash
ollama show qwen3-embedding:8b --modelfile > embedding-custom.modelfile

# Add this line to the modelfile
echo "PARAMETER num_ctx 2048" >> embedding-custom.modelfile

# Rebuild as a local model
ollama create qwen3-embedding:8b-local -f embedding-custom.modelfile
```

Then use `qwen3-embedding:8b-local` in your client.

---

### Fix 2: Chunk the Input Properly

```python
import ollama

def get_embeddings(text: str, model: str, host: str, 
                   chunk_size: int = 512) -> list:
    client = ollama.Client(host=host)
    
    # Split into word-based chunks
    words = text.split()
    chunks = [
        " ".join(words[i:i + chunk_size]) 
        for i in range(0, len(words), chunk_size)
    ]
    
    # Single batched call — much more efficient than looping
    response = client.embed(
        model=model,
        input=chunks   # ollama accepts a list
    )
    
    return response.embeddings  # list of vectors, one per chunk
```

If you need one vector per document (for RAG retrieval), mean-pool the chunks:

```python
import numpy as np

def embed_document(text: str, model: str, host: str) -> np.ndarray:
    embeddings = get_embeddings(text, model, host)
    return np.mean(embeddings, axis=0)
```

---

### Memory Budget After These Fixes

|Model|Before|After|
|---|---|---|
|qwen3-coder:30b @ 262K ctx|~35 GiB|~35 GiB (unchanged)|
|qwen3-embedding:8b @ 262K ctx|~14.3 GiB|~5-6 GiB|
|**Total**|**~50 GiB**|**~41 GiB**|

The DGX Spark has 128 GiB unified memory but the OS and other processes need headroom. Dropping the embedding model from 14 → 5 GiB gives meaningful breathing room and removes the KV cache spike that came from 213K-token embedding requests.

What does `text` contain in your client code — is it a raw document, a query string, or something else?
## Modelfile Looks Perfect — Build It

```bash
ollama create qwen3-embedding:8b-local -f embedding-custom.modelfile
```

Then verify the context took:

```bash
ollama show qwen3-embedding:8b-local --modelfile | grep num_ctx
# Should show: PARAMETER num_ctx 2048
```

And verify the memory footprint dropped after loading it once:

```bash
ollama run qwen3-embedding:8b-local ""
nvidia-smi --query-gpu=memory.used --format=csv,noheader
# Expect ~5-6 GiB vs the previous 14.3 GiB
```

---

### Update Your Client

```python
# Before
model = "qwen3-embedding:8b"

# After
model = "qwen3-embedding:8b-local"
```

---

### Then Confirm Both Models Fit Comfortably

Once both are loaded simultaneously run:

```bash
ollama ps
```

You should see something like:

```
NAME                        SIZE    PROCESSOR
qwen3-coder:30b             ~35 GiB  GPU
qwen3-embedding:8b-local    ~5 GiB   GPU
```

That's ~40 GiB total against 128 GiB unified — plenty of headroom for the OS, KV cache growth during long coder sessions, and no more memory cliff. The 213K-token embedding requests were the direct trigger for your last freeze. With `num_ctx 2048` the model will simply truncate rather than allocate a 200K KV cache and take the system down with it.

# next-next day crash

## Root Cause Identified: KV Cache Is Eating Your Memory

### The Timeline

```
08:08:51  Tailscale flickers (Frankfurt relay) — red herring, client is localhost local
08:09:25  Last embed + chat completion — both 200 OK
08:10:04  Final successful requests — chat took 39 seconds ← near full context
08:10:04  App starts case 38344_5856_3 (never completes)
          [Ollama receives it, starts processing, system freezes silently]
08:20:05  App notices — starts retrying after OpenAI timeout
```

Ollama was alive and healthy. The freeze happened **on the next request**, not during an existing one. Tailscale is completely unrelated — your client is on local LAN `localhost`.

---

### The Memory Math is the Problem

```
ollama ps:
qwen3-coder:30b      SIZE = 71 GB    ← this is NOT just model weights
qwen3-embedding:8b   SIZE =  5.7 GB
                            -------
                            76.7 GB  just for loaded models
```

In `ollama ps`, SIZE includes **model weights + allocated KV cache**. For qwen3-coder:30b the weights alone should be ~20-25GB (Q4). The remaining ~45-50GB **is KV cache for a 262K context window**. That 39-second inference request means it was processing close to the full context.

Add OS + your pipeline app + vector DB overhead and you're likely at 100-110GB of 128GB unified memory. The next request tips it over the edge.

---

### Fix: Compress the KV Cache

You can cut that 71GB down dramatically without changing model quality:

```bash
sudo systemctl edit ollama
```

```ini
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=5m"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_DEBUG=INFO"
MemoryMax=115G
MemorySwapMax=0
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

`OLLAMA_KV_CACHE_TYPE=q8_0` halves KV cache memory. If still tight, `q4_0` halves it again:

|KV Cache Type|Estimated coder:30b SIZE|Total with embedding|
|---|---|---|
|f16 (current default)|~71 GB|~77 GB|
|q8_0|~45 GB|~51 GB|
|q4_0|~32 GB|~38 GB|

---

### Verify Before Next Run

```bash
# After restart, load both models and check actual SIZE
ollama run qwen3-coder:30b ""
ollama ps

# Watch memory during first inference
watch -n 3 'free -h | head -2 && ollama ps'
```

You want `free -h` to show at least 20-25GB free during peak inference — that's your safety margin. If `q8_0` still cuts it too close, drop to `q4_0`. The quality difference on KV cache quantization is minimal for most tasks.Do I need the KV cache at all?
-->
## No — But You Can't Disable It. Here's Why.

The KV cache is intrinsic to how transformer inference works. Every single request uses it internally to avoid recomputing attention for every token during generation. It's not optional.

**What IS optional is the size you pre-allocate for it.** Ollama allocates KV cache for the full `num_ctx` upfront when the model loads. Your 262K context = ~45-50GB pre-allocated and sitting there whether you use it or not.

---

### The Real Question: Do You Actually Use 262K?

Your logs show the actual token usage:

```
Tokens for embeddings: 1119          ← tiny, 2K ctx is perfect
chat/completions took 39 seconds     ← long, but how long is the actual prompt?
```

Check what your pipeline is actually sending:

```bash
# Turn on request logging temporarily
sudo systemctl edit ollama
# Add: Environment="OLLAMA_DEBUG_LOG_REQUESTS=true"
sudo systemctl daemon-reload && sudo systemctl restart ollama

# Then watch real prompt lengths
journalctl -u ollama -f | grep -E "prompt=|num_ctx|tokens"
```

If your legal document prompts are typically under 32K tokens, you're pre-allocating 8x more KV cache than needed.

---

### Right-Size the Context Instead

```bash
ollama show qwen3-coder:30b --modelfile > coder-custom.modelfile
```

Add based on your actual usage:

```
PARAMETER num_ctx 32768    # try this first — 32K fits most legal docs
PARAMETER num_ctx 65536    # if some docs genuinely need more
```

```bash
ollama create qwen3-coder:30b-local -f coder-custom.modelfile
```

|num_ctx|Approx KV cache|Total model SIZE|Headroom on 128GB|
|---|---|---|---|
|262144 (current)|~48 GB|~71 GB|dangerously low|
|65536|~12 GB|~35 GB|comfortable|
|32768|~6 GB|~29 GB|very safe|

Combined with embedding at 5.7GB, dropping to 32K context puts you at ~35GB total — less than half your available unified memory. That's the fix. What are your typical prompt sizes?


## Why the Silence: The Unified Memory Trap

It's a specific consequence of the GB10 architecture combined with how the GPU driver holds memory.

### Normal Crash vs. What You're Experiencing

On a normal system with discrete GPU, when RAM runs out the OOM killer fires, writes logs, kills a process, system recovers. You get messages because the CPU always has its own memory to work with.

On the GB10, **the CPU and GPU share the same physical LPDDR5X pool**. There is no separate memory the kernel can fall back to. When the GPU's KV cache consumes most of the 128GB pool, the kernel itself is starved.

### The Specific Failure Chain

```
1. Ollama loads KV cache for 262K context     → ~48GB pinned, unswappable
2. Model weights on top                       → ~23GB pinned, unswappable
3. New large request arrives
4. GPU driver requests more memory
5. Kernel attempts to allocate sk_buff 
   for incoming network packets               → FAILS, no memory
6. Packets silently dropped at interrupt level
   before any kernel code can log anything
7. TCP connections time out from client side
8. journald tries to write a log entry        → FAILS, no memory
9. Watchdog kernel thread still runs          → keeps petting /dev/watchdog0
   (it's tiny, already-resident code)         → no automatic reboot
10. System appears frozen to network          → kernel thinks it's fine
```

### Why No OOM Killer Either

The OOM killer can only reclaim memory that's **pageable**. GPU KV cache allocations are:

- Pinned (can't be swapped)
- Held by the CUDA driver context
- Invisible to the OOM killer as reclaimable

So the OOM killer looks at the memory map, sees it's all legitimately allocated, and does nothing. No kill message, no log entry.

### Why The Watchdog Doesn't Save You

```
Apr 23 systemd[1]: Watchdog running with a timeout of 10min
```

The SBSA watchdog only fires if the kernel completely stops running. But a low-level watchdog kernel thread requires almost zero memory — it's already resident in cache. It keeps petting `/dev/watchdog0` while everything else is frozen. The hardware sees a healthy heartbeat. No reset.

### This is Why MemoryMax Matters So Much

```ini
MemoryMax=115G
```

This is not just a nice-to-have. It forces the kernel's cgroup memory controller to trigger the OOM killer **on Ollama specifically** before the unified pool is exhausted. The result:

- Ollama gets killed (recoverable — systemd restarts it)
- Kernel retains enough memory to write logs
- Network stack keeps functioning
- You get a log entry instead of a silent freeze

Without it, Ollama is just another process to the kernel — allowed to grow until the whole system chokes.

### One Line Summary

> The GPU driver pins memory the OOM killer can't touch, the kernel runs out of memory to log with, but retains just enough to keep the watchdog alive — so the system appears frozen to the network while appearing healthy to the hardware.



## EFI pstore Is Already Working — And Secure Boot Explains Everything

### Two Critical Discoveries

**1. You don't need ramoops — EFI pstore is already registered:**

```
[7.544318] pstore: Registered efi_pstore as persistent store backend
```

**2. THIS is why ramoops failed:**

```
Kernel is locked down from EFI Secure Boot mode
```

Secure Boot kernel lockdown explicitly blocks arbitrary physical memory access. ramoops was never going to work. EFI pstore is the correct mechanism for this system and it's already loaded.

---

### Just Mount It

```bash
# Mount pstore — it's already backed by EFI, just not mounted
sudo mount -t pstore pstore /sys/fs/pstore

# Verify
ls /sys/fs/pstore/
# If empty = no crashes captured yet (expected)
# After next crash = files will appear here
```

Make it permanent:

```bash
echo 'pstore /sys/fs/pstore pstore defaults 0 0' | sudo tee -a /etc/fstab
```

---

### Enable Panic on Lockup

```bash
sudo tee /etc/sysctl.d/99-lockup-panic.conf << 'EOF'
kernel.softlockup_panic=1
kernel.hardlockup_panic=1
kernel.watchdog_thresh=10
EOF

sudo sysctl -p /etc/sysctl.d/99-lockup-panic.conf

# Enable GPU persistence mode
sudo nvidia-smi -pm 1
```

---

### After the Next Crash, Immediately Run

```bash
ls /sys/fs/pstore/       # files here = crash captured
cat /sys/fs/pstore/*     # read before systemd-pstore clears them
```

You are now fully instrumented. The next freeze will write its cause to EFI NVRAM and survive the power cycle.


-----
pr 23 21:00:08 spark-niki systemd[2485]:

Failed to start snap.firmware-updater.firmware-notifier.service - Service for snap application firmware-updater.firmware-notifier.

Apr 24 00:00:08 spark-niki systemd[2485]:

Failed to start snap.firmware-updater.firmware-notifier.service - Service for snap application firmware-updater.firmware-notifier.

Apr 24 01:46:41 spark-niki systemd-networkd-wait-online[8624]:

Timeout occurred while waiting for network connectivity.

Apr 24 03:00:08 spark-niki systemd[2485]:

Failed to start snap.firmware-updater.firmware-notifier.service - Service for snap application firmware-updater.firmware-notifier.

Apr 24 06:00:08 spark-niki systemd[2485]:

Failed to start snap.firmware-updater.firmware-notifier.service - Service for snap application firmware-updater.firmware-notifier.

Apr 24 06:04:02 spark-niki systemd-networkd-wait-online[10853]:

Timeout occurred while waiting for network connectivity.

Apr 24 09:00:08 spark-niki systemd[2485]:

Failed to start snap.firmware-updater.firmware-notifier.service - Service for snap application firmware-updater.firmware-notifier.
## These Are Harmless Background Noise

### `firmware-updater.firmware-notifier` — Ignore Completely

```
Failed to start snap.firmware-updater.firmware-notifier.service
```

This runs every 3 hours via a snap timer to check for firmware updates. It fails silently because it needs a desktop session/GUI to show notifications and you're running headless. It has zero operational impact — it's cosmetic. No action needed.

---

### `systemd-networkd-wait-online` — Worth a Look

```
01:46:41  Timeout waiting for network connectivity
06:04:02  Timeout waiting for network connectivity
```

This fires when something triggered a network-dependent service before the USB4 NIC finished initializing. Two scenarios:

**Likely cause:** Something waking from a brief state, or a service restarting, before `enx607d090ea316` was fully up. Possibly related to your earlier drops.

Check what depends on it:

```bash
systemctl status systemd-networkd-wait-online
systemctl cat systemd-networkd-wait-online | grep -E "Timeout|Interface"

# Is networkd even managing your interface?
networkctl status enx607d090ea316
```

Quick fix if networkd isn't needed (you're using NetworkManager or DHCP directly):

```bash
# Check which network manager is actually running
systemctl status NetworkManager
systemctl status systemd-networkd

# If NetworkManager is running and networkd isn't needed
sudo systemctl disable systemd-networkd-wait-online
```

---

### The Bigger Picture

You've now run for a meaningful stretch without a hard crash. Current status:

|Issue|Status|
|---|---|
|RTL8127 rx_mac_missed|✅ Fixed — USB4 adapter|
|KV cache memory exhaustion|✅ Fixed — embedding ctx capped, KV q8_0|
|USB autosuspend|✅ Already off|
|Thermal|✅ Managed with external fan|
|EFI pstore crash capture|✅ Ready for next event|
|softlockup_panic|✅ Applied|
|Firmware notifier failures|⚪ Cosmetic, ignore|
|networkd-wait-online timeouts|🟡 Minor, disable if networkd unused|

Has the machine stayed up since applying the fan + pstore + panic settings?

## Found the Wait-Online Cause

### What the Status Reveals

```
systemd-networkd: disabled (but running via socket activation)
enx607d090ea316:  Link UP  ✅
tailscale0:       Link UP  ✅  
docker0:          Link UP  ✅
enp1s0f1np1:      Link DOWN ← these three are causing the timeout
enP2p1s0f0np0:    Link DOWN ←
enP2p1s0f1np1:    Link DOWN ←
```

`systemd-networkd-wait-online` times out because it waits for **all managed interfaces** to come up, including those three permanently-down ports. These are likely the onboard NICs that are either unplugged or disabled — the old RTL8127 (`enP7s7` is conspicuously absent here) and possibly SFP/fiber ports on the DGX Spark.

### Fix: Tell wait-online to Only Care About Your Active NIC

```bash
sudo systemctl edit systemd-networkd-wait-online
```

Add:

```ini
[Service]
ExecStart=
ExecStart=/usr/lib/systemd/systemd-networkd-wait-online --interface=enx607d090ea316
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart systemd-networkd-wait-online
```

This tells it "I only care if the USB4 adapter is up" — the DOWN interfaces are ignored.

### Verify the Down Interfaces

```bash
# What are these interfaces?
networkctl list
ip link show enp1s0f1np1
ip link show enP2p1s0f0np0
ip link show enP2p1s0f1np1
```

These are probably unused SFP+ or the old onboard NIC. If nothing is ever plugged into them:

```bash
# Tell networkd to ignore unmanaged/down interfaces at boot
sudo tee /etc/systemd/network/99-ignore-down.network << 'EOF'
[Match]
Name=enp1s0f1np1 enP2p1s0f0np0 enP2p1s0f1np1

[Link]
Unmanaged=yes
EOF

sudo systemctl restart systemd-networkd
```

### Current System Health

Everything is now looking stable. The journal shows no crashes since the fan was added, just two cosmetic recurring entries. Your machine has been up since Apr 23 20:08 — over 13 hours.



## What's Actually Happening

This is a **mid-generation pipeline deadlock**, distinct from the previous request-start failure. Look at what's missing:

```
# Every healthy batch follows this pattern:
computeBatch: waiting for inputs to be ready   ← batchID=N
computeBatch: inputs are ready                 ← batchID=N  ✓
computeBatch: logits ready                     ← batchID=N  ✓
computeBatch: outputs are ready                ← batchID=N  ✓

# Batch 104959 breaks the chain:
computeBatch: waiting for inputs to be ready   ← batchID=104959
[GIN] 500 | 10m0s                              ← NEVER gets inputs_ready
```

Batch 104959 **hangs indefinitely** waiting for inputs — no logits, no outputs, no error — then hits the 10m hard timeout and 500s. Token generation was healthy up to that point (~30ms/token: discounts → based → on → purchases → during → the → **stall**).

## Root Cause: KV Cache Exhaustion Mid-Generation

The generation started successfully because there was `remaining=1` slot (from the earlier log). That last slot was consumed mid-stream. When the runner tried to allocate a KV cache entry for batch 104959's attention computation, **there was nothing left to allocate** — but instead of returning an error, the llama.cpp runner in Ollama silently blocks waiting for memory that never frees, until the HTTP timeout kills it.

The silent hang (no `ERROR` log at all before the 500) is the tell — a normal error would log immediately. Memory starvation causes the goroutine to block on a channel that never receives.

## Fix

Same root cause as before — `num_ctx` is too small for your actual generation length. The fix from the previous response applies, but now you can see it's not just at request-start, it's corrupting **in-flight responses** too. Priority order:

**1. Immediate — increase `num_ctx` via systemd:**

```bash
sudo systemctl edit ollama
```

```ini
[Service]
Environment="OLLAMA_NUM_CTX=65536"
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

**2. To confirm this is the cause before restarting**, check current effective context size:

```bash
ollama show qwen3-coder:30b --modelfile | grep num_ctx
# If blank, it's using the model default (likely 32768 or less)
```

**3. Monitor after the fix** to verify KV cache is no longer bottoming out:

```bash
journalctl -u ollama -f | grep "loading cache slot"
# Watch the "remaining=" value — should stay well above 0
```

The mid-generation 500 is actually worse UX than the request-start 500, since the client gets a partial stream that terminates with an error rather than a clean failure upfront.


## The Real Culprit: `NUM_PARALLEL=2` × `num_ctx=131072`

Your config creates a **combinatorial memory problem**, not a context size problem.

### The Math

With `OLLAMA_NUM_PARALLEL=2`, Ollama pre-allocates KV cache for **both slots simultaneously**. For qwen3-coder:30b with `q8_0` KV cache:

```
qwen3-coder:30b architecture:
  ~64 layers, 8 KV heads (GQA), head_dim=128

KV cache per token per slot:
  2 (K+V) × 8 heads × 128 dim × 1 byte (q8_0) × 64 layers = 131,072 bytes/token

Per slot at num_ctx=131072:
  131,072 tokens × 128 KB/token = ~16 GB

2 parallel slots:  ~32 GB KV cache
Model weights (Q4): ~18 GB
─────────────────────────────
Total:             ~50 GB per loaded model
```

You have `OLLAMA_MAX_LOADED_MODELS=2`, so worst case: **~100 GB**, right against your `MemoryMax=130G` wall. With `MemorySwapMax=0` there's zero relief valve — the kernel just hard-blocks the allocation.

### Why It Fails Mid-Stream

The trace shows batch 104959 stalls at `waiting for inputs to be ready` — the compute goroutine never signals back. This happens when a **second concurrent request** arrives and tries to allocate its KV cache slot while generation is in flight. With memory nearly exhausted, the allocator blocks waiting for memory that won't come, deadlocking the batch pipeline for the first request too.

### Diagnose First

Check if two requests are actually overlapping when the 500 occurs:

```bash
journalctl -u ollama --since "22:01:00" --until "22:12:00" | grep "GIN\|completion request\|loading cache slot"
```

Check actual memory at peak:

```bash
# While load is happening
systemctl status ollama | grep Memory
# or
cat /sys/fs/cgroup/system.slice/ollama.service/memory.current
```

---

### Fixes (pick based on your use case)

**Option A — Single user / agentic workload (recommended)**

```ini
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
```

Eliminates contention entirely. DGX Spark's 128GB is most efficiently used as one large uncontested pool.

**Option B — Keep parallelism, reduce context per slot**

```ini
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_NUM_CTX=65536"
```

Halves KV cache to ~16 GB total, leaving much more headroom.

**Option C — Keep parallelism, switch KV cache to q4_0**

```ini
Environment="OLLAMA_KV_CACHE_TYPE=q4_0"   # was q8_0
```

Halves KV cache memory with minor quality impact (~16 GB → ~8 GB for both slots).

**Option D — Drop to q4_0 KV + limit memory explicitly**

Ollama doesn't expose `--max-vram` directly, but you can cap KV cache budget in the Modelfile:

```
PARAMETER num_ctx 131072
PARAMETER num_keep 4096
```

`num_keep` preserves the system prompt in cache and forces older tokens to be evicted rather than blocking.

---

### Bottom Line

Your setup is right at the memory edge. The `MemoryMax=130G` + `MemorySwapMax=0` + two loaded models + two parallel 131K contexts is an allocation time bomb. Start with `OLLAMA_NUM_PARALLEL=1` — if the 500s disappear, that's your confirmation.



## New Finding: The Problem Is In Your Data Pipeline

Look at the failing prompt at 22:11:56 — `prompt=41837` tokens. Scroll through the Irish Sugar context:

```
| 1994-05-01 | spring 1994 | Irish Sugar offered target-based discounts...
| 1994-05-01 | spring 1994 | Irish Sugar granted volume-related discounts...
| 1994-05-01 | spring 1994 | Irish Sugar offered target-based discounts...   ← duplicate
| 1994-05-01 | spring 1994 | Irish Sugar granted volume-related discounts... ← duplicate
| 1994-05-01 | spring 1994 | Irish Sugar offered target-based discounts...   ← duplicate
... × ~20 more times
```

**Your timeline extraction/deduplication is broken.** The same 3-4 spring 1994 events are being appended in a loop. A clean version of this prompt would probably be ~8-10K tokens, not 41K. This is burning ~30K tokens of KV cache on garbage repetition.

---

## Why This Causes the Hang

The model starts generating into a near-full context (41K prompt + ~20K tokens of generation over 10 minutes ≈ 61K used). At some point mid-generation the **model itself enters a repetition loop** mirroring the repetitive input — generating the same discount descriptions over and over — which exhausts the remaining context window and causes the same KV cache stall you saw before, just delayed by 10 minutes of generation time.

---

## Two Problems to Fix

### 1. Upstream deduplication bug (root cause)

Your pipeline is emitting duplicate timeline rows. Find where timeline entries are being appended and add deduplication:

```python
# wherever you build timeline rows
seen = set()
deduped_rows = []
for row in timeline_rows:
    key = (row['standardized_date'], row['event_description'][:80])
    if key not in seen:
        seen.add(key)
        deduped_rows.append(row)
```

This alone will likely eliminate the 500s for large cases.

### 2. Add a prompt token guard before sending to Ollama

```python
import tiktoken  # or use ollama's tokenizer estimate

MAX_PROMPT_TOKENS = 80000  # leave headroom for generation

if estimated_tokens > MAX_PROMPT_TOKENS:
    # log warning, truncate timeline, alert
    raise ValueError(f"Prompt too large: {estimated_tokens} tokens for case {case_id}")
```

Fail fast loudly rather than letting Ollama hang for 10 minutes.

---

## The Ollama-Side Mitigation

Since you can't always control what the pipeline sends, add a hard timeout shorter than 10m so you get fast retries rather than blocked workers:

```python
# in your httpx/requests client
client = httpx.Client(timeout=httpx.Timeout(connect=10, read=180, write=10))
```

180 seconds is generous for a 41K prompt but will catch runaway generations well before Ollama's 10m hard timeout kills it.



With 190 GB of physical memory, you can comfortably set `OLLAMA_NUM_CTX=131072` (128k), but jumping to the full 262,144 (256k) will exceed your current RAM capacity. [1]

## Memory Breakdown for Qwen3-Coder:30B

Memory usage for large context is dominated by the KV Cache, which grows linearly with the context length.

- 131,072 Context (~175 GB total): This fits within your 190 GB limit. The model weights at 4-bit quantization take ~15-18 GB, while the KV cache for 128k context requires roughly 160 GB.
- 262,144 Context (~335 GB total): This is impossible on your current hardware. Doubling the context to 256k would require roughly 320 GB just for the KV cache, far exceeding your 190 GB.

## Recommendation

1. Stick to 131,072: This utilizes nearly all your available memory (~175 GB for the model/context + ~6 GB for embeddings = ~181 GB), leaving a small buffer for your OS.
2. Enable Flash Attention: Ensure you set `OLLAMA_FLASH_ATTENTION=1` in your environment. This is critical for keeping memory usage manageable at these lengths.
3. Check for "Spillover": If you notice extremely slow responses, Ollama is likely swapping to disk because it ran out of physical RAM. If this happens, lower the context to 98,304 (96k) for stability. [2, 3, 4, 5]

Note on Quantization: If you are using a higher precision model (like Q8 or FP16), your available context will be even smaller because the model weights themselves will take up 30-60 GB instead of ~15 GB.

Would you like the specific terminal commands to set these environment variables permanently on your operating system?

```python
def estimate_vram(model_params_b, bits_per_weight, ctx_size, hidden_size, num_layers):
    # Model weights
    model_gb = (model_params_b * bits_per_weight) / 8
    
    # KV Cache (approximate formula: 2 * layers * hidden * ctx * 2 bytes for FP16/BF16)
    # Using Qwen2.5/3 32B/30B typical hidden size ~5120
    kv_cache_gb = (2 * num_layers * hidden_size * ctx_size * 2) / (1024**3)
    
    return model_gb, kv_cache_gb

# Qwen2.5/3 30B/32B typical specs
layers = 64
hidden = 5120

# 131k context
m1, kv1 = estimate_vram(30, 4, 131072, layers, hidden)
# 262k context
m2, kv2 = estimate_vram(30, 4, 262144, layers, hidden)

print(f"131k: Weights {m1:.1f}GB + KV {kv1:.1f}GB = {m1+kv1:.1f}GB")
print(f"262k: Weights {m2:.1f}GB + KV {kv2:.1f}GB = {m2+kv2:.1f}GB")
```

  

[1] [https://www.youtube.com](https://www.youtube.com/watch?v=ZJPUxApp-U0)

[2] [https://www.reddit.com](https://www.reddit.com/r/ollama/comments/1jbifhz/what_happens_if_context_length_is_set_larger_than/)

[3] [https://github.com](https://github.com/ollama/ollama/issues/1005)

[4] [https://community.frame.work](https://community.frame.work/t/quickstart-guide-ollama-with-gpu-support-no-rocm-needed/79186)

[5] [https://github.com](https://github.com/ollama/ollama/issues/14073)

# Environment="OLLAMA_FLASH_ATTENTION=1" ?
	
## Short Answer: No, Not for This Problem

`OLLAMA_FLASH_ATTENTION=1` optimizes **how attention is computed**, not how much KV cache is consumed. It tiles the QK᷊V computation to avoid materializing the full N×N attention matrix in VRAM, which helps with:

- Throughput on long sequences (faster tokens/sec)
- Peak activation memory during the forward pass
- Numerical stability at long contexts

**It does not reduce KV cache size.** The cache stores K and V tensors for every past token regardless of how attention is computed — Flash Attention still reads from the same cache, just more efficiently.

So for your situation:

```
41K token prompt (with 20K duplicate garbage)
→ 41K KV cache slots consumed regardless of flash attention
→ context exhaustion mid-generation unchanged
```

---

## What It Would Actually Give You

It's still worth enabling on a DGX Spark — qwen3-coder:30b at 131K context will see meaningful throughput gains:

```ini
Environment="OLLAMA_FLASH_ATTENTION=1"
```

You'd get maybe 10-20% faster decode on long sequences, which means the 10-minute hang becomes an 8-minute hang. Not your fix.

---

**Your fix is still the deduplication bug.** The `spring 1994` rows repeating ~20 times is what's killing you — that's the only thing that will prevent the KV cache exhaustion on large cases.

# STATUS
2026-04-24 22:22:43,586 - INFO - pipelines.agent_dspy.universal_fact_extraction_pipeline - Skipping pipeline for CELEX-31997D0123: Fact exists.
 47%|█████████████████████████████████████████████████████                                                            | 349/744 [8:26:01<3:47:42, 34.59s/it]2026-04-24 22:22:43,610 - INFO - batch.BatchJobRunner - Processing CELEX ID: CELEX-31998D0273
2026-04-24 22:22:43,612 - INFO - pipelines.agent_dspy.universal_fact_extraction_pipeline - No condition defined .. processing