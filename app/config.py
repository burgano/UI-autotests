import os
import shutil

CLAUDE_CLI_PATH = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

MODELS = {
    "haiku":  {"id": "claude-haiku-4-5-20251001",  "label": "Haiku 4.5  - fast, simple pages",      "min_complexity": 0},
    "sonnet": {"id": "claude-sonnet-4-6",           "label": "Sonnet 4.6 - recommended, most pages", "min_complexity": 10},
    "opus":   {"id": "claude-opus-4-6",             "label": "Opus 4.6   - complex, deep flows",     "min_complexity": 40},
}

MODEL_DEFAULT = "sonnet"

# Minimum recommended model per estimated test count
MODEL_THRESHOLDS = {
    "haiku":  (0, 9),    # 0-9 tests
    "sonnet": (10, 39),  # 10-39 tests
    "opus":   (40, 999), # 40+ tests
}

DEFAULT_BUDGET_USD  = 3.00   # per endpoint call. Safety ceiling - actual spend is usually much lower.
FIX_MODEL_ID        = MODELS["haiku"]["id"]  # model used for self-healing fixes - simpler task, cheaper
FIX_BUDGET_USD      = 1.00   # budget ceiling per fix attempt
MAX_URLS_MANUAL     = 20
MAX_URLS_EXPLORE    = 50
FLAKINESS_RERUNS    = 1

# Tokens estimate per test (rough)
TOKENS_PER_TEST_INPUT  = 500
TOKENS_PER_TEST_OUTPUT = 800
COST_PER_1K_SONNET     = 0.003  # USD input, rough blended
