import os
import re
import shutil
import subprocess
import threading

CLAUDE_CLI_PATH = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

# Fallback model list — used immediately on startup and replaced in background
MODELS = {
    "haiku":  {"id": "claude-haiku-4-5-20251001",  "label": "Haiku 4.5  - fast, simple pages",      "min_complexity": 0},
    "sonnet": {"id": "claude-sonnet-4-6",           "label": "Sonnet 4.6 - recommended, most pages", "min_complexity": 10},
    "opus":   {"id": "claude-opus-4-6",             "label": "Opus 4.6   - complex, deep flows",     "min_complexity": 40},
}

MODEL_DEFAULT = "sonnet"

# Tier detection: maps lowercase keywords in model name → tier key + description
_TIER_MAP = [
    ("haiku",  "haiku",  "fast, simple pages",      0),
    ("sonnet", "sonnet", "recommended, most pages", 10),
    ("opus",   "opus",   "complex, deep flows",     40),
]


def _discover_models_bg() -> None:
    """
    Run `claude models` in a background thread and update MODELS in-place.
    Does not block Flask startup.
    """
    global MODELS

    if not os.path.isfile(CLAUDE_CLI_PATH):
        return

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    try:
        result = subprocess.run(
            [CLAUDE_CLI_PATH, "models"],
            capture_output=True, text=True, timeout=15, env=env,
        )
        output = result.stdout or ""
    except Exception:
        return

    discovered: dict[str, dict] = {}
    for line in output.splitlines():
        m = re.match(r"\|\s*\*{0,2}(.+?)\*{0,2}\s*\|\s*`([^`]+)`\s*\|", line)
        if not m:
            continue
        name, model_id = m.group(1).strip(), m.group(2).strip()
        name_low = name.lower()

        for keyword, tier_key, description, min_complexity in _TIER_MAP:
            if keyword in name_low:
                existing = discovered.get(tier_key)
                if existing is None or model_id > existing["id"]:
                    discovered[tier_key] = {
                        "id":             model_id,
                        "label":          f"{name} - {description}",
                        "min_complexity": min_complexity,
                    }
                break

    if not discovered:
        return

    # Fill in any missing tiers from current MODELS
    for key, val in MODELS.items():
        if key not in discovered:
            discovered[key] = val

    MODELS.update(discovered)


# Kick off discovery without blocking startup
threading.Thread(target=_discover_models_bg, daemon=True).start()

# Minimum recommended model per estimated test count
MODEL_THRESHOLDS = {
    "haiku":  (0, 9),    # 0-9 tests
    "sonnet": (10, 39),  # 10-39 tests
    "opus":   (40, 999), # 40+ tests
}

DEFAULT_BUDGET_USD  = 3.00   # per endpoint call. Safety ceiling - actual spend is usually much lower.
def FIX_MODEL_ID() -> str:  # function so it always returns the current haiku ID after bg discovery
    return MODELS["haiku"]["id"]
FIX_BUDGET_USD      = 1.00   # budget ceiling per fix attempt
MAX_URLS_MANUAL     = 20
MAX_URLS_EXPLORE    = 50
FLAKINESS_RERUNS    = 1

# Tokens estimate per test (rough)
TOKENS_PER_TEST_INPUT  = 500
TOKENS_PER_TEST_OUTPUT = 800
COST_PER_1K_SONNET     = 0.003  # USD input, rough blended
