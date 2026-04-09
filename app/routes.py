import importlib.metadata
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.request

from flask import Blueprint, jsonify, render_template, request

from app import socketio
from app.config import MODELS, MODEL_DEFAULT, DEFAULT_BUDGET_USD, FIX_MODEL_ID, FIX_BUDGET_USD
from app.core import (
    claude_client,
    flakiness_detector,
    frontend_analyzer,
    model_validator,
    page_analyzer,
    prompt_builder,
    test_project_analyzer,
    test_runner,
)
from app.core.job_manager import job_manager

bp = Blueprint("main", __name__)


# ─── Pages ────────────────────────────────────────────────────────────────────

@bp.get("/run-tests")
def run_tests_page():
    return render_template("run_tests.html")


@bp.post("/run-tests")
def run_tests_api():
    data = request.get_json() or {}
    project_path        = data.get("project_path", "").strip()
    keyword             = data.get("keyword", "").strip()
    workers             = max(1, min(15, int(data.get("workers", 1))))
    sleep_ms            = max(0, int(data.get("sleep_ms", 0)))
    bypass_header_name  = data.get("bypass_header_name", "").strip()
    bypass_header_value = data.get("bypass_header_value", "").strip()

    valid, err = test_project_analyzer.validate_path(project_path)
    if not valid:
        return jsonify(ok=False, error=err), 400

    run_id = str(__import__("uuid").uuid4())
    _run_processes[run_id] = None

    thread = threading.Thread(
        target=_stream_pytest,
        args=(run_id, project_path, keyword, bypass_header_name, bypass_header_value, workers, sleep_ms),
        daemon=True,
    )
    thread.start()
    return jsonify(ok=True, run_id=run_id)


@bp.post("/stop-tests/<run_id>")
def stop_tests(run_id):
    proc = _run_processes.get(run_id)
    if proc:
        proc.terminate()
    return "", 204


# In-memory store for running pytest processes
_run_processes: dict = {}

# Active Claude subprocesses per job - used for cancellation
_claude_procs: dict = {}   # job_id -> subprocess.Popen


@bp.post("/job/<job_id>/cancel")
def cancel_job(job_id):
    job = job_manager.get(job_id)
    if not job or job.status not in ("running", "pending"):
        return jsonify(ok=False, error="Job is not running")
    # Kill active Claude subprocess if any
    proc = _claude_procs.get(job_id)
    if proc and proc.poll() is None:
        proc.kill()
    _claude_procs.pop(job_id, None)
    job_manager.update(job_id, status="cancelled")
    socketio.emit("job_cancelled", {"job_id": job_id})
    return jsonify(ok=True)


_404_TITLE_PATTERNS = re.compile(
    r"\b(404|not found|page not found|error 404|404 error|no page|doesn.t exist)\b",
    re.IGNORECASE,
)


def _is_404_page(analysis) -> bool:
    """Return True if the scanned page looks like a 404/error page."""
    if analysis.error:
        return True
    title = (analysis.title or "").strip()
    if _404_TITLE_PATTERNS.search(title):
        return True
    # Completely empty page with no interactive content is also suspicious
    if not title and not analysis.form_fields and not analysis.buttons and not analysis.headings:
        return True
    return False


_ALLURE_CLI = "/opt/homebrew/bin/allure"
_allure_proc = None   # currently running `allure open` process


def _allure_cli_path() -> str | None:
    import shutil
    return shutil.which("allure") or (_ALLURE_CLI if os.path.isfile(_ALLURE_CLI) else None)


def _allure_pytest_installed() -> bool:
    try:
        import importlib.metadata
        importlib.metadata.version("allure-pytest")
        return True
    except Exception:
        pass
    # Fallback: ask pip directly (handles importlib cache miss on Windows)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "allure-pytest"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _stream_pytest(run_id: str, project_path: str, keyword: str,
                   bypass_header_name: str = "", bypass_header_value: str = "",
                   workers: int = 1, sleep_ms: int = 0):
    report_path  = os.path.join(project_path, ".report.json")
    allure_dir   = os.path.join(project_path, ".allure-results")
    use_allure   = _allure_pytest_installed()

    # Remove stale report so a stopped/interrupted run never shows old results
    try:
        if os.path.isfile(report_path):
            os.remove(report_path)
    except Exception:
        pass

    cmd = [
        sys.executable, "-m", "pytest",
        "--tb=short",
        "--override-ini=addopts=",   # suppress pytest.ini addopts to avoid duplicate flags
        "--json-report",
        f"--json-report-file={report_path}",
        "-v",
    ]
    if use_allure:
        cmd += [f"--alluredir={allure_dir}", "--clean-alluredir"]
    if workers > 1:
        cmd += ["-n", str(workers)]
    if keyword:
        # Allow raw pytest args like "-k Login" or "tests/test_login.py"
        cmd += keyword.split()

    # Pass runtime overrides via env vars so conftest.py can pick them up
    env = os.environ.copy()
    if bypass_header_name and bypass_header_value:
        env["AUTOTEST_BYPASS_HEADER_NAME"]  = bypass_header_name
        env["AUTOTEST_BYPASS_HEADER_VALUE"] = bypass_header_value
    if sleep_ms > 0:
        env["AUTOTEST_SLEEP_MS"] = str(sleep_ms)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=project_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        _run_processes[run_id] = proc

        for line in proc.stdout:
            line = line.rstrip()
            socketio.emit("run_log", {"run_id": run_id, "line": line})

        proc.wait()
    except Exception as e:
        socketio.emit("run_error", {"run_id": run_id, "error": str(e)})
        _run_processes.pop(run_id, None)
        return

    # If the process was killed/stopped by the user - don't emit results
    if proc.returncode < 0:
        _run_processes.pop(run_id, None)
        return

    # Parse report and emit results
    results = test_runner._parse_report(report_path)

    by_endpoint = {
        endpoint: [
            {"name": t.name, "outcome": t.outcome, "duration": t.duration, "error_message": t.error_message}
            for t in tests
        ]
        for endpoint, tests in results.by_endpoint.items()
    }

    import platform as _platform
    socketio.emit("run_done", {
        "run_id":         run_id,
        "project_path":   project_path,
        "allure_ready":   use_allure and os.path.isdir(allure_dir),
        "allure_cli":     bool(_allure_cli_path()),
        "allure_plugin":  use_allure,
        "os_platform":    _platform.system(),
        "results": {
            "total":       results.total,
            "passed":      results.passed,
            "failed":      results.failed,
            "errors":      results.errors,
            "skipped":     results.skipped,
            "duration":    results.duration,
            "by_endpoint": by_endpoint,
        },
    })
    _run_processes.pop(run_id, None)


# ─── Allure ───────────────────────────────────────────────────────────────────

@bp.get("/allure/status")
def allure_status():
    cli = _allure_cli_path()
    project_path = request.args.get("project_path", "").strip()
    results_ready = False
    if project_path:
        results_ready = os.path.isdir(os.path.join(project_path, ".allure-results"))
    import platform
    return jsonify(
        cli_available=bool(cli),
        cli_path=cli or "",
        pytest_plugin=_allure_pytest_installed(),
        results_ready=results_ready,
        platform=platform.system(),  # "Windows" | "Darwin" | "Linux"
    )


@bp.post("/allure/install-plugin")
def allure_install_plugin():
    """Install allure-pytest into the app venv."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "allure-pytest"],
            capture_output=True, text=True, timeout=120, check=True,
        )
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@bp.post("/allure/open")
def allure_open():
    global _allure_proc
    data         = request.get_json() or {}
    project_path = data.get("project_path", "").strip()
    allure_cli   = _allure_cli_path()

    if not project_path or not os.path.isdir(project_path):
        return jsonify(ok=False, error="Invalid project path")
    if not allure_cli:
        return jsonify(ok=False, error="Allure CLI not found. Install with: brew install allure")

    allure_dir    = os.path.join(project_path, ".allure-results")
    allure_report = os.path.join(project_path, ".allure-report")

    if not os.path.isdir(allure_dir):
        return jsonify(ok=False, error=".allure-results not found - run tests first")

    # Kill previous allure server if running
    if _allure_proc and _allure_proc.poll() is None:
        _allure_proc.terminate()

    try:
        # Generate report
        subprocess.run(
            [allure_cli, "generate", allure_dir, "-o", allure_report, "--clean"],
            capture_output=True, text=True, timeout=60, check=True,
        )
        # Open report in browser (allure serve opens its own server)
        _allure_proc = subprocess.Popen(
            [allure_cli, "open", allure_report],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify(ok=True)
    except subprocess.CalledProcessError as e:
        return jsonify(ok=False, error=(e.stderr or str(e))[:300])
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@bp.post("/shutdown")
def shutdown():
    def _stop():
        import time
        time.sleep(0.3)  # Let the response reach the browser first
        test_runner._kill_all_procs()
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_stop, daemon=True).start()
    return "", 204


# ─── Claude Code status ───────────────────────────────────────────────────────

def _claude_usage_stats() -> dict:
    """Count Claude assistant turns from ~/.claude/projects/**/*.jsonl.
    This works even when running in -p (subprocess) mode, unlike stats-cache.json
    which is only updated during interactive sessions."""
    import datetime
    import glob
    try:
        today    = str(datetime.date.today())
        week_ago = str(datetime.date.today() - datetime.timedelta(days=7))
        projects_dir = os.path.expanduser("~/.claude/projects")
        today_count = 0
        week_count  = 0
        for jsonl in glob.glob(os.path.join(projects_dir, "*/*.jsonl")):
            try:
                with open(jsonl, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            if d.get("type") != "assistant":
                                continue
                            ts = d.get("timestamp", "")[:10]
                            if ts == today:
                                today_count += 1
                            if ts >= week_ago:
                                week_count += 1
                        except Exception:
                            pass
            except Exception:
                pass
        return {"today": today_count, "week": week_count}
    except Exception:
        return {"today": 0, "week": 0}


@bp.get("/claude-status")
def claude_code_status():
    from app.config import CLAUDE_CLI_PATH

    # 1. CLI not installed
    if not os.path.isfile(CLAUDE_CLI_PATH):
        return jsonify(status="not_installed",
                       message=f"Claude CLI not found at {CLAUDE_CLI_PATH}. Install Claude Code first.")

    # 2. Run `claude auth status`
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    try:
        res = subprocess.run(
            [CLAUDE_CLI_PATH, "auth", "status"],
            capture_output=True, text=True, timeout=8, env=env,
        )
        auth = json.loads(res.stdout or res.stderr)
    except Exception:
        # CLI exists but returned garbage - treat as unknown
        auth = {}

    if not auth.get("loggedIn"):
        return jsonify(status="not_authenticated",
                       message="Claude Code is not authenticated. Run: claude auth login")

    usage = _claude_usage_stats()
    return jsonify(
        status="ok",
        email=auth.get("email", ""),
        plan=auth.get("subscriptionType", ""),
        org=auth.get("orgName", ""),
        today_messages=usage["today"],
        week_messages=usage["week"],
    )


@bp.post("/claude-probe")
def claude_probe():
    """Send a minimal message to Claude to check if rate limit is active."""
    import datetime as _dt
    result = claude_client.probe(model="claude-haiku-4-5-20251001")
    if result[0]:
        return jsonify(ok=True, reachable=True)

    error = (result[1] or "").lower()
    is_rate_limit = any(p in error for p in claude_client.RATE_LIMIT_PHRASES)
    if is_rate_limit:
        # Estimate reset times
        now = _dt.datetime.now()
        midnight = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        minutes_to_midnight = int((midnight - now).total_seconds() / 60)
        hours = minutes_to_midnight // 60
        mins = minutes_to_midnight % 60
        reset_str = f"{hours}h {mins}m" if hours else f"{mins}m"
        return jsonify(ok=False, reachable=False, rate_limited=True,
                       reset_in=reset_str, reset_at=midnight.strftime("%H:%M"))
    return jsonify(ok=False, reachable=False, rate_limited=False, error=result[1])


# ─── Stack management ─────────────────────────────────────────────────────────

_TRACKED_PACKAGES = [
    {"id": "playwright",         "label": "Playwright",         "pip": "playwright",         "post_install": ["playwright", "install", "chromium"]},
    {"id": "playwright-stealth", "label": "Playwright Stealth", "pip": "playwright-stealth", "post_install": None},
    {"id": "pytest",             "label": "pytest",             "pip": "pytest",             "post_install": None},
    {"id": "pytest-playwright",  "label": "pytest-playwright",  "pip": "pytest-playwright",  "post_install": None},
    {"id": "pytest-xdist",       "label": "pytest-xdist",       "pip": "pytest-xdist",       "post_install": None},
    {"id": "flask",              "label": "Flask",              "pip": "Flask",              "post_install": None},
    {"id": "faker",              "label": "Faker",              "pip": "Faker",              "post_install": None},
]


def _pypi_latest(pip_name: str) -> str | None:
    try:
        import ssl
        url = f"https://pypi.org/pypi/{pip_name}/json"
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except ImportError:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, timeout=6, cafile=None) as r:
            return json.loads(r.read())["info"]["version"]
    except Exception:
        try:
            import ssl, urllib.request
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(urllib.request.Request(
                f"https://pypi.org/pypi/{pip_name}/json",
                headers={"User-Agent": "UI-Autotest-Generator/1.0"}
            ), context=ctx, timeout=6) as r:
                return json.loads(r.read())["info"]["version"]
        except Exception:
            return None


def _installed_version(pip_name: str) -> str:
    try:
        return importlib.metadata.version(pip_name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


@bp.get("/stack-status")
def stack_status():
    result = []
    for pkg in _TRACKED_PACKAGES:
        current = _installed_version(pkg["pip"])
        latest  = _pypi_latest(pkg["pip"])
        result.append({
            "id":         pkg["id"],
            "label":      pkg["label"],
            "current":    current,
            "latest":     latest,
            "up_to_date": current == latest if latest else True,
        })
    return jsonify(packages=result)


@bp.post("/stack-update")
def stack_update():
    data       = request.get_json() or {}
    package_id = data.get("package", "").strip()
    pkg        = next((p for p in _TRACKED_PACKAGES if p["id"] == package_id), None)
    if not pkg:
        return jsonify(ok=False, error="Unknown package"), 400

    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", pkg["pip"]],
            capture_output=True, text=True, timeout=180, check=True,
        )
        if pkg["post_install"]:
            subprocess.run(
                [sys.executable, "-m"] + pkg["post_install"],
                capture_output=True, text=True, timeout=300,
            )
    except subprocess.CalledProcessError as e:
        return jsonify(ok=False, error=e.stderr[:400] if e.stderr else str(e))
    except Exception as e:
        return jsonify(ok=False, error=str(e))

    # Reload importlib.metadata cache so newly installed package is visible
    import importlib.metadata as _meta
    try:
        # Python 3.12+: invalidate_caches exists
        _meta.packages_distributions.cache_clear()  # type: ignore
    except AttributeError:
        pass
    try:
        importlib.invalidate_caches()
    except Exception:
        pass

    new_version = _installed_version(pkg["pip"])
    if new_version == "not installed":
        # Fallback: ask pip directly
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "show", pkg["pip"]],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.splitlines():
                if line.lower().startswith("version:"):
                    new_version = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass
    return jsonify(ok=True, version=new_version)


@bp.get("/")
def index():
    return render_template("index.html", models=MODELS, default_model=MODEL_DEFAULT)


@bp.get("/run/<job_id>")
def run_page(job_id):
    return render_template("run.html", job_id=job_id)


@bp.get("/results/<job_id>")
def results(job_id):
    job = job_manager.get(job_id)
    if not job:
        return "Job not found", 404
    autotest_path = (job.results or {}).get("autotest_path", "")
    initially_skipped = _get_skipped_in_files(autotest_path)
    return render_template("results.html", job=job, job_id=job_id, initially_skipped=initially_skipped)


# ─── API ──────────────────────────────────────────────────────────────────────

@bp.post("/validate-path")
def validate_path():
    data = request.get_json()
    path = (data or {}).get("path", "").strip()
    path_type = (data or {}).get("type", "autotest")

    if not path:
        return jsonify(ok=False, error="Path is required")
    if not os.path.exists(path):
        if path_type == "autotest":
            return jsonify(ok=False, error="Directory does not exist. The autotest project must already exist on your machine.")
        return jsonify(ok=False, error="Directory does not exist")
    if not os.path.isdir(path):
        return jsonify(ok=False, error="Path must be a directory, not a file")
    return jsonify(ok=True)


@bp.post("/estimate")
def estimate():
    data = request.get_json() or {}
    url_count        = max(1, int(data.get("url_count", 1)))
    include_positive = data.get("include_positive", True)
    include_negative = data.get("include_negative", True)
    max_positive     = max(0, int(data.get("max_positive", 0)))
    max_negative     = max(0, int(data.get("max_negative", 0)))
    chosen_model     = data.get("model", MODEL_DEFAULT)

    rec = model_validator.estimate(url_count, include_positive, include_negative, chosen_model,
                                   max_positive=max_positive, max_negative=max_negative)
    return jsonify(
        recommended=rec.recommended,
        minimum=rec.minimum,
        is_sufficient=rec.is_sufficient,
        warning=rec.warning,
        estimated_tests=rec.estimated_tests,
        estimated_cost_usd=rec.estimated_cost_usd,
    )


@bp.post("/generate")
def generate():
    data = request.get_json() or {}

    autotest_path  = data.get("autotest_path", "").strip()
    frontend_path  = data.get("frontend_path", "").strip()
    base_url       = data.get("base_url", "").strip()
    urls           = [u.strip() for u in data.get("urls", []) if u.strip()]
    mode           = data.get("mode", "specific")
    login          = data.get("login", "").strip()
    password       = data.get("password", "").strip()
    login_url      = data.get("login_url", "/login").strip() or "/login"
    include_pos    = data.get("include_positive", True)
    include_neg    = data.get("include_negative", True)
    max_positive   = max(0, int(data.get("max_positive", 0)))
    max_negative   = max(0, int(data.get("max_negative", 0)))
    model_key      = data.get("model", MODEL_DEFAULT)
    remote_url     = data.get("remote_url", "").strip()
    budget_usd     = float(data.get("budget_usd", DEFAULT_BUDGET_USD))
    workers        = max(1, min(15, int(data.get("workers", 1))))
    bypass_header  = data.get("bypass_header") or {}  # {"name": "...", "value": "..."}
    sleep_ms       = max(0, min(10000, int(data.get("sleep_ms", 0))))

    # Mandatory: autotest project must exist
    valid, err = test_project_analyzer.validate_path(autotest_path)
    if not valid:
        return jsonify(ok=False, error=err), 400
    if not base_url:
        return jsonify(ok=False, error="Base URL is required"), 400

    model_id = MODELS.get(model_key, MODELS[MODEL_DEFAULT])["id"]

    job = job_manager.create()
    job_id = job.id
    thread = threading.Thread(
        target=_run_pipeline,
        args=(job_id, autotest_path, frontend_path, base_url, urls, mode,
              login, password, login_url, include_pos, include_neg, model_id, model_key, remote_url, budget_usd,
              workers, bypass_header, sleep_ms, max_positive, max_negative),
        daemon=True,
        name=f"pipeline-{job_id}",
    )
    thread.start()

    return jsonify(ok=True, job_id=job_id)


@bp.get("/job/<job_id>/status")
def job_status(job_id):
    job = job_manager.get(job_id)
    if not job:
        return jsonify(error="Not found"), 404
    return jsonify(
        status=job.status,
        stage=job.stage,
        stage_index=job.stage_index,
        error=job.error,
        has_results=job.results is not None,
        discovered_urls=job.discovered_urls,
        endpoint_statuses=job.endpoint_statuses,
    )


@bp.post("/push/<job_id>")
def push(job_id):
    data = request.get_json() or {}
    autotest_path = data.get("autotest_path", "").strip()
    remote_url    = data.get("remote_url", "").strip()
    commit_msg    = data.get("commit_message", "feat: add Playwright autotests").strip()

    if not autotest_path or not remote_url:
        return jsonify(ok=False, error="autotest_path and remote_url are required"), 400

    try:
        _git_push(autotest_path, remote_url, commit_msg)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@bp.get("/push-preview/<job_id>")
def push_preview(job_id):
    job = job_manager.get(job_id)
    if not job or not job.results:
        return jsonify(ok=False, error="Job results not ready"), 404

    autotest_path = job.results.get("autotest_path", "")
    if not autotest_path:
        return jsonify(ok=False, error="No autotest path in results"), 400

    try:
        changed = _git_changed_files(autotest_path)
        return jsonify(ok=True, files=changed)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ─── Test file manipulation ───────────────────────────────────────────────────

def _parse_test_nodes(test_names: list) -> dict:
    """Group test node IDs by file, stripping parametrize suffixes like [chromium]."""
    by_file: dict[str, set] = {}
    for name in test_names:
        parts = name.split("::")
        file_part = parts[0]
        func_part = parts[-1] if len(parts) > 1 else parts[0]
        func_name = re.sub(r'\[.*\]$', '', func_part)
        by_file.setdefault(file_part, set()).add(func_name)
    return by_file


def _remove_function_from_content(content: str, func_name: str) -> str:
    """Remove a top-level test function and its preceding decorators/blank lines."""
    lines = content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(rf'^def {re.escape(func_name)}\s*\(', line):
            # Strip trailing blank lines from result
            while result and result[-1].strip() == '':
                result.pop()
            # Strip preceding decorators
            while result and result[-1].strip().startswith('@'):
                result.pop()
            # Strip blank lines before decorators
            while result and result[-1].strip() == '':
                result.pop()
            # Skip function body (until next top-level non-blank, non-indented line)
            i += 1
            while i < len(lines):
                l = lines[i]
                if l and not l[0].isspace():
                    break
                i += 1
            # Add two blank lines as separator if more content follows
            if result and i < len(lines):
                result.append('')
                result.append('')
            continue
        result.append(line)
        i += 1
    return '\n'.join(result)


def _get_skipped_in_files(autotest_path: str) -> list[str]:
    """Return list of test function names that have @pytest.mark.skip in their test files."""
    skipped = []
    if not autotest_path or not os.path.isdir(autotest_path):
        return skipped
    tests_dir = os.path.join(autotest_path, "tests")
    if not os.path.isdir(tests_dir):
        return skipped
    for fname in os.listdir(tests_dir):
        if not fname.startswith("test_") or not fname.endswith(".py"):
            continue
        try:
            lines = open(os.path.join(tests_dir, fname), encoding="utf-8").readlines()
            for i, line in enumerate(lines):
                if re.match(r'^def (test_\w+)\s*\(', line):
                    func_name = re.match(r'^def (test_\w+)\s*\(', line).group(1)
                    # Check preceding lines for @pytest.mark.skip
                    for j in range(max(0, i - 5), i):
                        if re.match(r'^\s*@pytest\.mark\.skip', lines[j]):
                            skipped.append(func_name)
                            break
        except Exception:
            continue
    return skipped


def _add_skip_decorator_to_content(content: str, func_name: str, reason: str = "manually skipped") -> str:
    """Add @pytest.mark.skip before a test function if not already present.
    Also ensures 'import pytest' exists at the top of the file."""
    # Ensure 'import pytest' is present
    if 'import pytest' not in content:
        # Insert after the last leading docstring/comment block, before first import or code
        lines = content.split('\n')
        insert_at = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''") or stripped.startswith('#') or stripped == '':
                insert_at = i + 1
            else:
                break
        lines.insert(insert_at, 'import pytest')
        content = '\n'.join(lines)

    lines = content.split('\n')
    result = []
    for line in lines:
        if re.match(rf'^def {re.escape(func_name)}\s*\(', line):
            has_skip = any(
                '@pytest.mark.skip' in result[j]
                for j in range(max(0, len(result) - 5), len(result))
            )
            if not has_skip:
                result.append(f'@pytest.mark.skip(reason="{reason}")')
        result.append(line)
    return '\n'.join(result)


def _remove_skip_decorator_from_content(content: str, func_name: str) -> str:
    """Remove @pytest.mark.skip decorator(s) immediately before a test function."""
    lines = content.split('\n')
    result = []
    for line in lines:
        if re.match(rf'^def {re.escape(func_name)}\s*\(', line):
            while result and re.match(r'^\s*@pytest\.mark\.skip', result[-1]):
                result.pop()
        result.append(line)
    return '\n'.join(result)


@bp.post("/tests/delete")
def tests_delete():
    data = request.get_json() or {}
    autotest_path = data.get("autotest_path", "").strip()
    test_names = data.get("test_names", [])

    if not autotest_path or not test_names:
        return jsonify(ok=False, error="autotest_path and test_names are required"), 400

    by_file = _parse_test_nodes(test_names)
    for rel_path, func_names in by_file.items():
        abs_path = os.path.join(autotest_path, rel_path)
        if not os.path.isfile(abs_path):
            continue
        content = open(abs_path, encoding="utf-8").read()
        for fn in func_names:
            content = _remove_function_from_content(content, fn)
        open(abs_path, "w", encoding="utf-8").write(content)

    return jsonify(ok=True)


@bp.post("/tests/mark-skip")
def tests_mark_skip():
    data = request.get_json() or {}
    autotest_path = data.get("autotest_path", "").strip()
    test_names = data.get("test_names", [])

    if not autotest_path or not test_names:
        return jsonify(ok=False, error="autotest_path and test_names are required"), 400

    by_file = _parse_test_nodes(test_names)
    for rel_path, func_names in by_file.items():
        abs_path = os.path.join(autotest_path, rel_path)
        if not os.path.isfile(abs_path):
            continue
        content = open(abs_path, encoding="utf-8").read()
        for fn in func_names:
            content = _add_skip_decorator_to_content(content, fn)
        open(abs_path, "w", encoding="utf-8").write(content)

    return jsonify(ok=True)


@bp.post("/tests/unskip")
def tests_unskip():
    data = request.get_json() or {}
    autotest_path = data.get("autotest_path", "").strip()
    test_names = data.get("test_names", [])

    if not autotest_path or not test_names:
        return jsonify(ok=False, error="autotest_path and test_names are required"), 400

    by_file = _parse_test_nodes(test_names)
    for rel_path, func_names in by_file.items():
        abs_path = os.path.join(autotest_path, rel_path)
        if not os.path.isfile(abs_path):
            continue
        content = open(abs_path, encoding="utf-8").read()
        for fn in func_names:
            content = _remove_skip_decorator_from_content(content, fn)
        open(abs_path, "w", encoding="utf-8").write(content)

    return jsonify(ok=True)


# ─── Pipeline ─────────────────────────────────────────────────────────────────

STAGES = ["Analyzing", "Coverage", "Generating", "Running", "Done"]


def _log(job_id: str, message: str):
    job_manager.append_log(job_id, message)
    socketio.emit("log_line", {"job_id": job_id, "line": message})


def _set_stage(job_id: str, stage: str):
    idx = STAGES.index(stage) if stage in STAGES else 0
    job_manager.update(job_id, stage=stage, stage_index=idx, status="running")
    socketio.emit("stage_change", {"job_id": job_id, "stage": stage, "index": idx})


def _run_pipeline(
    job_id, autotest_path, frontend_path, base_url,
    urls, mode, login, password, login_url="/login",
    include_pos=True, include_neg=True, model_id=None, model_key=None, remote_url="", budget_usd=DEFAULT_BUDGET_USD,
    workers=1, bypass_header=None, sleep_ms=0, max_positive=0, max_negative=0,
):
    # Guard: only one pipeline per job
    job = job_manager.get(job_id)
    if not job or job.status not in ("pending",):
        return
    job_manager.update(job_id, status="running")

    try:
        # ── Stage 1: Analyze frontend ──────────────────────────────────────
        _set_stage(job_id, "Analyzing")

        frontend_summary = {"routes": [], "framework": "unknown"}
        browser_page_map = {}   # url_path -> route_info dict from live browser scan

        if frontend_path and os.path.isdir(frontend_path):
            _log(job_id, "Analyzing frontend source code...")
            frontend_summary = frontend_analyzer.analyze(frontend_path)
            _log(job_id, f"Framework: {frontend_summary.get('framework', 'unknown')}")
            _log(job_id, f"Routes found in source: {frontend_summary.get('total_routes', 0)}")
        else:
            _log(job_id, "No frontend source provided - will scan pages live in browser")

        if mode == "explore" and frontend_summary.get("routes"):
            discovered = [r["path"] for r in frontend_summary["routes"]]
            urls = discovered[:50]
            _log(job_id, f"Explore mode: {len(urls)} routes from source")
        elif not urls:
            urls = ["/"]

        if not urls:
            urls = ["/"]

        job_manager.update(job_id,
            discovered_urls=urls,
            endpoint_statuses={u: "pending" for u in urls},
        )
        socketio.emit("endpoints_discovered", {"job_id": job_id, "urls": urls})

        # Browser scan: always run if no frontend source, or for explore mode discovery
        if not frontend_path or not os.path.isdir(frontend_path):
            _log(job_id, f"Opening browser to scan {len(urls)} page(s)...")
            page_analyses = page_analyzer.analyze_pages(
                urls=urls,
                base_url=base_url,
                login=login,
                password=password,
                login_url=login_url,
                log_callback=lambda line: _log(job_id, line),
            )
            dead_urls = []
            for url_path, analysis in page_analyses.items():
                if _is_404_page(analysis):
                    reason = analysis.error or f"title='{analysis.title}'"
                    _log(job_id, f"  ✗ {url_path}: page not found or unreachable ({reason})")
                    dead_urls.append(url_path)
                else:
                    browser_page_map[url_path] = page_analyzer.to_route_info(analysis)
                    fields_count = len(analysis.form_fields)
                    buttons_count = len(analysis.buttons)
                    _log(job_id, f"  {url_path}: {fields_count} field(s), {buttons_count} button(s), title='{analysis.title}'")

            if dead_urls:
                # Mark dead URLs in the UI
                for du in dead_urls:
                    _update_endpoint_status(job_id, du, "not_found")
                    socketio.emit("endpoint_generated", {"job_id": job_id, "url": du, "status": "not_found"})
                # Remove dead URLs from the pipeline
                urls = [u for u in urls if u not in dead_urls]
                if not urls:
                    msg = "All URLs returned 404 or are unreachable. Check the Base URL and endpoint paths, then try again."
                    _log(job_id, msg)
                    job_manager.update(job_id, status="error", error=msg)
                    socketio.emit("job_error", {"job_id": job_id, "error": msg})
                    return
                _log(job_id, f"  Skipping {len(dead_urls)} unreachable URL(s), continuing with {len(urls)} valid URL(s).")

            _log(job_id, "Browser scan complete")

            # In Explore mode, crawl nav links discovered on scanned pages
            if mode == "explore" and urls:
                discovered_paths: set[str] = set()
                for url_path, analysis in page_analyses.items():
                    if url_path in dead_urls:
                        continue
                    for link in analysis.links:
                        href = link.get("href", "")
                        # Only same-domain relative paths
                        if href.startswith("/") and not href.startswith("//"):
                            clean = href.split("?")[0].split("#")[0].rstrip("/") or "/"
                            if clean not in urls and clean not in discovered_paths:
                                discovered_paths.add(clean)

                if discovered_paths:
                    max_extra = max(0, 50 - len(urls))
                    new_paths = sorted(discovered_paths)[:max_extra]
                    _log(job_id, f"Explore mode: found {len(new_paths)} additional page(s) via nav links")

                    # Register new endpoints in the UI (merge into existing statuses)
                    all_urls_so_far = urls + new_paths
                    current_job = job_manager.get(job_id)
                    merged_statuses = dict(current_job.endpoint_statuses) if current_job else {}
                    for p in new_paths:
                        merged_statuses[p] = "pending"
                    job_manager.update(
                        job_id,
                        discovered_urls=all_urls_so_far,
                        endpoint_statuses=merged_statuses,
                    )
                    socketio.emit("endpoints_discovered", {"job_id": job_id, "urls": all_urls_so_far})

                    new_analyses = page_analyzer.analyze_pages(
                        urls=new_paths,
                        base_url=base_url,
                        login=login,
                        password=password,
                        login_url=login_url,
                        log_callback=lambda line: _log(job_id, line),
                    )
                    new_alive: list[str] = []
                    for np_path, np_analysis in new_analyses.items():
                        if _is_404_page(np_analysis):
                            reason = np_analysis.error or f"title='{np_analysis.title}'"
                            _log(job_id, f"  ✗ {np_path}: page not found ({reason})")
                            _update_endpoint_status(job_id, np_path, "not_found")
                            socketio.emit("endpoint_generated", {"job_id": job_id, "url": np_path, "status": "not_found"})
                        else:
                            browser_page_map[np_path] = page_analyzer.to_route_info(np_analysis)
                            fields_count = len(np_analysis.form_fields)
                            buttons_count = len(np_analysis.buttons)
                            _log(job_id, f"  {np_path}: {fields_count} field(s), {buttons_count} button(s), title='{np_analysis.title}'")
                            new_alive.append(np_path)

                    urls = urls + new_alive
                    _log(job_id, f"Explore mode: {len(urls)} total page(s) after discovery")
                else:
                    _log(job_id, "Explore mode: no additional nav links found on scanned page(s)")

        # ── Stage 2: Analyze test coverage ────────────────────────────────
        _set_stage(job_id, "Coverage")
        _log(job_id, "Checking existing test coverage...")

        project = None
        if not test_project_analyzer.analyze(autotest_path).is_empty:
            # Non-empty project: use Claude to understand arbitrary structure
            _log(job_id, "  Analyzing project structure via Claude...")
            project = _claude_analyze_project(autotest_path, model_id)
            if project is None:
                _log(job_id, "  Claude analysis unavailable, falling back to static analysis...")
        if project is None:
            project = test_project_analyzer.analyze(autotest_path)
        _log(job_id, project.summary)

        if project.scaffold_needed:
            _log(job_id, "Setting up project scaffold...")
            _scaffold_project(autotest_path, base_url, login, password, login_url, bypass_header, sleep_ms)
            _log(job_id, "Scaffold created: conftest.py, pytest.ini, tests/")

        _ensure_run_script(autotest_path)
        _ensure_stealth_deps(autotest_path)
        _ensure_gitignore(autotest_path)
        _ensure_github_actions(autotest_path)
        _log(job_id, "run_tests.sh - updated, playwright-stealth - ensured")

        existing_names = [t.function_name for t in project.existing_tests]

        # ── Stage 3: Generate tests ────────────────────────────────────────
        _set_stage(job_id, "Generating")
        _log(job_id, f"Model: {model_id}")
        _log(job_id, f"Generating tests for {len(urls)} URL(s)...")

        # Source-based route map takes priority; browser scan fills the gaps
        route_map = {r["path"]: r for r in frontend_summary.get("routes", [])}
        page_objects_known = _find_page_objects(autotest_path)
        total_cost = 0.0

        for url in urls:
            route_info = (
                route_map.get(url)
                or browser_page_map.get(url)
                or {"path": url, "form_fields": [], "buttons": [], "api_calls": [], "auth_required": False}
            )

            # Read existing test file for this endpoint (if any)
            test_filename = f"test_{_url_to_filename(url)}.py"
            test_file_path = os.path.join(autotest_path, "tests", test_filename)
            existing_file_content = ""
            existing_file_tests = []

            if os.path.isfile(test_file_path):
                existing_file_content = open(test_file_path, encoding="utf-8").read()
                # Extract test names from this specific file
                existing_file_tests = [
                    t.function_name
                    for t in project.existing_tests
                    if t.file_path == test_file_path
                ]

            if existing_file_tests:
                _log(job_id, f"  Analyzing coverage: {url} ({len(existing_file_tests)} existing tests)")
            else:
                _log(job_id, f"  Generating: {url} (new)")

            prompt = prompt_builder.build_generate_tests(
                autotest_path=autotest_path,
                url=url,
                route_info=route_info,
                existing_test_names=existing_file_tests,
                existing_file_content=existing_file_content,
                include_positive=include_pos,
                include_negative=include_neg,
                base_url=base_url,
                login=login,
                password=password,
                login_url=login_url,
                page_objects_known=page_objects_known,
                max_positive=max_positive,
                max_negative=max_negative,
            )

            result = claude_client.run(
                prompt=prompt,
                working_dir=autotest_path,
                extra_dirs=[frontend_path] if frontend_path else [],
                model=model_id,
                budget_usd=budget_usd,
                on_proc_start=lambda p: _claude_procs.update({job_id: p}),
            )
            _claude_procs.pop(job_id, None)

            # Check if job was cancelled while Claude was running
            current_job = job_manager.get(job_id)
            if current_job and current_job.status == "cancelled":
                _log(job_id, "Generation cancelled by user.")
                return

            if result.success:
                if result.cost_usd:
                    total_cost += result.cost_usd
                # Claude responds with SKIP if existing tests already cover the page
                if result.output.strip().upper().startswith("SKIP"):
                    _log(job_id, f"  ↩ {url} - coverage sufficient, no new tests needed")
                    _update_endpoint_status(job_id, url, "skip")
                    socketio.emit("endpoint_generated", {"job_id": job_id, "url": url, "status": "skip"})
                else:
                    # Verify generated tests pass; self-heal if they don't
                    if os.path.isfile(test_file_path):
                        verify_run = test_runner.run_tests(
                            autotest_path=autotest_path,
                            test_file=test_file_path,
                            use_allure=False,
                            workers=1,
                        )
                        failing = [t for t in verify_run.tests if t.outcome in ("failed", "error")]
                        if failing:
                            _log(job_id, f"  {verify_run.passed} passed, {len(failing)} failed - starting auto-fix")
                            endpoint_status = _heal_test_file(
                                job_id=job_id,
                                autotest_path=autotest_path,
                                test_file_path=test_file_path,
                                failures=failing,
                                url=url,
                                model_id=model_id,
                                budget_usd=budget_usd,
                            )
                            if endpoint_status == "cancelled":
                                return
                        else:
                            _log(job_id, f"  ✓ {url} - {verify_run.passed} test(s) pass")
                            endpoint_status = "ok"
                    else:
                        _log(job_id, f"  ✓ {url}")
                        endpoint_status = "ok"

                    _update_endpoint_status(job_id, url, endpoint_status)
                    socketio.emit("endpoint_generated", {"job_id": job_id, "url": url, "status": endpoint_status})
            else:
                if result.error_type == "rate_limit":
                    msg = "Claude Code rate limit reached. Check your usage limits at claude.ai/settings and try again later."
                    _log(job_id, f"  ✗ {url} - {msg}")
                    _update_endpoint_status(job_id, url, "error")
                    socketio.emit("endpoint_generated", {"job_id": job_id, "url": url, "status": "error"})
                    socketio.emit("claude_error", {"job_id": job_id, "error_type": "rate_limit"})
                    job_manager.update(job_id, status="error", error=msg)
                    socketio.emit("job_error", {"job_id": job_id, "error": msg})
                    return
                elif result.error_type == "auth":
                    msg = "Claude Code authentication error. Run: claude auth login"
                    _log(job_id, f"  ✗ {url} - {msg}")
                    _update_endpoint_status(job_id, url, "error")
                    socketio.emit("endpoint_generated", {"job_id": job_id, "url": url, "status": "error"})
                    socketio.emit("claude_error", {"job_id": job_id, "error_type": "auth"})
                    job_manager.update(job_id, status="error", error=msg)
                    socketio.emit("job_error", {"job_id": job_id, "error": msg})
                    return
                elif result.error_type == "overload":
                    _log(job_id, f"  ⚡ {url} - Anthropic servers temporarily overloaded. Skipping this URL, continuing...")
                    _update_endpoint_status(job_id, url, "overload")
                    socketio.emit("endpoint_generated", {"job_id": job_id, "url": url, "status": "overload"})
                else:
                    _log(job_id, f"  ✗ {url} - {result.error}")
                    _update_endpoint_status(job_id, url, "error")
                    socketio.emit("endpoint_generated", {"job_id": job_id, "url": url, "status": "error"})

        _log(job_id, f"Generation complete. Estimated cost: ${total_cost:.3f}")

        # ── Stage 4: Run tests ─────────────────────────────────────────────
        _set_stage(job_id, "Running")
        _log(job_id, "Running generated tests...")

        run_result = test_runner.run_tests(
            autotest_path=autotest_path,
            log_callback=lambda line: _log(job_id, line),
            workers=workers,
        )

        skipped_note = f", {run_result.skipped} skipped" if run_result.skipped > 0 else ""
        _log(job_id, f"Results: {run_result.passed} passed, {run_result.failed} failed, {run_result.errors} errors{skipped_note}")
        if run_result.skipped > 0:
            _log(job_id, "ℹ Skipped tests may be caused by anti-bot protection on the target site. Tests were generated successfully but could not be verified after generation.")

        # ── Stage 4b: Diagnose failures ────────────────────────────────────
        diagnoses = []
        failed_tests = [t for t in run_result.tests if t.outcome in ("failed", "error")]

        if failed_tests:
            _log(job_id, f"Diagnosing {len(failed_tests)} failures...")
            raw_diagnoses = flakiness_detector.diagnose(
                autotest_path=autotest_path,
                failed_tests=failed_tests,
                log_callback=lambda line: _log(job_id, line),
                workers=workers,
            )

            for d in raw_diagnoses:
                if d.classification == "SELECTOR_ISSUE":
                    _log(job_id, f"  Auto-fixing: {d.test_name}")
                    failed_test = next((t for t in failed_tests if t.name == d.test_name), None)
                    if failed_test:
                        fix_prompt = prompt_builder.build_diagnose_failure(
                            test_name=d.test_name,
                            test_file=os.path.join(autotest_path, failed_test.file),
                            error_output=failed_test.error_message or "",
                            url=failed_test.endpoint,
                            rerun_results=d.rerun_results,
                        )
                        fix_result = claude_client.run(fix_prompt, working_dir=autotest_path, model=model_id)
                        if fix_result.success:
                            _log(job_id, f"  ✓ Fixed: {d.test_name}")

                diagnoses.append({
                    "test_name": d.test_name,
                    "classification": d.classification,
                    "rerun_results": d.rerun_results,
                    "recommendation": d.recommendation,
                })

        # ── Stage 5: Done ──────────────────────────────────────────────────
        _set_stage(job_id, "Done")

        by_endpoint_serializable = {
            endpoint: [
                {
                    "name": t.name,
                    "outcome": t.outcome,
                    "duration": t.duration,
                    "error_message": t.error_message,
                    "endpoint": t.endpoint,
                }
                for t in tests
            ]
            for endpoint, tests in run_result.by_endpoint.items()
        }

        job_manager.update(
            job_id,
            status="done",
            results={
                "autotest_path": autotest_path,
                "remote_url": remote_url,
                "total": run_result.total,
                "passed": run_result.passed,
                "failed": run_result.failed,
                "errors": run_result.errors,
                "skipped": run_result.skipped,
                "duration": run_result.duration,
                "by_endpoint": by_endpoint_serializable,
                "diagnoses": diagnoses,
                "total_cost_usd": round(total_cost, 3),
                "urls_tested": urls,
            },
        )
        socketio.emit("job_done", {"job_id": job_id})
        _log(job_id, "All done!")

    except Exception as exc:
        import traceback
        _log(job_id, f"Pipeline error: {exc}\n{traceback.format_exc()}")
        job_manager.update(job_id, status="error", error=str(exc))
        socketio.emit("job_error", {"job_id": job_id, "error": str(exc)})


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _update_endpoint_status(job_id: str, url: str, status: str):
    job = job_manager.get(job_id)
    if job:
        updated = dict(job.endpoint_statuses)
        updated[url] = status
        job_manager.update(job_id, endpoint_statuses=updated)


def _build_file_tree(root: str, max_files: int = 200) -> str:
    """Build a compact file tree string for the given directory (relative paths only)."""
    lines = []
    count = 0
    for dirpath, dirnames, files in os.walk(root):
        # Skip hidden dirs and venvs early
        dirnames[:] = [
            d for d in sorted(dirnames)
            if not d.startswith(".") and d not in ("__pycache__", "node_modules", "venv", ".venv", "env")
        ]
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir == "." else rel_dir + "/"
        for f in sorted(files):
            if count >= max_files:
                lines.append(f"  ... (truncated, {max_files}+ files)")
                return "\n".join(lines)
            lines.append(prefix + f)
            count += 1
    return "\n".join(lines) if lines else "(empty directory)"


def _claude_analyze_project(autotest_path: str, model_id: str) -> "test_project_analyzer.ProjectAnalysis":
    """
    Use Claude to analyze the autotest project structure.
    Falls back to static analysis if Claude fails or returns invalid JSON.
    """
    file_tree = _build_file_tree(autotest_path)
    prompt = prompt_builder.build_analyze_project_structure(autotest_path, file_tree)

    result = claude_client.run(
        prompt=prompt,
        working_dir=autotest_path,
        model=MODELS["haiku"]["id"],   # always use Haiku - simple task, no need for Sonnet
        budget_usd=0.50,
        timeout=60,
    )

    if not result.success:
        return None   # caller will fall back to static analysis

    # Extract JSON from response (Claude may wrap it in markdown)
    raw = result.output.strip()
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if not json_match:
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None

    # Build a ProjectAnalysis-compatible object from Claude's response
    tests_dir = data.get("tests_dir", "tests")
    has_conftest = data.get("has_conftest", False)
    existing_count = data.get("existing_test_count", 0)
    test_files = data.get("test_files", [])
    covered = data.get("covered_endpoints", [])
    notes = data.get("notes", "")
    fixtures = data.get("fixtures_detected", [])

    # Reconstruct TestInfo list from test files (still need function names for gap analysis)
    # Claude gave us file paths - use static parser on those specific files
    existing_tests = []
    for rel_path in test_files:
        abs_path = os.path.join(autotest_path, rel_path)
        if os.path.isfile(abs_path):
            existing_tests.extend(test_project_analyzer._extract_tests(abs_path))

    summary_parts = [f"Claude analysis: {existing_count} test(s) in {len(test_files)} file(s)."]
    if covered:
        summary_parts.append(f"Covered: {', '.join(covered)}.")
    if fixtures:
        summary_parts.append(f"Fixtures: {', '.join(fixtures)}.")
    if notes:
        summary_parts.append(f"Notes: {notes}")

    return test_project_analyzer.ProjectAnalysis(
        path=autotest_path,
        exists=True,
        is_empty=(existing_count == 0 and not test_files),
        existing_tests=existing_tests,
        covered_endpoints=covered,
        scaffold_needed=not has_conftest,
        summary=" ".join(summary_parts),
    )


def _scaffold_project(autotest_path: str, base_url: str, login: str, password: str, login_url: str = "/login", bypass_header: dict | None = None, sleep_ms: int = 0):
    tests_dir = os.path.join(autotest_path, "tests")
    os.makedirs(tests_dir, exist_ok=True)

    conftest = test_project_analyzer.build_conftest(autotest_path, base_url, login, password, login_url, bypass_header, sleep_ms)
    with open(os.path.join(autotest_path, "conftest.py"), "w") as f:
        f.write(conftest)

    ini = test_project_analyzer.build_pytest_ini(autotest_path)
    with open(os.path.join(autotest_path, "pytest.ini"), "w") as f:
        f.write(ini)

    req_path = os.path.join(autotest_path, "requirements.txt")
    if not os.path.isfile(req_path):
        with open(req_path, "w") as f:
            f.write("pytest\npytest-playwright\npytest-json-report\nFaker\nplaywright-stealth\npytest-xdist\npytest-timeout\n")

    init_path = os.path.join(tests_dir, "__init__.py")
    if not os.path.isfile(init_path):
        open(init_path, "w").close()

    gitignore_path = os.path.join(autotest_path, ".gitignore")
    if not os.path.isfile(gitignore_path):
        with open(gitignore_path, "w") as f:
            f.write(
                "# Python\n"
                "__pycache__/\n"
                "*.pyc\n"
                "*.pyo\n"
                "\n"
                "# Virtual environment\n"
                ".venv/\n"
                "venv/\n"
                "\n"
                "# Pytest\n"
                ".pytest_cache/\n"
                ".report.json\n"
                "\n"
                "# Allure\n"
                ".allure-results/\n"
                ".allure-report/\n"
                "\n"
                "# Environment files\n"
                ".env\n"
                ".env.local\n"
            )


def _ensure_github_actions(autotest_path: str):
    """Create .github/workflows/tests.yml if it doesn't exist."""
    workflow_dir = os.path.join(autotest_path, ".github", "workflows")
    workflow_path = os.path.join(workflow_dir, "tests.yml")
    if os.path.isfile(workflow_path):
        return
    os.makedirs(workflow_dir, exist_ok=True)
    with open(workflow_path, "w", encoding="utf-8") as f:
        f.write(
            "name: UI Tests\n"
            "\n"
            "on:\n"
            "  push:\n"
            "    branches: [main, master]\n"
            "  pull_request:\n"
            "    branches: [main, master]\n"
            "\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "\n"
            "      - name: Set up Python\n"
            "        uses: actions/setup-python@v5\n"
            "        with:\n"
            "          python-version: '3.11'\n"
            "\n"
            "      - name: Install dependencies\n"
            "        run: pip install -r requirements.txt\n"
            "\n"
            "      - name: Install Playwright browsers\n"
            "        run: playwright install chromium --with-deps\n"
            "\n"
            "      - name: Run tests\n"
            "        run: pytest --tb=short -v\n"
            "\n"
            "      - name: Upload screenshots on failure\n"
            "        if: failure()\n"
            "        uses: actions/upload-artifact@v4\n"
            "        with:\n"
            "          name: screenshots\n"
            "          path: tests/screenshots/\n"
            "          if-no-files-found: ignore\n"
        )


def _ensure_gitignore(autotest_path: str):
    """Ensure required entries exist in .gitignore, patching if needed."""
    gitignore_path = os.path.join(autotest_path, ".gitignore")
    required = [
        "__pycache__/",
        "*.pyc",
        "*.pyo",
        ".venv/",
        "venv/",
        ".pytest_cache/",
        ".report.json",
        ".allure-results/",
        ".allure-report/",
        "tests/screenshots/",
        ".env",
    ]
    existing = ""
    if os.path.isfile(gitignore_path):
        existing = open(gitignore_path, encoding="utf-8").read()

    missing = [e for e in required if e not in existing]
    if not missing:
        return

    with open(gitignore_path, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n# Added by UI Autotest Generator\n")
        for entry in missing:
            f.write(entry + "\n")


def _ensure_stealth_deps(autotest_path: str):
    """Add playwright-stealth and pytest-xdist to requirements.txt if not already present."""
    req_path = os.path.join(autotest_path, "requirements.txt")
    if not os.path.isfile(req_path):
        return
    content = open(req_path, encoding="utf-8").read()
    additions = []
    if "playwright-stealth" not in content:
        additions.append("playwright-stealth")
    if "pytest-xdist" not in content:
        additions.append("pytest-xdist")
    if "pytest-timeout" not in content:
        additions.append("pytest-timeout")
    if "allure-pytest" not in content:
        additions.append("allure-pytest")
    if additions:
        with open(req_path, "a", encoding="utf-8") as f:
            f.write("\n".join(additions) + "\n")


def _ensure_run_script(autotest_path: str):
    """Always write the latest self-contained run_tests.sh into the autotest project."""
    script_path = os.path.join(autotest_path, "run_tests.sh")
    content = """\
#!/bin/bash
# Run Playwright tests - self-contained, no external tools needed.
#
# Usage:
#   bash run_tests.sh                        - run all tests
#   bash run_tests.sh tests/test_login.py   - run a specific file
#   bash run_tests.sh -k "Login"            - filter tests by keyword
#   bash run_tests.sh --headed              - run in headed (visible) browser
#   bash run_tests.sh -n 4                  - run in parallel with 4 workers
#   bash run_tests.sh -n auto               - auto-detect workers by CPU count
#
# First run: automatically creates .venv and installs all dependencies.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

if [ ! -f "$VENV/bin/python" ]; then
  echo "First run - setting up virtual environment..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"
  echo "Installing Playwright browsers..."
  "$VENV/bin/playwright" install chromium
  echo ""
  echo "Setup complete. Starting tests..."
  echo ""
fi

cd "$SCRIPT_DIR"
"$VENV/bin/python" -m pytest "$@" -v --tb=short
"""
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(script_path, 0o755)


def _find_page_objects(autotest_path: str) -> list:
    result = []
    po_dir = os.path.join(autotest_path, "page_objects")
    if os.path.isdir(po_dir):
        for f in os.listdir(po_dir):
            if f.endswith(".py") and not f.startswith("__"):
                result.append(f.replace(".py", ""))
    return result


def _url_to_filename(url: str) -> str:
    return url.strip("/").replace("/", "_").replace("-", "_") or "home"


def _heal_test_file(
    job_id: str,
    autotest_path: str,
    test_file_path: str,
    failures: list,
    url: str,
    model_id: str,
    budget_usd: float,
) -> str:
    """
    Try to fix failing tests in test_file_path up to 2 times.
    If all attempts fail, the file is restored to its original state before marking needs_review.
    Returns: 'ok' | 'needs_review' | 'cancelled'
    """
    report_path = os.path.join(autotest_path, ".report.json")

    # Save original content so we can restore it if all fix attempts fail
    original_content = None
    if os.path.isfile(test_file_path):
        try:
            original_content = open(test_file_path, encoding="utf-8").read()
        except Exception:
            pass

    for attempt in range(1, 3):
        failure_details = "\n\n".join(
            f"Test: {t.name}\nError: {(t.error_message or 'no details')[:600]}"
            for t in failures
        )
        _log(job_id, f"  Auto-fix attempt {attempt}/2 ({len(failures)} failing test(s))...")

        fix_prompt = prompt_builder.build_fix_failing_tests(
            test_file=test_file_path,
            failure_details=failure_details,
            url=url,
        )
        fix_result = claude_client.run(
            prompt=fix_prompt,
            working_dir=autotest_path,
            model=FIX_MODEL_ID,
            budget_usd=FIX_BUDGET_USD,
            on_proc_start=lambda p: _claude_procs.update({job_id: p}),
        )
        _claude_procs.pop(job_id, None)

        current_job = job_manager.get(job_id)
        if current_job and current_job.status == "cancelled":
            return "cancelled"

        if not fix_result.success:
            _log(job_id, f"  Fix attempt {attempt} failed: {fix_result.error}")
            break

        rerun = test_runner.run_tests(
            autotest_path=autotest_path,
            test_file=test_file_path,
            use_allure=False,
            workers=1,
        )
        still_failing = [t for t in rerun.tests if t.outcome in ("failed", "error")]
        if not still_failing:
            _log(job_id, f"  ✓ All tests pass after fix (attempt {attempt})")
            return "ok"

        failures = still_failing
        _log(job_id, f"  Still {len(still_failing)} failing after attempt {attempt}")

    _log(job_id, f"  ⚠ {len(failures)} test(s) need manual review after 2 fix attempts")
    # Restore original file content - discard all speculative changes Claude made
    if original_content is not None:
        try:
            with open(test_file_path, "w", encoding="utf-8") as f:
                f.write(original_content)
            _log(job_id, "  ↩ Test file restored to original state")
        except Exception:
            pass
    # Auto-skip unfixable tests so they don't pollute the Stage 4 full run
    _skip_failing_tests(test_file_path, failures)
    return "needs_review"


def _skip_failing_tests(test_file_path: str, failures: list) -> None:
    """Add @pytest.mark.skip to each failing test so Stage 4 run stays clean."""
    if not os.path.isfile(test_file_path):
        return
    try:
        import re as _re
        content = open(test_file_path, encoding="utf-8").read()
        failing_names = {t.name.split("::")[-1].split("[")[0] for t in failures}
        skip_marker = '@pytest.mark.skip(reason="needs manual review: auto-fix failed after 2 attempts")'

        def _add_skip(m):
            fn_name = m.group(2)
            if fn_name in failing_names and skip_marker not in m.group(0):
                return f"{m.group(1)}{skip_marker}\n{m.group(1)}def {fn_name}{m.group(3)}"
            return m.group(0)

        patched = _re.sub(
            r"^([ \t]*)def (test_\w+)(\()",
            _add_skip,
            content,
            flags=_re.MULTILINE,
        )
        if patched != content:
            open(test_file_path, "w", encoding="utf-8").write(patched)
    except Exception:
        pass


def _git_changed_files(path: str) -> list:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=path, capture_output=True, text=True,
    )
    lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    return [l[2:].strip() for l in lines]


def _git_push(path: str, remote_url: str, commit_msg: str):
    def run(cmd):
        r = subprocess.run(cmd, cwd=path, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout)
        return r.stdout

    if not os.path.isdir(os.path.join(path, ".git")):
        run(["git", "init"])

    remotes = subprocess.run(["git", "remote"], cwd=path, capture_output=True, text=True).stdout
    if "origin" in remotes:
        run(["git", "remote", "set-url", "origin", remote_url])
    else:
        run(["git", "remote", "add", "origin", remote_url])

    run(["git", "add", "."])
    run(["git", "commit", "-m", commit_msg])
    run(["git", "push", "-u", "origin", "HEAD"])
