"""
Runs pytest in the autotest project and parses JSON report results.
"""

import atexit
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Optional

# Global registry of all active pytest subprocesses - killed on app exit
_active_procs: list[subprocess.Popen] = []
_procs_lock = threading.Lock()


def _kill_all_procs():
    with _procs_lock:
        for p in _active_procs:
            try:
                p.kill()
            except Exception:
                pass
        _active_procs.clear()


atexit.register(_kill_all_procs)


@dataclass
class TestResult:
    name: str
    outcome: str          # passed | failed | error | skipped
    duration: float
    file: str
    error_message: Optional[str] = None
    endpoint: str = ""


@dataclass
class RunResult:
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    duration: float
    tests: list[TestResult] = field(default_factory=list)
    by_endpoint: dict = field(default_factory=dict)


def _allure_pytest_installed() -> bool:
    try:
        import importlib.metadata
        importlib.metadata.version("allure-pytest")
        return True
    except Exception:
        return False


def run_tests(
    autotest_path: str,
    test_file: Optional[str] = None,
    specific_nodes: Optional[list] = None,
    log_callback=None,
    workers: int = 1,
    timeout_ms: Optional[int] = None,
    use_allure: bool = True,
) -> RunResult:
    report_path = os.path.join(autotest_path, ".report.json")
    allure_dir  = os.path.join(autotest_path, ".allure-results")

    # Remove stale report so an interrupted run never shows old results
    try:
        if os.path.isfile(report_path):
            os.remove(report_path)
    except Exception:
        pass

    # Ensure pytest.ini exists
    ini_path = os.path.join(autotest_path, "pytest.ini")
    if not os.path.isfile(ini_path):
        _write_pytest_ini(autotest_path)

    cmd = [
        sys.executable, "-m", "pytest",   # use same python as Flask (our venv)
        "--json-report",
        f"--json-report-file={report_path}",
        "-v",
        "--tb=short",
        "--override-ini=addopts=",         # ignore pytest.ini addopts to avoid duplicate flags
        "--browser", "chromium",           # explicit browser to prevent duplicate parametrization
        "--browser-channel", "chrome",     # use system Chrome (avoids downloading Playwright Chromium)
    ]
    if use_allure and _allure_pytest_installed():
        cmd += [f"--alluredir={allure_dir}", "--clean-alluredir"]
    if workers > 1:
        cmd += ["-n", str(workers)]
    # Note: pytest-timeout is not used here because Playwright ignores Python signals.
    # Hard timeout is enforced at the subprocess level via proc.wait(timeout=...).
    if specific_nodes:
        cmd += specific_nodes              # rerun specific test node IDs only
    elif test_file:
        cmd.append(test_file)

    # Hard subprocess timeout: per-test limit × count + 30s buffer.
    # For reruns timeout_ms is set; for full runs use a generous ceiling.
    if timeout_ms and specific_nodes:
        hard_timeout = (timeout_ms / 1000) * len(specific_nodes) + 30
    else:
        hard_timeout = None  # no limit for normal full runs

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=autotest_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with _procs_lock:
            _active_procs.append(proc)

        def _read_output():
            for line in proc.stdout:
                line = line.rstrip()
                if log_callback:
                    log_callback(line)

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        try:
            proc.wait(timeout=hard_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            if log_callback:
                log_callback(f"[Flakiness] Rerun killed - exceeded {int(hard_timeout)}s hard limit")
        finally:
            reader.join(timeout=5)
            with _procs_lock:
                try:
                    _active_procs.remove(proc)
                except ValueError:
                    pass

    except FileNotFoundError:
        return RunResult(total=0, passed=0, failed=0, errors=1, skipped=0, duration=0,
                         tests=[], by_endpoint={"error": "pytest not found"})

    return _parse_report(report_path)


def _parse_report(report_path: str) -> RunResult:
    if not os.path.isfile(report_path):
        return RunResult(total=0, passed=0, failed=0, errors=1, skipped=0, duration=0)

    try:
        data = json.load(open(report_path, encoding="utf-8"))
    except Exception:
        return RunResult(total=0, passed=0, failed=0, errors=1, skipped=0, duration=0)

    summary = data.get("summary", {})
    tests_raw = data.get("tests", [])

    tests = []
    for t in tests_raw:
        name = t.get("nodeid", "")
        outcome = t.get("outcome", "unknown")
        duration = t.get("duration", 0.0)
        file_path = name.split("::")[0] if "::" in name else name

        error_msg = None
        if outcome in ("failed", "error"):
            call_data = t.get("call", {})
            longrepr = call_data.get("longrepr", "") or t.get("longrepr", "")
            if isinstance(longrepr, dict):
                longrepr = longrepr.get("reprcrash", {}).get("message", str(longrepr))
            error_msg = str(longrepr)[:500]

        endpoint = _infer_endpoint(name)
        tests.append(TestResult(
            name=name,
            outcome=outcome,
            duration=round(duration, 2),
            file=file_path,
            error_message=error_msg,
            endpoint=endpoint,
        ))

    by_endpoint: dict[str, list[TestResult]] = {}
    for t in tests:
        by_endpoint.setdefault(t.endpoint, []).append(t)

    return RunResult(
        total=summary.get("total", len(tests)),
        passed=summary.get("passed", 0),
        failed=summary.get("failed", 0),
        errors=summary.get("errors", 0),
        skipped=summary.get("skipped", 0),
        duration=round(data.get("duration", 0), 2),
        tests=tests,
        by_endpoint={k: v for k, v in by_endpoint.items()},
    )


def _infer_endpoint(nodeid: str) -> str:
    """test_login.py::test_Login_Submit_Valid -> /login, test_home.py -> /"""
    filename = nodeid.split("::")[0].split("/")[-1]
    path = filename.replace("test_", "").replace(".py", "")
    if path == "home":
        return "/"
    return "/" + path.replace("_", "/")


def _write_pytest_ini(autotest_path: str):
    content = "[pytest]\naddopts = --json-report --json-report-file=.report.json -v\ntestpaths = tests\n"
    open(os.path.join(autotest_path, "pytest.ini"), "w").write(content)
