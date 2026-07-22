"""Central settings, all overridable by environment variables (see .env.example)."""
import os, pathlib, sys

BASE          = pathlib.Path(__file__).resolve().parent.parent      # repo root (dev) or site-packages (installed)
CONTRACT_DIR  = BASE / "contract"
STEEL_ENGINE  = BASE / "steel_engine"
FRONTEND_DIR  = BASE / "frontend"
EXAMPLES_DIR  = BASE / "test_buildings"

# DISCLAIMER.md lives at the repo root in a dev checkout; the wheel bundles a copy inside the package.
_pkg_disclaimer = pathlib.Path(__file__).resolve().parent / "DISCLAIMER.md"
DISCLAIMER_FILE = (BASE / "DISCLAIMER.md") if (BASE / "DISCLAIMER.md").exists() else _pkg_disclaimer


def _default_data_dir() -> pathlib.Path:
    """Per-user application data dir (overridable with DATA_DIR)."""
    env = os.environ.get("DATA_DIR")
    if env:
        return pathlib.Path(env)
    if sys.platform == "win32":
        root = pathlib.Path(os.environ.get("LOCALAPPDATA") or (pathlib.Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = pathlib.Path.home() / "Library" / "Application Support"
    else:
        root = pathlib.Path(os.environ.get("XDG_DATA_HOME") or (pathlib.Path.home() / ".local" / "share"))
    return root / "Steltic"


DATA         = _default_data_dir();       DATA.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR = DATA / "sessions";         SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# --- sandbox executor ---
EXECUTOR        = os.environ.get("EXECUTOR", "auto")        # auto | docker | subprocess
SANDBOX_IMAGE   = os.environ.get("SANDBOX_IMAGE", "steel-sandbox:latest")
SANDBOX_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT", "900"))   # seconds per run_python
SANDBOX_MEM     = os.environ.get("SANDBOX_MEM", "2g")
SANDBOX_CPUS    = os.environ.get("SANDBOX_CPUS", "2")
SANDBOX_PIDS    = os.environ.get("SANDBOX_PIDS", "256")

# --- agent loop ---
MAX_STEPS       = int(os.environ.get("MAX_STEPS", "200"))
REPEAT_LIMIT    = int(os.environ.get("REPEAT_LIMIT", "10"))     # PAUSE if one tool-call signature recurs this many times
REPEAT_NUDGE    = int(os.environ.get("REPEAT_NUDGE", "3"))      # soft-nudge once when a signature recurs this many times
PROGRESS_STALL_NUDGE = int(os.environ.get("PROGRESS_STALL_NUDGE", "10"))  # steps w/ no new file or rc=0 -> nudge
PROGRESS_STALL_PAUSE = int(os.environ.get("PROGRESS_STALL_PAUSE", "20"))  # steps w/ no progress -> pause for review
MAX_CALLS       = int(os.environ.get("MAX_CALLS", "0"))         # hard cap on model calls per run (0 = use MAX_STEPS)
TOOL_OUT_CAP    = int(os.environ.get("TOOL_OUT_CAP", "16000"))
RAG_OUT_CAP     = int(os.environ.get("RAG_OUT_CAP", "60000"))   # spec RAG results (saved to file + evicted at completion) get a higher cap so full hits stay in context and the JSON stays valid for eviction
TEMPERATURE     = float(os.environ.get("TEMPERATURE", "0.3"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "1200"))
RETRIES         = int(os.environ.get("RETRIES", "8"))           # LLM-call retries before giving up
RETRY_BACKOFF_CAP = int(os.environ.get("RETRY_BACKOFF_CAP", "60"))  # max seconds between retries (backoff ramps up to this)

# --- RAG (engineering standards). Empty => the search tool returns a 'disabled' note and the agent
# relies on its own AISC knowledge. Point it at your own RAG server, or request access to the
# hosted Steltic RAG via the contact details at https://stelticai.com. ---
RAG_API_URL   = os.environ.get("RAG_API_URL", "")
RAG_API_TOKEN = os.environ.get("RAG_API_TOKEN", "")  # bearer token, sent on every RAG call when set
RAG_TOP_K          = int(os.environ.get("RAG_TOP_K", "5"))          # chunks per RAG search
RAG_KEEP_RECENT    = int(os.environ.get("RAG_KEEP_RECENT", "0"))    # 0 = OFF (recommended w/ provider caching). >0 = keep N recent RAG results full, stub older
RAG_SEARCH_SOFTCAP = int(os.environ.get("RAG_SEARCH_SOFTCAP", "20"))  # after this many searches, nudge the agent to stop searching and derive
# Spec (AISC/ASCE) RAG results are ALWAYS saved to jobs/<name>/rag/<slug>.txt and, once a design completes,
# evicted from the saved conversation to a file pointer (agent._evict_all_rag) so optimisation runs don't bloat.
