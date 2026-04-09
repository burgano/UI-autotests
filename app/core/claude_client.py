"""
Subprocess wrapper for the Claude Code CLI.
Claude writes test files directly into the autotest project directory.

Key fixes:
- Prompt passed via stdin (not as CLI argument) - avoids OS arg length limits and hanging
- --dangerously-skip-permissions (correct flag name)
- CLAUDECODE env var removed - prevents "nested session" error
"""

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from app.config import CLAUDE_CLI_PATH, DEFAULT_BUDGET_USD


RATE_LIMIT_PHRASES = (
    "rate limit", "rate_limit", "429", "too many requests",
    "quota exceeded", "claude.ai/settings",
    "you have exceeded", "daily limit", "monthly limit",
)

# Temporary server-side overload - NOT a user rate limit
OVERLOAD_PHRASES = (
    "overloaded", "529", "service unavailable", "503",
)

AUTH_ERROR_PHRASES = (
    "not logged in", "unauthenticated", "authentication", "not authenticated",
    "login required", "api key", "unauthorized", "401",
)


@dataclass
class ClaudeResult:
    success: bool
    output: str
    error: Optional[str] = None
    error_type: Optional[str] = None   # "rate_limit" | "auth" | "budget" | "overload" | None
    cost_usd: Optional[float] = None
    tokens_used: Optional[int] = None


def run(
    prompt: str,
    working_dir: str,
    extra_dirs: Optional[list] = None,
    model: str = "claude-sonnet-4-6",
    budget_usd: float = DEFAULT_BUDGET_USD,
    timeout: int = 300,
    on_proc_start=None,   # callback(proc) called right after Popen, for cancellation support
) -> ClaudeResult:
    if not os.path.isfile(CLAUDE_CLI_PATH):
        return ClaudeResult(
            success=False,
            output="",
            error=f"Claude CLI not found at: {CLAUDE_CLI_PATH}. Install Claude Code first.",
        )

    cmd = [
        CLAUDE_CLI_PATH,
        "-p",                          # --print shorthand
        "--output-format", "json",
        "--model", model,
        "--max-budget-usd", str(budget_usd),
        "--dangerously-skip-permissions",  # correct flag (not --permission-mode)
        "--add-dir", working_dir,
    ]

    for d in (extra_dirs or []):
        if d and os.path.isdir(d):
            cmd += ["--add-dir", d]

    # Remove CLAUDECODE env var - Claude CLI refuses to run inside another Claude session
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=working_dir,
            env=env,
        )
        if on_proc_start:
            on_proc_start(proc)
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return ClaudeResult(success=False, output="", error="Claude CLI timed out")
        # Build a result-like object for the rest of the function
        result = type("R", (), {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr})()
    except FileNotFoundError:
        return ClaudeResult(success=False, output="", error=f"Cannot execute Claude CLI: {CLAUDE_CLI_PATH}")

    # Killed by cancel - treat as cancelled, not an error
    if result.returncode == -9 or result.returncode == -15:
        return ClaudeResult(success=False, output="", error="cancelled")

    # Log raw stderr for debugging (first 200 chars)
    if result.stderr and result.stderr.strip():
        import sys as _sys
        print(f"[claude_client] stderr: {result.stderr.strip()[:200]}", file=_sys.stderr)

    # Check stderr for auth / rate limit / overload signals before parsing stdout
    stderr_low = (result.stderr or "").lower()
    if result.returncode != 0 and not result.stdout:
        err_msg = (result.stderr or "Claude CLI returned non-zero exit code")[:500]
        err_low  = err_msg.lower()
        if any(p in err_low for p in RATE_LIMIT_PHRASES):
            return ClaudeResult(success=False, output="", error=err_msg, error_type="rate_limit")
        if any(p in err_low for p in AUTH_ERROR_PHRASES):
            return ClaudeResult(success=False, output="", error=err_msg, error_type="auth")
        if any(p in err_low for p in OVERLOAD_PHRASES):
            return ClaudeResult(success=False, output="", error=err_msg, error_type="overload")
        return ClaudeResult(success=False, output="", error=err_msg)

    try:
        data = json.loads(result.stdout)

        # Check for budget exceeded
        if data.get("subtype") == "error_max_budget_usd":
            actual_cost = data.get("total_cost_usd", "?")
            return ClaudeResult(
                success=False,
                output="",
                error=f"Budget exceeded: actual cost ${actual_cost:.3f}, limit was ${budget_usd}. Increase budget in config.",
                error_type="budget",
            )

        # Check result text for rate limit / auth errors
        result_text = str(data.get("result", data.get("content", "")))
        result_low  = result_text.lower()
        if data.get("is_error") or data.get("subtype", "").startswith("error"):
            if any(p in result_low for p in RATE_LIMIT_PHRASES) or any(p in stderr_low for p in RATE_LIMIT_PHRASES):
                return ClaudeResult(success=False, output="", error=result_text[:400], error_type="rate_limit")
            if any(p in result_low for p in AUTH_ERROR_PHRASES) or any(p in stderr_low for p in AUTH_ERROR_PHRASES):
                return ClaudeResult(success=False, output="", error=result_text[:400], error_type="auth")
            return ClaudeResult(success=False, output="", error=result_text[:400])

        cost   = data.get("total_cost_usd")
        tokens = data.get("usage", {}).get("output_tokens")
        return ClaudeResult(success=True, output=result_text, cost_usd=cost, tokens_used=tokens)

    except json.JSONDecodeError:
        # Plain text output - check for known error patterns
        raw_low = result.stdout.lower()
        if any(p in raw_low for p in RATE_LIMIT_PHRASES):
            return ClaudeResult(success=False, output="", error=result.stdout[:400], error_type="rate_limit")
        if any(p in raw_low for p in AUTH_ERROR_PHRASES):
            return ClaudeResult(success=False, output="", error=result.stdout[:400], error_type="auth")
        return ClaudeResult(success=True, output=result.stdout)


def probe(model: str) -> tuple[bool, str]:
    """Quick probe to verify the model is reachable and functional."""
    result = run(
        prompt="Reply with exactly: OK",
        working_dir="/tmp",
        model=model,
        budget_usd=0.50,
        timeout=30,
    )
    if not result.success:
        return False, result.error or "Unknown error"
    return True, result.output.strip()
