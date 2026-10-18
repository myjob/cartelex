import os
from pathlib import Path
import logging
import logging.config
import yaml

# Project root, three levels up from src/config/settings.py. Defined at module
# level because the .env below must load before the Config class body reads
# os.environ.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_MAIN_DIR = PROJECT_ROOT / "config_main"
ENV_FILE = CONFIG_MAIN_DIR / ".env"

# Load the env file in-process, so every entry point picks it up the same way:
# terminal `python -m unittest`, the VS Code test runner and debugger, cron,
# and systemd units alike. Relying on the shell (~/.bashrc) or on VS Code's
# python.envFile would cover only some of those. Existing process env wins --
# load_dotenv does not override -- so an exported variable silently beats the
# file; keep one owner per variable.
# Not fatal if python-dotenv is missing: fall back to the ambient environment.
try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE)
except ImportError:
    pass


class Config:
    # --- Database Configuration ---
    DATABASE_URL = os.environ["DATABASE_URL"]

    # DB Schema settings
    DB_SCHEMA = os.getenv("DB_SCHEMA", "celex_facts")
    DB_SEARCH_PATH = os.getenv("DB_SEARCH_PATH", "celex_facts,public")
    COLLECTION_NAME = os.getenv("COLLECTION_NAME", "celex_doc_chunks")

    # --- Case-identifier registry ---
    # Authoritative CSV-loaded celex_id -> case-number mapping.  Read once per
    # process to build the in-memory identifier index that gates the retrieval
    # pipeline (see tools/celex_checker.py).
    CELEX_CASE_ID_TABLE = os.getenv("CELEX_CASE_ID_TABLE", "celex_case_id")

    # Models

    # --------------------------------------------------------------------------
    # The main embedding model for vector store
    # EMBEDDING_MODEL_qwen3 = os.getenv("EMBEDDING_MODEL_qwen3", "qwen3-embedding:8b")
    EMBEDDING_MODEL_qwen3 = os.getenv(
        "EMBEDDING_MODEL_qwen3", "qwen3-embedding:8b-local"
    )
    OLLAMA_EMBEDDING_QWEN3_TOKEN_LIMIT = os.getenv(
        "OLLAMA_EMBEDDING_QWEN3_TOKEN_LIMIT", "2048"
    )

    # --- Embedding Table ---
    # Qwen3 is the sole embedding provider (the OpenAI paths and the
    # EMBEDDING_PROVIDER toggle were pruned in Aug 2026), so only the table
    # name stays configurable.
    EMBEDDING_TABLE = os.getenv("EMBEDDING_TABLE", "pg_embedding_qwen")

    # --------------------------------------------------------------------------
    # The default LLM for general tasks currently used by UI  backend in oi_dspy and the modulecorp index searcher
    # LLM_MODEL_ID should goLLM_MODEL_ID_qwen3, context = 262144
    LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "qwen3-coder:30b")
    ## LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "qwen3.8:27b")
    
    # Decoding temperature for fact extraction. Default 0.0 (greedy) so batch
    # ingestion is reproducible: at the provider default the same document can
    # flip between extracting an entity and returning NA, or vary how many
    # entities it emits. Extraction is a faithfulness task, not a creative one.
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.0"))
    # Thinking (chain-of-thought) mode for every LLM call, declared in the
    # canonical form {"thinking": {"type": LLM_THINKING_MODE}} and translated
    # per provider by config.llm_thinking.thinking_kwargs. Default "disabled":
    # extraction and classification are faithfulness tasks, the trace is
    # discarded, and on the local Ollama server a one-word answer costs 64+
    # reasoning tokens before any content is emitted. Set "enabled" per run to
    # let the model think.
    LLM_THINKING_MODE = os.getenv("LLM_THINKING_MODE", "disabled")  # enabled|disabled
    LLM_MODEL_CLOUD_ID = os.getenv("LLM_MODEL_CLOUD_ID", "deepseek-chat")
    # LLM_MODEL_CLOUD_ID = os.getenv("LLM_MODEL_CLOUD_ID", "deepseek-v4-flash")
    # LLM_MODEL_CLOUD_ID = os.getenv("LLM_MODEL_CLOUD_ID", "deepseek-v4-pro")
    LLM_MODEL_CLOUD_API_KEY = os.environ["DEEPSEEK_API_KEY"]

    # --- OpenCorporates ---
    # Fallback company-identifier lookup (https://api.opencorporates.com/v0.4).
    # Token read from env var ``opencorporates_api_token`` (exact lowercase
    # name); hard-fail if absent, like DATABASE_URL. Auth is sent as the
    # X-API-TOKEN request header, never in the URL.
    OPENCORPORATES_API_TOKEN = os.environ["opencorporates_api_token"]
    OPENCORP_BASE_URL = os.getenv(
        "OPENCORP_BASE_URL", "https://api.opencorporates.com/v0.4"
    )
    OPENCORP_TIMEOUT_SECONDS = float(os.getenv("OPENCORP_TIMEOUT_SECONDS", "15"))
    OPENCORP_MAX_RETRIES = int(os.getenv("OPENCORP_MAX_RETRIES", "1"))
    OPENCORP_RETRY_BACKOFF_SECONDS = float(
        os.getenv("OPENCORP_RETRY_BACKOFF_SECONDS", "5.0")
    )
    # Backoff for the HTML "this IP address may be accessing OpenCorporates at a
    # higher than expected rate" page, which is served as HTTP 403. It is an
    # edge throttle rather than a per-request rate limit, so it needs a longer
    # pause than the generic retry backoff above.
    OPENCORP_THROTTLE_BACKOFF_SECONDS = float(
        os.getenv("OPENCORP_THROTTLE_BACKOFF_SECONDS", "60.0")
    )
    # Fail-fast switch for long roster runs. When true, no request is retried:
    # the first failure is raised straight to the caller, which logs it, records
    # it against the company, and moves to the next name instead of ending the
    # run on an error streak. Motivated by names the API rejects outright with
    # HTTP 400 -- retrying and then stopping the whole batch spends minutes of
    # backoff on a failure that will never succeed for that name. An IP throttle
    # and a rejected token still stop the run: neither is fixable by moving on.
    OPENCORP_SKIP_RETRY = (
        os.getenv("OPENCORP_SKIP_RETRY", "true").lower() == "true"
    )
    OPENCORP_PER_PAGE = int(os.getenv("OPENCORP_PER_PAGE", "30"))

    # --- Resolution ladder -------------------------------------------------
    # A zero-hit search is closed permanently as fk_opencorp_idx = 'NA', so a
    # name shape or a wrong jurisdiction becomes indistinguishable from a
    # company genuinely absent from every registry. The ladder retries a target
    # under progressively relaxed queries and scores the hits instead of
    # linking them blind. See docs/claude/plans/opencorp-zero-hit-relaxation-ladder.md.
    #
    # Largest result set still worth linking from a jurisdiction-filtered rung.
    # A broader set is not evidence about this company.
    OPENCORP_HIT_CAP_JURISDICTION = int(
        os.getenv("OPENCORP_HIT_CAP_JURISDICTION", "200")
    )
    # Same, for a rung that drops the jurisdiction filter. Much tighter: an
    # unfiltered search is global, and the cap doubles as the fetch budget --
    # 'Apple Inc.' returns 12,821 hits over 428 pages and is abandoned after one
    # call, while 'Halkis' (29 hits) completes in that same single call.
    OPENCORP_HIT_CAP_UNFILTERED = int(
        os.getenv("OPENCORP_HIT_CAP_UNFILTERED", "50")
    )
    # Ceiling on links written for one roster row. Without it a generic name
    # links thousands: 'La Poste' alone holds 10,395 rows in the link table.
    OPENCORP_MAX_LINKS_PER_TARGET = int(
        os.getenv("OPENCORP_MAX_LINKS_PER_TARGET", "5")
    )
    # Minimum score to accept a hit on a scored rung. The default admits only an
    # exact folded name or a whole-word folded prefix, because scored rungs are
    # the ones that searched outside the target's own jurisdiction: 'Halkis'
    # must still reach 'HALKIS CEMENT TRANSPORTERS CO. LIMITED' (prefix) while
    # 'GESTION HALKIS INC.' (mere containment) is rejected.
    OPENCORP_MIN_MATCH_SCORE = float(os.getenv("OPENCORP_MIN_MATCH_SCORE", "0.8"))
    # Shortest relaxed query worth sending. Guards against a rung stripping a
    # name down to an initialism: 'RS', 'PC', 'TA' and 'GFA' produce both the
    # worst link fan-out and the unexplained HTTP 400s.
    OPENCORP_MIN_RELAXED_QUERY_LENGTH = int(
        os.getenv("OPENCORP_MIN_RELAXED_QUERY_LENGTH", "5")
    )
    # Same, for a run started with --relax. Lowered because the default silences
    # whole names rather than mere initialism fragments: 'STET' is four
    # characters and produces no rung at all, yet searched unfiltered it is a
    # real query. Three keeps 'RS' and 'PC' out while letting a short but
    # complete name through.
    OPENCORP_RELAXED_MIN_QUERY_LENGTH = int(
        os.getenv("OPENCORP_RELAXED_MIN_QUERY_LENGTH", "3")
    )

    # --- LLM profiles -------------------------------------------------------
    # Two consumers pick their own model independently: the oi_dspy UI planner
    # and the corp index searcher. They used to share LLM_MODEL_CLOUD_ID, so
    # switching the batch to a cheaper model silently re-pointed the UI as well
    # (and vice versa). Each profile is a (provider, model id) pair resolved by
    # config.llm_factory; the provider supplies the endpoint and credential, so
    # neither is repeated here.
    #
    # A blank model id means "this provider's shared default" --
    # LLM_MODEL_ID for ollama, LLM_MODEL_CLOUD_ID for deepseek -- which is what
    # keeps an unset profile behaving exactly as it did before it existed.

    # A profile also carries its own thinking mode and its own LM cache switch,
    # for the same reason it carries its own model: the two consumers want
    # opposite answers. A blank thinking mode follows the global
    # LLM_THINKING_MODE.

    # UI planner (oi_dspy backend server).
    UI_LLM_PROVIDER = os.getenv("UI_LLM_PROVIDER", "ollama")  # "ollama" | "deepseek"
    UI_LLM_MODEL_ID = os.getenv("UI_LLM_MODEL_ID", "")
    UI_LLM_THINKING_MODE = os.getenv("UI_LLM_THINKING_MODE", "")  # blank | enabled | disabled
    UI_LLM_CACHE = os.getenv("UI_LLM_CACHE", "true").lower() == "true"

    # Corp index searcher (Main_LLM_corp_index_searcher.py and the DSPy modules
    # in pipelines.corp_index). Defaults to the cloud provider: this stage runs
    # where every register lookup already failed, so it asks a model with broad
    # world knowledge rather than the local coding model.
    CORP_INDEX_LLM_PROVIDER = os.getenv("CORP_INDEX_LLM_PROVIDER", "deepseek")
    # A reasoning model by default, unlike every other stage. This one is
    # recall-bound rather than faithfulness-bound: it asks a model to remember a
    # decades-old undertaking that no register holds, and a single-shot answer
    # returns 'NA' for facts the same model produces once it may think first.
    CORP_INDEX_LLM_MODEL_ID = os.getenv("CORP_INDEX_LLM_MODEL_ID", "deepseek-reasoner")
    CORP_INDEX_LLM_THINKING_MODE = os.getenv("CORP_INDEX_LLM_THINKING_MODE", "enabled")
    # DSPy's LM cache is off for this profile. The cache is keyed by the request,
    # so a --refresh pass over unchanged prompts replays the stored answer
    # instead of re-asking, and the batch reports as renewed a set of rows it
    # never actually renewed. Set true to make a repeated question free again.
    CORP_INDEX_LLM_CACHE = os.getenv("CORP_INDEX_LLM_CACHE", "false").lower() == "true"

    # Offline batches (fact ingestion, section classifier, chunk-context
    # enrichment, evidence verifier, knowledge ingestion, book cross-check).
    # They share one profile because they share one purpose -- long unattended
    # runs against the local server -- and because every one of them used to
    # hand-roll the same dspy.LM block. The provider switch lets a batch move to
    # the cloud model without touching six scripts. Cache is always off: a batch
    # re-run that replays stored answers reports work it never did.
    BATCH_LLM_PROVIDER = os.getenv("BATCH_LLM_PROVIDER", "ollama")  # "ollama" | "deepseek"
    BATCH_LLM_MODEL_ID = os.getenv("BATCH_LLM_MODEL_ID", "")
    BATCH_LLM_THINKING_MODE = os.getenv("BATCH_LLM_THINKING_MODE", "")  # blank | enabled | disabled

    ### LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "qwen3-coder-next:q8_0") --> Avoid: Models specifically designated as "Reasoning" or with "-next" in the name if you want to avoid the explicit step-by-step thinking phase
    # Specialized models if needed (can default to LLM_MODEL_ID)
    #!! todo: harmonize with above
    AGENT_MODEL_ID = os.getenv("AGENT_MODEL_ID", "qwen3-coder:30b")
    EXTRACTION_MODEL_ID = os.getenv("EXTRACTION_MODEL_ID", "qwen3-coder:30b")
    # --- Ollama / LLM Configuration ---
    OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

    # Record of the server's declared context window (the Ollama unit's
    # OLLAMA_CONTEXT_LENGTH), kept so the client side documents what it is
    # talking to. Documentation only: no code reads it, and nothing here can
    # change the server. Prefill cost scales with the prompt actually sent, not
    # with the window declared -- measured on the DGX Spark, a 7.4k prompt
    # prefills in 2s and a 15.8k one in 13s regardless of whether the window is
    # 32768 or 262144 -- so a wide window costs throughput nothing and buys
    # ~24 GB of reservation. The guard against a runaway prompt is
    # OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT below, on this side of the wire.
    # See docs/infra/Performance of LLM with MTP.md.
    OLLAMA_LLM_INFERENCE_TOKEN_LIMIT = int(
        os.getenv("OLLAMA_LLM_INFERENCE_TOKEN_LIMIT", "262144")
    )

    # Seconds to pause between per-chunk LLM calls in batch backfills so the
    # Ollama server (e.g. on an Nvidia DGX Spark) can cool down and avoid a
    # thermal shutdown. Set to 0 to disable the grace pause.
    THERMAL_GRACE_DURATION = float(os.getenv("THERMAL_GRACE_DURATION", "1"))

    # --- DGX Spark thermal health gate ---
    # The blind pause above is not a guard: it sleeps whether the box is at 40 C
    # or 84 C. These knobs drive config.ollama_health_gate, which measures the
    # server before each unit of batch work and stops the run gracefully rather
    # than letting the Spark cross its shutdown threshold. That threshold is the
    # reason the gate exists at all: above roughly 85 C the unit dies with no
    # dump, no log and no response, because its Grace-Blackwell memory is
    # unified with the CPU's and nothing survives to be written out.
    THERMAL_GATE_ENABLED = (
        os.getenv("THERMAL_GATE_ENABLED", "true").lower() == "true"
    )

    # Ceiling applied to the gate temperature, which is the maximum of the GPU
    # die sensor and every ACPI thermal zone. Taking the maximum is what makes
    # this a single number with an "either sensor" meaning: the gate trips when
    # the GPU reaches the limit OR when any SoC zone does, whichever gets there
    # first. That matters because the two diverge on the GB10, always in the
    # dangerous direction -- measured on spark-niki at 88 percent utilisation,
    # temperature.gpu read 75 C while thermal_zone0 read 83.3 C. A limit applied
    # to temperature.gpu alone would not have fired there.
    #
    # 91 is an operator decision, set against the SoC firmware cutout at 93-96 C
    # rather than against the GPU's own ~105 C throttle, and above the measured
    # 81.9-83.3 C loaded working band so ordinary extraction does not throttle.
    # It leaves 2-5 C of margin, which is less than one THERMAL_COOLDOWN_SECONDS
    # of warning: the cap on boost clocks below is what buys back the headroom,
    # so keep THERMAL_CLOCK_CONTROL_ENABLED on at this limit.
    THERMAL_MAX_TEMP_C = float(os.getenv("THERMAL_MAX_TEMP_C", "91.0"))

    # How long to stand down after a breach before re-measuring. Sized from the
    # observed cooling curve of the unit under batch load rather than from a
    # round number: two to three minutes of idle is what brings it back from the
    # low 80s to the low 70s.
    THERMAL_COOLDOWN_SECONDS = float(os.getenv("THERMAL_COOLDOWN_SECONDS", "150.0"))

    # Cooldowns to serve before giving up and aborting the run. At the default
    # of 5 that is about 12 minutes of waiting; a box still too hot after that
    # is not going to recover on its own, and continuing to poll only delays a
    # stop that has to happen anyway.
    THERMAL_MAX_COOLDOWN_CYCLES = int(os.getenv("THERMAL_MAX_COOLDOWN_CYCLES", "5"))

    # Lifetime of a reading before the gate re-probes. One SSH round trip to the
    # Spark costs ~0.45s measured, so probing per CELEX would add minutes of
    # pure latency to a 744-document run, and with --max-workers above 1 every
    # worker would open its own session. At 30s the whole pool shares one probe
    # per window, which is far shorter than the minutes a thermal excursion
    # takes to develop.
    THERMAL_READING_TTL_SECONDS = float(
        os.getenv("THERMAL_READING_TTL_SECONDS", "30.0")
    )

    # What to do when the server answers but its temperature cannot be read
    # (SSH refused, probe not deployed, garbled JSON). Default true: an
    # unreadable temperature counts as a breach. That is the deliberate choice,
    # because a failed SSH to the very box serving Ollama is itself the
    # documented network-drop symptom (docs/NVIDIA connection drops (CLAUDE).md)
    # and a silent shutdown costs more than a stalled batch. Set false to fail
    # open while the probe has not been deployed yet.
    THERMAL_GATE_STRICT = os.getenv("THERMAL_GATE_STRICT", "true").lower() == "true"

    # Audible alert on a breach. Empty disables sound and leaves the terminal
    # bell and the log banner. dialog-warning.oga is chosen for length: it runs
    # 0.5s, so it can be repeated into an unmistakable pattern, where
    # alarm-clock-elapsed.oga runs 6.1s and three of those would block a batch
    # for nearly twenty seconds.
    THERMAL_ALERT_SOUND_CMD = os.getenv(
        "THERMAL_ALERT_SOUND_CMD",
        "paplay /usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
    )

    # How many times the alert sound is played, back to back. More than one
    # because a single short chime is easy to miss or mistake for another
    # application. The plays are awaited rather than detached: a fire-and-forget
    # subprocess is killed when the batch exits, which is exactly the moment a
    # thermal abort happens, so the operator heard nothing at all.
    THERMAL_ALERT_SOUND_REPEAT = int(os.getenv("THERMAL_ALERT_SOUND_REPEAT", "5"))

    # Per-play deadline for the sound command, so a wedged audio server costs a
    # few seconds rather than the run.
    THERMAL_ALERT_SOUND_TIMEOUT = float(
        os.getenv("THERMAL_ALERT_SOUND_TIMEOUT", "10.0")
    )

    # --- GPU clock capping ---
    # The Spark does not die of GPU die temperature. Under sustained inference
    # the ARM cores and ACPI zones spike toward 93-96C and the board firmware
    # executes a hard power-off, while the GPU die trails at 78-84C, far below
    # its own ~105C throttle. The chassis intake is restrictive and the
    # embedded-controller fan curve is conservative, so the firmware reaches its
    # cutout before the GPU ever asks for help. Capping the boost clock removes
    # the transient power spikes that heat the shared package faster than the
    # fans respond.
    #
    # Requires passwordless sudo for nvidia-smi on the Spark; without it the
    # controller logs one actionable warning and the gate still protects the box
    # by throttling and stopping. See docs/infra/INSTALL celex-facts.md.
    THERMAL_CLOCK_CONTROL_ENABLED = (
        os.getenv("THERMAL_CLOCK_CONTROL_ENABLED", "true").lower() == "true"
    )

    # Bounds handed to `nvidia-smi -lgc <min>,<max>` while the box is hot. The
    # measured GB10 idles at 2405 MHz against a 3003 MHz maximum, so 2200 is a
    # real cut to the boost ceiling rather than a no-op, and 300 keeps the floor
    # low enough that the governor can still drop the clock when idle.
    # Root-owned wrapper on the Spark that is the entire privileged surface:
    # it accepts "reset" or "cap <min> <max>", validates both numbers, and calls
    # nvidia-smi itself. The sudoers rule names this path and nothing else.
    # Granting nvidia-smi directly would need a wildcard for the clock argument,
    # and sudo matches arguments as one concatenated string -- "nvidia-smi -lgc *"
    # would also permit "-lgc 300,2200 -i 0 -r", a GPU reset.
    # Installed by scripts/install_dgx_clock_guard.sh.
    DGX_CLOCK_GUARD_PATH = os.getenv(
        "DGX_CLOCK_GUARD_PATH", "/usr/local/sbin/gpu-clock-guard"
    )

    THERMAL_CLOCK_MIN_MHZ = int(os.getenv("THERMAL_CLOCK_MIN_MHZ", "300"))
    THERMAL_CLOCK_CAP_MHZ = int(os.getenv("THERMAL_CLOCK_CAP_MHZ", "2200"))

    # Temperature at or below which the boost cap is lifted again. Derived from
    # the limit rather than fixed, so the hysteresis band tracks it: capping and
    # restoring at the same temperature would flap the clock on every reading
    # that straddles the limit, and each flap is a privileged SSH command.
    #
    # A fixed value silently changes meaning when the limit moves. At a limit of
    # 91 an absolute 70 sits below the 81.9-83.3 C loaded working band, so the
    # first breach would latch the cap on for the rest of a busy run; 6 C below
    # the limit lifts it as soon as the box is genuinely back in normal
    # operation. Override to pin an absolute value.
    THERMAL_CLOCK_RESTORE_TEMP_C = float(
        os.getenv("THERMAL_CLOCK_RESTORE_TEMP_C", str(THERMAL_MAX_TEMP_C - 6.0))
    )

    # Deadline for the Ollama liveness probe. This hits /api/version, which
    # returns in milliseconds from a healthy server; the value only bounds how
    # long an unreachable one stalls the gate.
    OLLAMA_HEALTH_PING_TIMEOUT = float(os.getenv("OLLAMA_HEALTH_PING_TIMEOUT", "5.0"))

    # SSH identity for the temperature probe. There is deliberately no
    # DGX_SSH_HOST: the host is derived from OLLAMA_HOST, so the machine being
    # measured is by construction the machine serving inference and the two can
    # never drift apart (this box has already moved once, from .152 to .130).
    DGX_SSH_USER = os.getenv("DGX_SSH_USER", "job")

    # Where scripts/deploy_dgx_thermal_probe.sh puts config.dgx_thermal_probe on
    # the Spark. Under the SSH user's home, so deployment needs no privileges.
    DGX_SSH_PROBE_PATH = os.getenv("DGX_SSH_PROBE_PATH", "~/bin/dgx_thermal_probe.py")

    # Connect-plus-run deadline for that SSH call. A healthy round trip is
    # ~0.45s measured; 15 tolerates a congested link without letting a dead one
    # hold up the batch.
    DGX_SSH_TIMEOUT_SECONDS = int(os.getenv("DGX_SSH_TIMEOUT_SECONDS", "15"))

    # Client-side circuit breaker on the assembled context, checked with
    # tiktoken before a request is sent (see
    # pipelines.agent_dspy.universal_modules._check_context_token_limit). It is
    # not a share of the context window any more: at the old 173500 it sat deep
    # inside the region where prefill turns quadratic and could not trip before
    # the damage was done. A runaway 233k-token prompt costs 332s of prefill
    # alone on the Spark -- the six-minute failure this project has already seen
    # twice -- while 40000 is caught in milliseconds.
    #
    # 40000 is above every legitimate context and below the ~65k knee where
    # measured prefill leaves its ~1200 tok/s plateau. Corpus maxima over 744
    # documents: hybrid retrieval (the 50 largest chunks of a document, at
    # HYBRID_SEARCH_TOP_K=50) tops out at 24987 tokens, and FactSummaryModule
    # (every fact of one CELEX concatenated) at ~24800. The other guarded paths
    # are far smaller -- full-document context is capped by
    # SMALL_DOC_CONTEXT_TOKEN_LIMIT, FactDetailRetriever by
    # DETAIL_RETRIEVER_TOP_K, and the enumeration aggregate step passes entity
    # names rather than chunks. Anything that trips this is a bug.
    #
    # The check covers the context, not the whole prompt: signature
    # instructions plus schema add well under 1k, so the effective prompt
    # ceiling is about 41000.
    OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT = int(
        os.getenv("OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT", "40000")
    )

    # Per-HTTP-request deadline handed to LiteLLM by
    # config.llm_factory.lm_runtime_kwargs -- not a per-fact or per-document
    # budget. A module that issues one call per chunk (EnumerationFactModule
    # runs the map step over every candidate chunk) legitimately spans many
    # such deadlines in one fact.
    #
    # Sized from the measured curve rather than guessed: at the 40000-token cap
    # above, prefill is ~35s (~1180 tok/s), and decoding a full LLM_MAX_TOKENS
    # of 8192 at the measured 79 tok/s of qwen3-coder:30b is ~104s, so the
    # worst legitimate request lands near 139s. 150 covers it and still fails
    # 2.4x faster than the previous 360, which was itself the same six minutes
    # the runaway prefill produces.
    OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "150"))

    # Hard cap on generated tokens for every dspy.LM call (LiteLLM max_tokens ->
    # Ollama num_predict). Without it DSPy sends max_tokens=None, LiteLLM drops
    # the None, and Ollama decodes at num_predict=-1 until it fills the slot --
    # a greedy model that falls into a repetition loop then never returns and
    # the client times out instead. Sized from logs/pipeline_audit.jsonl over
    # 23395 successful extractions: p99 6273 characters, largest 15159
    # (case_summary, roughly 4-5k tokens), so this leaves about 2x headroom.
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8192"))

    # Attempts after the first for a failed LLM request, across both retry
    # layers. Default 0: one attempt. A retried timeout costs another full
    # OLLAMA_TIMEOUT and hides which request actually failed, so a run that
    # loses a document should lose it once. See config.llm_factory for why this
    # single knob has to disarm both layers.
    LLM_NUM_RETRIES = int(os.getenv("LLM_NUM_RETRIES", "0"))

    # Penalty applied to already-emitted tokens. Default 0.0 (no penalty) so
    # extraction output is bit-identical to the greedy baseline. Raise it (0.1
    # to 0.3) only when a run is confirmed to loop: it is the mitigation for a
    # repetition loop that keeps decoding deterministic, whereas raising
    # LLM_TEMPERATURE would trade reproducibility away for the same effect.
    LLM_FREQUENCY_PENALTY = float(os.getenv("LLM_FREQUENCY_PENALTY", "0.0"))

    # Per-token cost attributed to a locally served (Ollama) model, in USD.
    # LiteLLM prices only the models in its own cost map, and no local tag is in
    # it: completion_cost() raises "This model isn't mapped yet" for
    # openai/qwen3-coder:30b, so LiteLLM stores response_cost = None and a local
    # run carries no cost figure at all. config.llm_usage.register_local_model_costs
    # registers the served models with these rates, which makes the cost a
    # number rather than an absence.
    #
    # Default 0.0: local inference is free at the margin, the hardware being
    # already bought. Set a GPU-hour-amortized rate (dividing the run cost of the
    # DGX Spark by its measured throughput) to compare a local run against a
    # DeepSeek one in money rather than in tokens.
    LOCAL_LLM_INPUT_COST_PER_TOKEN = float(
        os.getenv("LOCAL_LLM_INPUT_COST_PER_TOKEN", "0.0")
    )
    LOCAL_LLM_OUTPUT_COST_PER_TOKEN = float(
        os.getenv("LOCAL_LLM_OUTPUT_COST_PER_TOKEN", "0.0")
    )

    # Seconds to wait for LiteLLM's accounting of in-flight requests before
    # reading a usage total. LiteLLM dispatches its success callback on its own
    # thread pool (litellm_core_utils/thread_pool_executor.py), and it lands
    # about a millisecond after the completion returns to the caller -- measured
    # against the DGX, +0.704s versus +0.705s. Reading the totals the instant a
    # CELEX finishes therefore loses its last request or two, which is a silent
    # undercount in the audit trail rather than a visible error.
    #
    # config.llm_usage counts each request synchronously when it is issued and
    # waits here for the matching callbacks. The wait is normally microseconds;
    # the timeout only bounds the damage if a callback never arrives at all.
    LLM_USAGE_DRAIN_TIMEOUT = float(os.getenv("LLM_USAGE_DRAIN_TIMEOUT", "10.0"))

    # When a CELEX document's total chunk tokens fit within this budget, the
    # universal pipeline skips hybrid retrieval and feeds the entire document to
    # the LLM. Short decisions (e.g. negative-clearance notifications) embed
    # poorly against loaded questions, so ranking can silently drop the very
    # chunks that name the parties; feeding the whole (small) document avoids
    # that failure mode. Kept well under
    # OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT so large documents still retrieve.
    SMALL_DOC_CONTEXT_TOKEN_LIMIT = int(
        os.getenv("SMALL_DOC_CONTEXT_TOKEN_LIMIT", "12000")
    )

    # --------------------------------------------------------------------------
    VECTOR_STORE_K = int(os.getenv("VECTOR_STORE_K", "40"))
    HYBRID_SEARCH_TOP_K = int(os.getenv("HYBRID_SEARCH_TOP_K", "50"))
    DOC_SEARCH_TOP_K = int(os.getenv("DOC_SEARCH_TOP_K", "40"))
    SEMANTIC_SEARCH_HITS = int(os.getenv("SEMANTIC_SEARCH_HITS", "40"))

    # Per-entity chunk budget of the detail_retriever pipeline
    # (FactDetailRetriever). Small on purpose: each entity gets its own
    # targeted search plus its own LLM extraction call, so the context per
    # call stays focused and cheap.
    DETAIL_RETRIEVER_TOP_K = int(os.getenv("DETAIL_RETRIEVER_TOP_K", "8"))

    # Hits pulled from the lexical (Postgres FTS) leg of HybridSearch before
    # Reciprocal Rank Fusion. Mirrors SEMANTIC_SEARCH_HITS for the dense leg.
    HYBRID_FTS_HITS = int(os.getenv("HYBRID_FTS_HITS", "40"))

    # Trailing chunks HybridSearch always feeds the reranker and guarantees in
    # its result (the "closing reserve"), so the structurally fixed tail of a
    # decision -- addressees, "Done at Brussels", "For the Commission /
    # <name> / Member of the Commission" -- cannot be ranked out by a question
    # whose vocabulary never appears in it. Measured over the corpus, the
    # signature block is the very last chunk in 273 of 342 documents that have
    # one; a reserve of 2 covers 82%, 3 covers 84%, and the remaining tail is
    # documents with annexes after the signature (better served by
    # section_scope). Set to 0 to disable the reserve entirely.
    HYBRID_CLOSING_RESERVE = int(os.getenv("HYBRID_CLOSING_RESERVE", "2"))

    # Reciprocal Rank Fusion damping constant. score = sum 1 / (RRF_K + rank).
    # 60 is the value from the original Cormack et al. RRF paper and the de
    # facto default; larger values flatten the contribution of top ranks.
    RRF_K = int(os.getenv("RRF_K", "60"))

    # Enumeration strategy — wider recall than Q&A because rare entities
    # (a court named once) only appear in chunks the dense leg ranks low.
    ENUMERATION_DENSE_HITS = int(os.getenv("ENUMERATION_DENSE_HITS", "120"))
    ENUMERATION_BM25_HITS = int(os.getenv("ENUMERATION_BM25_HITS", "120"))

    # Structural prior added by the rerankers when a call site passes
    # apply_position_boost=True (see extract/rag/ranker/position_boost.py):
    #   "section_type"  (default) — weight chunks by their classified decision
    #                   section (operative/fines over recitals); survives long
    #                   annexes, unlike raw depth.
    #   "legal_climax"  — the pre-Phase-3 doc_pos depth boost, kept for A/B
    #                   comparison until the section weights are validated.
    #   "none"          — cross-encoder score only.
    RANKER_BOOST_MODE = os.getenv("RANKER_BOOST_MODE", "section_type")

    # --- Reranker (cross-encoder) Configuration ---
    # Two consumers pick their reranker independently, the same way the LLM
    # profiles above split the UI planner from the corp index searcher. Each
    # profile is a (backend, model id) pair resolved by
    # extract.rag.ranker.ranker_factory:
    #   * batch profile -- RERANKER_BACKEND / RERANKER_MODEL_ID: the ingestion
    #     CLIs (Main_ingest_generic_facts, Main_cross_check_facts_vs_book,
    #     search_cli) and every `ranker or create_ranker()` injection fallback.
    #   * ui profile -- UI_RERANKER_BACKEND / UI_RERANKER_MODEL_ID: the oi_dspy
    #     backend server only, where FlashRank's small ONNX model answers a chat
    #     question fast on CPU and leaves the GPU to a concurrent batch run.
    # "bge" uses a sentence-transformers CrossEncoder (the model id) -- a true
    # multilingual cross-encoder with 8k context, a strict upgrade for legal
    # EN/DE decision text; "flashrank" is the in-process FlashRank
    # ms-marco-MiniLM model, which pins its own bundled weights and ignores the
    # model id. The bge backend is model-agnostic: point the model id at any
    # CrossEncoder-compatible reranker (e.g. a Qwen3-Reranker size served on a
    # GPU box) without code changes.
    RERANKER_BACKEND = os.getenv("RERANKER_BACKEND", "bge")  # "flashrank" | "bge"
    # A Hub repo id ("BAAI/bge-reranker-v2-m3") or an absolute path to a
    # directory holding the downloaded weights ("/opt/models/bge-reranker-v2-m3",
    # produced by `hf download <repo> --local-dir <dir>`). A directory path
    # bypasses the Hub entirely — no resolve calls, no anonymous-request
    # warning, no cold-start stall when huggingface.co is slow or unreachable —
    # and BgeReranker infers local_files_only from it automatically.
    RERANKER_MODEL_ID = os.getenv("RERANKER_MODEL_ID", "BAAI/bge-reranker-v2-m3")
    # oi_dspy backend server. Its own variable rather than RERANKER_BACKEND so a
    # shell that exported the batch setting cannot re-point the container (see
    # the DATABASE_URL story in docker-compose.yml). A blank model id means
    # "this backend's shared default": RERANKER_MODEL_ID for bge; FlashRank
    # pins its own bundled model and ignores the id entirely.
    UI_RERANKER_BACKEND = os.getenv("UI_RERANKER_BACKEND", "flashrank")  # "flashrank" | "bge"
    UI_RERANKER_MODEL_ID = os.getenv("UI_RERANKER_MODEL_ID", "")
    # Local cache for downloaded CrossEncoder weights (bge-reranker-v2-m3 is
    # ~2.2 GB). Separate from FlashRank's CACHE_DIR; defaults to the HF cache.
    RERANKER_CACHE_DIR = os.getenv(
        "RERANKER_CACHE_DIR", str(Path.home() / ".cache" / "huggingface")
    )
    # Resolve the reranker from RERANKER_CACHE_DIR only, never from the Hub.
    # Applies when RERANKER_MODEL_ID is a repo id; a directory path is already
    # offline by construction. The cache must have been populated by one online
    # run or an `hf download` first, otherwise construction fails fast with an
    # actionable error instead of silently reaching for the network. Equivalent
    # to HF_HUB_OFFLINE=1 but scoped to this one model load.
    RERANKER_LOCAL_FILES_ONLY = (
        os.getenv("RERANKER_LOCAL_FILES_ONLY", "false").lower() == "true"
    )
    # Max sequence length per (query, chunk) pair. bge-reranker-v2-m3 supports up
    # to 8192; 512 bounds CPU latency. Raise for better recall on long chunks when
    # the model is served on a GPU.
    RERANKER_MAX_LENGTH = int(os.getenv("RERANKER_MAX_LENGTH", "512"))
    # Max candidates the cross-encoder scores per query. The fused hybrid pool can
    # reach HYBRID_FTS_HITS + SEMANTIC_SEARCH_HITS; reranking is linear in pool
    # size, so this cap bounds CPU latency. FTS-origin chunks needed for the
    # lexical reserve are preserved on top of this cap (see HybridSearch.search).
    RERANKER_INPUT_TOP_K = int(os.getenv("RERANKER_INPUT_TOP_K", "24"))
    # (query, chunk) pairs the cross-encoder scores per forward pass. Unlike
    # RERANKER_INPUT_TOP_K this does not change *which* candidates are scored —
    # only how many at a time — so lowering it is safe for recall-tuned callers
    # such as EnumerationSearch, which deliberately reranks its whole pool.
    # This is what bounds peak VRAM/activation memory: for bge-reranker-v2-m3
    # (XLM-R large) one batch holds batch_size x RERANKER_MAX_LENGTH tokens of
    # attention buffers per layer. 32 is the sentence-transformers default; use
    # 8 when the GPU is shared with the backend container on the 6GB card.
    RERANKER_BATCH_SIZE = int(os.getenv("RERANKER_BATCH_SIZE", "32"))
    # torch intra-op thread count for the CPU cross-encoder. Defaults to all
    # logical CPUs (torch otherwise pins to physical cores). Only affects the
    # CrossEncoder — Qwen3 embeddings run out-of-process via Ollama.
    RERANKER_TORCH_THREADS = int(
        os.getenv("RERANKER_TORCH_THREADS", str(os.cpu_count() or 1))
    )
    # torch device the CrossEncoder is loaded on:
    #   "auto" (default) — let sentence-transformers pick (CUDA when visible).
    #   "cpu" / "cuda" / "cuda:1" — pin explicitly.
    # The local 6GB card is shared whenever the backend container (~2.2 GB) and
    # a batch CLI run overlap, so a second ~2.2 GB cross-encoder does not always
    # fit; BgeReranker falls back to CPU when a CUDA load *or* a ranking call
    # raises OutOfMemoryError. Set "cpu" to skip the GPU attempt entirely, which
    # is the recommended setting for batch runs alongside a running container.
    RERANKER_DEVICE = os.getenv("RERANKER_DEVICE", "auto")
    CELEX_FACTS_PROMPT_DEBUG = os.getenv("CELEX_FACTS_PROMPT_DEBUG", "True")

    # --- Entity graph (Phase 5.1) ---
    # Minimum pg_trgm similarity (0..1) for accepting a fuzzy company-name match
    # against corp_lei.legal_name during entity resolution. Below this a company
    # fact keeps a name-based natural key instead of collapsing onto an LEI —
    # conservative default trades false-splits over false-merges.
    ENTITY_LEI_MATCH_THRESHOLD = float(
        os.getenv("ENTITY_LEI_MATCH_THRESHOLD", "0.6")
    )

    # Shortest company mention that may acquire an LEI at all, measured after
    # alias canonicalization and NFKC folding. Below it the node stays
    # name-keyed regardless of which tier answered.
    #
    # Both identity sources fail on short names, and they fail differently:
    # the roster's fk_lei (a LIKE '%needle%' at MIN_LEI_NEEDLE_LENGTH = 4)
    # yields Gant -> GANTE, Shell -> NUTSHELL, Wavin -> DATAWAVING LTD, while
    # the pg_trgm probe yields AT&T -> AT, Shell -> SEA SHELL, Knauf -> Knauf
    # Re. Measured 20260909 over the 419 company nodes whose name carries an
    # fk_lei: the two sources agree on 137 of 196 names at 12 characters or
    # more (70%) but only 63 of 223 below it (28%), so the floor -- not the
    # choice of source -- is what separates the signal.
    #
    # At 12 this un-keys 660 of the 2,060 LEI-keyed company nodes; at 8 it
    # un-keys 387 and spares the 8-11 character band. Lowering it is an env
    # override, not a code change, so the two can be A/B'd on one corpus run.
    ENTITY_LEI_MIN_NAME_LENGTH = int(
        os.getenv("ENTITY_LEI_MIN_NAME_LENGTH", "12")
    )

    # Public base URL of the oi_dspy QA server, used to build clickable
    # "view chunk" provenance links that resolve via GET /chunk/{chunk_id}.
    # Must be reachable from the user's browser (set to the externally exposed
    # host when oi_dspy sits behind a reverse proxy).
    CHUNK_VIEW_BASE_URL = os.getenv("CHUNK_VIEW_BASE_URL", "http://localhost:7777")

    # --- Paths ---
    # Base directory for job files
    JOB_BASE_DIR = os.getenv("JOB_BASE_DIR", "/usr/job/a/pdf-pytesseract-ocr")

    # FlashRank ONNX model storage (separate from RERANKER_CACHE_DIR). The
    # compose backend service points it inside the hf_cache volume so the
    # model survives a container recreate.
    CACHE_DIR = os.getenv("CACHE_DIR", str(Path.home() / ".cache" / "flashrank"))

    # Source document file store (original PDFs + mineru markdown), addressed by
    # celex_id: ``<CELEX_DOCS_DIR>/<celex_id>/<celex_id>.pdf`` and
    # ``<CELEX_DOCS_DIR>/<celex_id>/mineru/auto/<celex_id>.md``.
    # Set CELEX_DOCS_DIR in config_main/.env; the default is ~/a/celex_docs for
    # the current user. expanduser() makes a literal "~/..." value work too,
    # which neither the kernel nor Docker would expand.
    CELEX_DOCS_DIR = os.path.expanduser(
        os.getenv("CELEX_DOCS_DIR", str(Path.home() / "a" / "celex_docs"))
    )

    # Report output directory; defaults to <CELEX_DOCS_DIR>/reports.
    REPORT_PATH = os.path.expanduser(
        os.getenv("REPORT_PATH", str(Path(CELEX_DOCS_DIR) / "reports"))
    )
    # The Russo/Schinkel/Guenster/Carree 2010 monograph the book cross-check
    # judges facts against: its ``book_name`` key in pg_embedding_book_qwen and
    # the MinerU markdown it was ingested from. Resolved from CELEX_DOCS_DIR so
    # a moved corpus is one env var, not a hunt for string literals (the file
    # name carries a non-ASCII umlaut; the directory the ASCII spelling).
    BOOK_NAME = os.getenv("BOOK_NAME", "Russo_Schinkel_G\u00fcnster_Carree_2010_Book")
    BOOK_MD_PATH = os.getenv(
        "BOOK_MD_PATH",
        str(
            Path(CELEX_DOCS_DIR)
            / "Russo_Schinkel_Guenster_Book"
            / "Russo_Schinkel_G\u00fcnster_Carree_2010_Book-mineru.md"
        ),
    )

    # Derived paths can be properties or methods
    @property
    def DOCS_DIR(self):
        return os.path.join(self.JOB_BASE_DIR, "docs")

    PRJ_ROOT_DIR = PROJECT_ROOT
    CONFIG_DIR = CONFIG_MAIN_DIR
    FACT_EXTRACTOR_CONFIG_DIR = CONFIG_DIR / "FACT_EXTRACTOR.yaml"
    LOGGING_CONFIG_PATH = os.getenv(
        "LOGGING_CONFIG_PATH", str(CONFIG_DIR / "logging_config.yaml")
    )

    NACE_JSON_PATH = os.getenv(
        "NACE_JSON_PATH", str(Path(CELEX_DOCS_DIR) / "NACE_2_1.json")
    )

    # GLEIF Accepted Legal Jurisdictions code list (ISO 3166-1 country codes plus
    # the ISO 3166-2 subdivision codes GLEIF accepts as legal jurisdictions),
    # loaded into celex_facts.gleif_jurisdication2countrycode.
    GLEIF_JURISDICTIONS_CSV_PATH = os.getenv(
        "GLEIF_JURISDICTIONS_CSV_PATH",
        str(Path(CELEX_DOCS_DIR) / "gleif_acceptedjurisdictions_v1.5.csv"),
    )
    GLEIF_JURISDICTIONS_VERSION = os.getenv("GLEIF_JURISDICTIONS_VERSION", "1.5")

    # Per-document descriptor sheet driving the batch runners: which CELEX
    # documents exist, their local PDF/markdown files, and their languages.
    MASTER_DOC_DESCRIPTORS_PATH = os.getenv(
        "MASTER_DOC_DESCRIPTORS_PATH", str(CONFIG_DIR / "master_doc_descriptors.json")
    )
    # The pre-migration source sheet that MASTER_DOC_DESCRIPTORS_PATH was
    # derived from; read only by batch/utils/migrate_descriptors.py.
    MASTER_SHEET_PATH = os.getenv(
        "MASTER_SHEET_PATH", str(CONFIG_DIR / "master_sheet.json")
    )
    # CELEX ids to skip when loading the descriptor sheet.
    CELEX_FILTER_PATH = os.getenv(
        "CELEX_FILTER_PATH", str(Path(CELEX_DOCS_DIR) / "celexFilterEmpty.txt")
    )

    # --- HeidelTime Configuration ---
    HEIDELTIME_LANGUAGE = os.getenv("HEIDELTIME_LANGUAGE", "english")
    HEIDELTIME_DOCUMENT_TYPE = os.getenv("HEIDELTIME_DOCUMENT_TYPE", "Narrative")

    # --- Chunk Language Audit Configuration ---
    # The language every ingested document is expected to be in; anything else
    # is a candidate for re-loading from the correct translation.
    LANGUAGE_AUDIT_EXPECTED_LANGUAGE = os.getenv(
        "LANGUAGE_AUDIT_EXPECTED_LANGUAGE", "en"
    )
    # Below this chunk length langdetect is noise (markdown tables, "Table 3",
    # bare citations), so short chunks are excluded from the evidence base
    # rather than scored. Costs 1,763 of 56,005 chunks on the current corpus.
    LANGUAGE_AUDIT_MIN_CHARS = int(os.getenv("LANGUAGE_AUDIT_MIN_CHARS", "200"))
    # Minimum langdetect probability for a verdict to count as evidence.
    LANGUAGE_AUDIT_MIN_CONFIDENCE = float(
        os.getenv("LANGUAGE_AUDIT_MIN_CONFIDENCE", "0.90")
    )
    # A document with less scored text than this cannot be judged either way.
    LANGUAGE_AUDIT_MIN_SCORED_CHARS = int(
        os.getenv("LANGUAGE_AUDIT_MIN_SCORED_CHARS", "2000")
    )
    # Char-weighted non-expected-language share thresholds. Observed corpus is
    # sharply bimodal: the one genuine wrong-language document sits at 0.992,
    # the worst confirmed false positive (an English decision quoting Dutch
    # organisation names) at 0.355, the next at 0.167. RELOAD sits inside that
    # empty gap; REVIEW isolates the quoting documents for a human glance.
    LANGUAGE_AUDIT_REVIEW_SHARE = float(
        os.getenv("LANGUAGE_AUDIT_REVIEW_SHARE", "0.25")
    )
    LANGUAGE_AUDIT_RELOAD_SHARE = float(
        os.getenv("LANGUAGE_AUDIT_RELOAD_SHARE", "0.75")
    )
    # Per-document descriptor file supplying the expected language_init /
    # language_translated cross-check for the audit report. Same sheet as
    # MASTER_DOC_DESCRIPTORS_PATH; kept as its own key so the audit can be
    # pointed at a different snapshot without moving the batch runners.
    LANGUAGE_AUDIT_DESCRIPTOR_PATH = os.getenv(
        "LANGUAGE_AUDIT_DESCRIPTOR_PATH", MASTER_DOC_DESCRIPTORS_PATH
    )

    # --- Evidence Verifier Configuration ---
    # Minimum LLM support_score for a candidate chunk to count as fully
    # verified; between weak and this threshold the fact is "weak_support".
    EVIDENCE_SUPPORT_MIN_SCORE = float(
        os.getenv("EVIDENCE_SUPPORT_MIN_SCORE", "0.75")
    )
    EVIDENCE_WEAK_MIN_SCORE = float(os.getenv("EVIDENCE_WEAK_MIN_SCORE", "0.4"))
    # Upper bound on candidate chunks pulled from each retrieval leg before
    # scoring/gating (recall-oriented; verification is an offline job).
    EVIDENCE_CANDIDATE_LIMIT = int(os.getenv("EVIDENCE_CANDIDATE_LIMIT", "40"))
    # How many top-ranked gated candidates reach the LLM support judge. Kept
    # small: each is a sequential LLM call, and the loop stops early on the
    # first 'supports' verdict with an exact quote.
    EVIDENCE_JUDGE_TOP_N = int(os.getenv("EVIDENCE_JUDGE_TOP_N", "3"))
    # Fact names skipped entirely by the verifier: machine-emitted artefacts
    # that make no claim about the decision, so there is nothing for a chunk to
    # support. decision_chapter_tokens / decision_chapter_sentences are counters
    # over the decision text; heideltime_raw is the raw TIMEX3 annotation dump
    # of the very chunks the verifier would retrieve, so judging it only
    # confirms that the annotator read its own input (it scored
    # verified_supported on 737 of 745 rows before exclusion). Its temporal
    # claims are verified downstream on the derived event facts instead.
    # Comma-separated env override.
    EVIDENCE_EXCLUDED_FACT_NAMES = frozenset(
        name.strip()
        for name in os.getenv(
            "EVIDENCE_EXCLUDED_FACT_NAMES",
            "decision_chapter_tokens,decision_chapter_sentences,heideltime_raw",
        ).split(",")
        if name.strip()
    )
    # Whether the verifier enriches chunk retrieval and the LLM judge with the
    # original extraction question (from fact_ingestion_audit_trail). Helps
    # short-value facts (e.g. boolean / enum) where the value alone is a weak
    # retrieval/judging signal. Disable to fall back to value-only behaviour.
    EVIDENCE_USE_EXTRACTION_QUESTION = (
        os.getenv("EVIDENCE_USE_EXTRACTION_QUESTION", "true").lower() == "true"
    )

    # --- Book Cross-Check Configuration ---
    # Character budget for the book-chapter context concatenated into a single
    # support-judge call per fact (a chapter spans many rows; the cap bounds the
    # per-fact LLM prompt size and cost). Applied after ordering rows by doc_pos.
    BOOK_CROSSCHECK_CONTEXT_CHARS = int(
        os.getenv("BOOK_CROSSCHECK_CONTEXT_CHARS", "8000")
    )
    # Safety cap on the case-scoped candidate chunks (all chunks of every
    # chapter that mentions the case_id) reranked against each fact's extraction
    # question to pick the single best book chunk to judge that fact against.
    # Larger gives the reranker more of the case's chapters to choose from at the
    # cost of a bigger rerank batch.
    BOOK_CROSSCHECK_CANDIDATE_LIMIT = int(
        os.getenv("BOOK_CROSSCHECK_CANDIDATE_LIMIT", "80")
    )
    # Fact-name prefixes whose value identifies one specific case (its decision
    # date, its conduct period, its fines, its parties). A landmark chapter
    # covers several cases, so these facts are judged only against book chunks
    # whose text names the case; a chunk from elsewhere in the chapter would
    # state another case's date and read as a contradiction (design doc F-5).
    # Facts outside this list may also use the rest of the chapter.
    BOOK_CASE_SPECIFIC_FACT_PREFIXES: tuple[str, ...] = (
        "eu_commisssion_decision_date",
        "heideltime_",
        "cartel_period",
        "fines",
        "companies_",
        "case_commission",
    )
    # Fact-name prefixes whose value is an infringement classification. The book
    # organises cases by infringement type, so its chapter section is the
    # authoritative classification: a classification fact asserting a
    # mutually-exclusive type (e.g. a horizontal-cartel label for a case the book
    # analyses under abuse of dominance) is a contradiction the support judge
    # cannot see (the chapter is simply silent on the other type). These facts are
    # routed through the book-taxonomy consistency check instead.
    BOOK_CLASSIFICATION_FACT_PREFIXES: tuple[str, ...] = (
        "infringement_category",
        "infringement_subcategory",
    )
    # Fact-name prefixes for infringement facts. On a *simple* (example) book
    # reference only these facts may be flagged as contradictions; any other
    # fact can be corroborated (exact quote) but never contradicted, because a
    # brief example mention is silent about most facts rather than denying them.
    BOOK_INFRINGEMENT_FACT_PREFIXES: tuple[str, ...] = ("infringement",)
    # Path-segment markers (full section headings) identifying the book's
    # "Economic landmark cases" subsection. A case whose section path contains
    # one of these is a landmark case: the richest analysis, validated in full.
    # The book also uses the singular heading "F Economic landmark case" (3.9,
    # 6.3, chapter 8); the singular marker is a substring of both spellings.
    BOOK_LANDMARK_SECTION_MARKERS: tuple[str, ...] = ("F Economic landmark case",)
    # Path-segment markers for the other lettered sub-chapters of a book section
    # that the fact2book discrepancy reporter tells apart: "E" is a bare
    # chronological list of decisions (a mention, not an analysis) and "A" is
    # the section's introductory prose. B-D are reference lists.
    BOOK_LISTING_SECTION_MARKERS: tuple[str, ...] = (
        "E Commission decisions",
        "E Selection of commission decisions",
    )
    BOOK_INTRODUCTION_SECTION_MARKERS: tuple[str, ...] = ("A Introduction",)
    # Path prefixes marking pure index / back-matter references (bare case-id
    # lists). A case appearing only under these is skipped entirely.
    BOOK_INDEX_PATH_PREFIXES: tuple[str, ...] = ("Annex", "Index")
    # Case-number prefixes naming the same Commission case in different eras
    # ("IV/36545" before ~1999, "COMP/36.545" after). ``celex_case_id.case_id``
    # always carries "IV/", so book-case resolution also probes the other
    # prefixes; without it every COMP-era case reads as absent from the book.
    BOOK_CASE_ID_PREFIXES: tuple[str, ...] = ("IV/", "COMP/")
    # Deterministic book-taxonomy check (pipelines/agent_dspy/book_taxonomy.py).
    # Lower-case value labels of each label-valued classification fact, mapped
    # to the book chapter ("2") or section ("2.1") filing that infringement
    # type. A label is claimed when it occurs anywhere in the fact value, so
    # list, ";"-joined and table-formatted values all resolve.
    BOOK_TAXONOMY_LABEL_NUMBERS: dict[str, dict[str, str]] = {
        "infringement_category_hor_vert_lic_joint": {
            "horizontal": "2",
            "license": "4",
            "licence": "4",
            "vertical": "5",
            "joint venture": "6",
        },
        "infringement_subcategory_horizontal_restraints": {
            "price fixing": "2.1",
            "market sharing": "2.2",
            "bid-rigging": "2.3",
            "tender fixing": "2.3",
            "structural crisis": "2.4",
            "export cartel": "2.5",
            "trade association": "2.6",
            "marketing and advertising": "2.7",
            "agreements on standards": "2.8",
            "exchange of information": "2.9",
        },
        "infringement_subcategory_vertical_restraints": {
            "exclusive dealing": "5.1",
            "territorial exclusivity": "5.2",
            "parallel trade": "5.2",
            "resale price maintenance": "5.3",
            "bundling and tying": "5.4",
            "selective distribution": "5.5",
            "franchising": "5.6",
            "discounts and rebates": "5.7",
        },
    }
    # Boolean classification facts: the chapter a True value asserts.
    BOOK_TAXONOMY_BOOLEAN_CHAPTERS: dict[str, str] = {
        "infringement_category_abuse_of_dominance": "3",
        "infringement_category_member_states": "7",
    }
    # Conduct chapters that each file a case under one infringement type, so a
    # label fact claiming a different chapter is contradicted. Art. 106 (7) and
    # merger (8) placements never contradict on their own.
    BOOK_TAXONOMY_DECISIVE_CHAPTERS: tuple[str, ...] = ("2", "3", "4", "5", "6")
    # Cap on distinct section paths fetched per case to classify its reference
    # type (landmark / simple / index).
    BOOK_CROSSCHECK_REFERENCE_PATH_LIMIT = int(
        os.getenv("BOOK_CROSSCHECK_REFERENCE_PATH_LIMIT", "200")
    )

    # --- EU treaty renumbering canonicalization (infringement-concept) ---
    # The competition articles were renumbered twice (EEC -> EC at Amsterdam,
    # EC -> TFEU at Lisbon), so the same concept carries different numbers per
    # era. These cut-off dates separate the three eras and intentionally match
    # the thresholds already encoded in the FACT_EXTRACTOR.yaml infringement
    # conditions, so the canonical normalizer and any legacy date-coupled rule
    # agree on where an "Article 86" mention resolves.
    TREATY_BOUNDARY_AMSTERDAM = os.getenv("TREATY_BOUNDARY_AMSTERDAM", "1999-04-30")
    TREATY_BOUNDARY_LISBON = os.getenv("TREATY_BOUNDARY_LISBON", "2009-11-30")
    # Plausibility window for the year parsed from a CELEX id; bounds a malformed
    # id rather than trusting any four digits as a year.
    CELEX_MIN_YEAR = int(os.getenv("CELEX_MIN_YEAR", "1950"))
    CELEX_MAX_YEAR = int(os.getenv("CELEX_MAX_YEAR", "2099"))

    # --- Master Planner Agent Configuration ---
    # Fallback agent over the primary QA pipeline for compositional queries
    # (multi-entity filtering, count / group-by aggregation). Activates only
    # when the primary pipeline returns insufficient evidence.
    MASTER_PLANNER_ENABLED = (
        os.getenv("MASTER_PLANNER_ENABLED", "true").lower() == "true"
    )
    # Upper bound on sub-tasks executed per query (cost guard).
    MASTER_PLANNER_MAX_SUBTASKS = int(os.getenv("MASTER_PLANNER_MAX_SUBTASKS", "8"))
    # Upper bound on candidate cases whose structured facts are loaded for the
    # join / aggregate stage.
    MASTER_PLANNER_MAX_CASES = int(os.getenv("MASTER_PLANNER_MAX_CASES", "60"))
    # Whether the verify stage gates contributing rows via EvidenceVerifierModule.
    MASTER_PLANNER_VERIFY_ROWS = (
        os.getenv("MASTER_PLANNER_VERIFY_ROWS", "true").lower() == "true"
    )
    # Hard upper bound on the number of (celex_id, fact_name) rows sent to the
    # per-row evidence verifier in one query. A runaway guard: each row can cost
    # several sequential LLM-judge calls, so an unscoped case_list over dozens of
    # cases (~60 facts each) would otherwise issue hundreds of LLM calls and
    # stall the "Verifying contributing facts" stage for many minutes. Rows past
    # the cap are passed through unverified rather than dropped.
    MASTER_PLANNER_MAX_VERIFY_ROWS = int(
        os.getenv("MASTER_PLANNER_MAX_VERIFY_ROWS", "120")
    )
    # Per-value character budget for a fact value rendered into the synthesizer
    # evidence block. Applied AFTER markdown-table normalization so the model
    # sees a clean condensed value rather than a fragment chopped mid-table.
    MASTER_PLANNER_EVIDENCE_SNIPPET = int(
        os.getenv("MASTER_PLANNER_EVIDENCE_SNIPPET", "300")
    )

    # --- Knowledge Question Probe ---
    # Whether the QA executor matches the question against the stored
    # knowledge question variants before running the plan, so knowledge from a
    # previously answered question reaches the synthesizer whatever tools the
    # planner picked. False restores plan-only evidence for A/B.
    KNOWLEDGE_PROBE_ENABLED = (
        os.getenv("KNOWLEDGE_PROBE_ENABLED", "true").lower() == "true"
    )
    # Max cosine distance to a stored question variant. Measured 20260911 on
    # qwen3 embeddings: paraphrases land at 0.03-0.08, while entity swaps
    # ("cartel peers of NSK" vs the NTN item) sit at 0.17-0.19. A scope swap
    # still lands at 0.07, which the probe's anchor guard catches instead.
    KNOWLEDGE_PROBE_MAX_DISTANCE = float(
        os.getenv("KNOWLEDGE_PROBE_MAX_DISTANCE", "0.10")
    )
    # Items one probe may return. A list answer is distilled one item per
    # entry (one knowledge item per cartel peer), all sharing a seed question.
    KNOWLEDGE_PROBE_LIMIT = int(os.getenv("KNOWLEDGE_PROBE_LIMIT", "20"))

    # --- Knowledge Ingestion: entity-graph evidence ---
    # Whether knowledge ingestion resolves the cited facts to their validated
    # entity_edges relations, renders them into the extractor and the LLM
    # judge, and runs the deterministic pre-judge contradiction gate. The graph
    # is a verified projection of the fact store (source_fact_id is NOT NULL),
    # so its relations outrank retrieved prose. False restores the prose-only
    # pipeline for A/B; the edges stay in the database either way.
    KNOWLEDGE_GRAPH_EVIDENCE_ENABLED = (
        os.getenv("KNOWLEDGE_GRAPH_EVIDENCE_ENABLED", "true").lower() == "true"
    )
    # Upper bound on relations one lookup may return. A participation fact
    # family emits one edge per party, so edges-per-fact is heavily skewed:
    # measured 20260912 on the live graph, avg 4.3 and p95 15, but the largest
    # single fact carries 196. This bounds the pathological trace; the
    # deterministic contradiction gate wants the wide set, so keep it generous.
    KNOWLEDGE_GRAPH_EVIDENCE_MAX_RELATIONS = int(
        os.getenv("KNOWLEDGE_GRAPH_EVIDENCE_MAX_RELATIONS", "500")
    )
    # Relations rendered into the extractor and judge prompts. Far tighter than
    # the lookup bound because this one costs context on every call, and a long
    # party roster adds little the judge can use. Truncation is stated in the
    # block so the model never reads a partial list as an exhaustive one.
    KNOWLEDGE_GRAPH_EVIDENCE_RENDER_LIMIT = int(
        os.getenv("KNOWLEDGE_GRAPH_EVIDENCE_RENDER_LIMIT", "40")
    )

    # --- Entity-Graph Q&A Configuration ---
    # Whether the planner schema prose advertises the flat entity-graph views
    # (company_case_relations_vw, case_provision_relations_vw) alongside the EAV
    # facts table. Set false to A/B the planners without the graph surface; the
    # views stay in the database either way, only the LLM stops being told.
    GRAPH_QA_ENABLED = os.getenv("GRAPH_QA_ENABLED", "true").lower() == "true"

    # Whether the graph_search traversal tool is advertised to the planners and
    # dispatched. Independent of GRAPH_QA_ENABLED (which governs the flat views)
    # so the multi-hop surface can be A/B'd on its own.
    GRAPH_SEARCH_ENABLED = (
        os.getenv("GRAPH_SEARCH_ENABLED", "true").lower() == "true"
    )
    # Traversal depth bound for graph_search. Measured on the live graph: depth
    # 1 and 2 both answer in ~19ms, but depth 3 costs 368ms and returns 762
    # peers - over a tenth of every company node, which carries no meaning.
    GRAPH_SEARCH_MAX_HOPS = int(os.getenv("GRAPH_SEARCH_MAX_HOPS", "2"))
    # Cap on company nodes one name stem may seed. Aliases still split across
    # nodes (a minority are LEI-keyed), so a legitimate stem matches several;
    # the cap keeps the most-connected ones and bounds a too-short stem.
    GRAPH_SEARCH_MAX_SEEDS = int(os.getenv("GRAPH_SEARCH_MAX_SEEDS", "25"))
    # Cap on peers rendered into one evidence block, so a depth-2 expansion
    # cannot flood the synthesizer prompt.
    GRAPH_SEARCH_MAX_PEERS = int(os.getenv("GRAPH_SEARCH_MAX_PEERS", "80"))

    # Minimum length of a single-token company stem before it may take part in
    # subsequence matching of HeidelTime actor strings. Guards against an
    # initialism swallowing an unrelated company inside the same case (the
    # measured example: "AZ" must not match "AstraZeneca"). Multi-token stems
    # are never length-gated; exact matches bypass this entirely.
    HEIDELTIME_ACTOR_MIN_STEM_LEN = int(os.getenv("HEIDELTIME_ACTOR_MIN_STEM_LEN", "4"))

    # --- Conversation-thread Configuration ---
    # Open WebUI posts the whole message thread on every turn. These switches
    # control how much of it the backend actually uses. Each degrades to the
    # pre-feature behaviour when off, so any piece can be A/B'd independently.
    # Read them at the call site (never bind to a module constant at import) so
    # tests can flip one with patch.object(settings, ...).
    #
    # Master switch: when false no history reaches the retrieval router and a
    # follow-up is answered from its own words alone.
    CONVERSATION_HISTORY_ENABLED = (
        os.getenv("CONVERSATION_HISTORY_ENABLED", "true").lower() == "true"
    )
    # Whether a dependent follow-up ("list those cases") is rewritten into a
    # standalone question before it reaches the celex gate and the planner.
    CONVERSATION_CONDENSE_ENABLED = (
        os.getenv("CONVERSATION_CONDENSE_ENABLED", "true").lower() == "true"
    )
    # Bypass the pure looks_dependent() pre-gate and condense every turn. Costs
    # one extra LLM call per turn; used to A/B the heuristic against the model.
    CONVERSATION_CONDENSE_ALWAYS = (
        os.getenv("CONVERSATION_CONDENSE_ALWAYS", "false").lower() == "true"
    )
    # Whether the case set a turn resolved is pinned into its answer and reused
    # to scope the next turn, instead of being re-derived from rewritten text.
    CONVERSATION_PIN_CELEX_ENABLED = (
        os.getenv("CONVERSATION_PIN_CELEX_ENABLED", "true").lower() == "true"
    )
    # How many prior turns (newest first) are shown to the condenser.
    CONVERSATION_HISTORY_MAX_TURNS = int(
        os.getenv("CONVERSATION_HISTORY_MAX_TURNS", "6")
    )
    # Cap on pinned case ids carried across a turn, so a broad first answer
    # cannot produce an unbounded IN-list on the follow-up.
    CONVERSATION_PIN_MAX_CASES = int(os.getenv("CONVERSATION_PIN_MAX_CASES", "60"))
    # Whether Open WebUI's background calls (title / tag / follow-up generation)
    # are rejected at the endpoint instead of running the full pipeline.
    WEBUI_REJECT_TASK_PROMPTS = (
        os.getenv("WEBUI_REJECT_TASK_PROMPTS", "true").lower() == "true"
    )

    @staticmethod
    def setup_logging(default_path=LOGGING_CONFIG_PATH):
        logs_dir = Config.PRJ_ROOT_DIR / "logs"
        os.makedirs(logs_dir, exist_ok=True)

        with open(default_path, "rt") as f:
            config = yaml.safe_load(f.read())

        # Resolve relative log-file paths against the project root so the
        # correct location is used regardless of the process working directory.
        for handler in config.get("handlers", {}).values():
            filename = handler.get("filename")
            if filename and not Path(filename).is_absolute():
                handler["filename"] = str(Config.PRJ_ROOT_DIR / filename)

        logging.config.dictConfig(config)


settings = Config()
# Initialize once at the start of your app
settings.setup_logging()

# Now, loggers in different files will respect the YAML settings
logger = logging.getLogger(__name__)
logger.info(f"Logging is configured via {settings.LOGGING_CONFIG_PATH}")
