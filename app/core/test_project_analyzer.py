"""
Analyzes the autotest project directory.
- Validates that the path exists (mandatory requirement).
- Detects whether the project is empty or has existing tests.
- Extracts existing test function names grouped by file/endpoint.
- Determines coverage gaps.
"""

import ast
import os
import shutil
from dataclasses import dataclass, field
from typing import Optional

_SYSTEM_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium")
_CHROME_LAUNCH_ARGS  = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"]


def _has_system_chrome() -> bool:
    return any(shutil.which(n) for n in _SYSTEM_CHROME_NAMES)


@dataclass
class TestInfo:
    function_name: str
    file_path: str
    docstring: Optional[str] = None


@dataclass
class ProjectAnalysis:
    path: str
    exists: bool
    is_empty: bool
    existing_tests: list[TestInfo] = field(default_factory=list)
    covered_endpoints: list[str] = field(default_factory=list)
    scaffold_needed: bool = False
    summary: str = ""


def validate_path(path: str) -> tuple[bool, str]:
    """Returns (is_valid, error_message). Path must exist and be a directory."""
    if not path or not path.strip():
        return False, "Path is required"
    if not os.path.exists(path):
        return False, f"Directory does not exist: {path}"
    if not os.path.isdir(path):
        return False, f"Path is a file, not a directory: {path}"
    return True, ""


def analyze(autotest_path: str) -> ProjectAnalysis:
    valid, err = validate_path(autotest_path)
    if not valid:
        return ProjectAnalysis(path=autotest_path, exists=False, is_empty=True, summary=err)

    test_files = _find_test_files(autotest_path)
    if not test_files:
        return ProjectAnalysis(
            path=autotest_path,
            exists=True,
            is_empty=True,
            scaffold_needed=True,
            summary="Project is empty - scaffold will be created before test generation.",
        )

    existing_tests = []
    for fpath in test_files:
        existing_tests.extend(_extract_tests(fpath))

    covered = _infer_endpoints(existing_tests)

    summary_lines = [f"Found {len(existing_tests)} existing tests in {len(test_files)} files."]
    if covered:
        summary_lines.append(f"Covered endpoints: {', '.join(covered)}")

    return ProjectAnalysis(
        path=autotest_path,
        exists=True,
        is_empty=False,
        existing_tests=existing_tests,
        covered_endpoints=covered,
        scaffold_needed=not _has_conftest(autotest_path),
        summary=" ".join(summary_lines),
    )


def _find_test_files(root: str) -> list[str]:
    result = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.startswith("test_") and f.endswith(".py"):
                result.append(os.path.join(dirpath, f))
    return result


def _extract_tests(filepath: str) -> list[TestInfo]:
    tests = []
    try:
        source = open(filepath, encoding="utf-8", errors="ignore").read()
        tree = ast.parse(source)
    except Exception:
        return tests

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                doc = ast.get_docstring(node)
                tests.append(TestInfo(
                    function_name=node.name,
                    file_path=filepath,
                    docstring=doc,
                ))
    return tests


def _infer_endpoints(tests: list[TestInfo]) -> list[str]:
    """Guess which endpoints are covered by parsing test filenames."""
    endpoints = set()
    for t in tests:
        basename = os.path.basename(t.file_path)
        # test_login.py -> /login, test_dashboard_overview.py -> /dashboard/overview
        name = basename.replace("test_", "").replace(".py", "").replace("_", "/")
        endpoints.add("/" + name)
    return sorted(endpoints)


def _has_conftest(root: str) -> bool:
    return os.path.isfile(os.path.join(root, "conftest.py"))


def build_conftest(autotest_path: str, base_url: str, login: str = "", password: str = "", login_url: str = "/login", bypass_header: dict | None = None, sleep_ms: int = 0) -> str:
    """Generate conftest.py content for the autotest project."""
    launch_args_fixture = ""
    if _has_system_chrome():
        launch_args_fixture = f'''
@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    return {{
        **browser_type_launch_args,
        "args": {_CHROME_LAUNCH_ARGS!r},
    }}
'''

    auth_fixture = ""
    if login:
        _login_path = "/" + login_url.strip("/")
        auth_fixture = f'''

@pytest.fixture(scope="session")
def credentials():
    return {{"login": "{login}", "password": "{password}"}}


@pytest.fixture
def logged_in_page(page, credentials):
    """Authenticated page - logs in once and returns the page."""
    page.goto(BASE_URL + "{_login_path}")
    page.get_by_label("Login").fill(credentials["login"])
    page.get_by_label("Password").fill(credentials["password"])
    page.get_by_role("button", name="Login").click()
    page.wait_for_load_state("networkidle")
    return page
'''

    bypass_header_lines = ""
    extra_headers_fixture = ""
    if bypass_header:
        header_name = bypass_header.get("name", "")
        header_value = bypass_header.get("value", "")
        if header_name and header_value:
            bypass_header_lines = f'\nBYPASS_HEADER = {{"{header_name}": "{header_value}"}}\n'
            extra_headers_fixture = f'\n        "extra_http_headers": BYPASS_HEADER,'

    sleep_line = f"\n    time.sleep({sleep_ms / 1000:.3f})" if sleep_ms > 0 else ""
    time_import = "import time\n" if sleep_ms > 0 else ""

    return f'''{time_import}import os
import pytest
from playwright.sync_api import Page
from playwright_stealth import Stealth
from faker import Faker
{bypass_header_lines}
BASE_URL = "{base_url.rstrip("/")}"
fake = Faker()

_stealth = Stealth()

_CONSENT_SELECTORS = [
    'button:has-text("Accept all")',
    'button:has-text("Accept")',
    'button:has-text("Agree")',
    'button:has-text("OK")',
    '[id*="consent" i] button',
    '[id*="cookie" i] button',
    '[class*="consent" i] button',
    '[class*="cookie" i] button',
]


def dismiss_overlays(page: Page) -> None:
    """Try to close cookie/consent banners."""
    for sel in _CONSENT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=500):
                btn.click(timeout=500)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


def _get_env_bypass_header() -> dict:
    """Runtime bypass header override via env vars (set by Run Tests page)."""
    name  = os.environ.get("AUTOTEST_BYPASS_HEADER_NAME",  "").strip()
    value = os.environ.get("AUTOTEST_BYPASS_HEADER_VALUE", "").strip()
    if name and value:
        return {{name: value}}
    return {{}}


{launch_args_fixture}
@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    ctx = {{
        **browser_context_args,
        "ignore_https_errors": True,
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "viewport": {{"width": 1440, "height": 900}},{extra_headers_fixture}
    }}
    if "extra_http_headers" not in ctx:
        env_headers = _get_env_bypass_header()
        if env_headers:
            ctx["extra_http_headers"] = env_headers
    return ctx


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


@pytest.fixture(autouse=True)
def screenshot_on_failure(page: Page, request):
    yield
    rep = getattr(request.node, "rep_call", None)
    if rep and rep.failed:
        shots_dir = os.path.join(os.path.dirname(__file__), "tests", "screenshots")
        os.makedirs(shots_dir, exist_ok=True)
        safe_name = request.node.name.replace("/", "_").replace(":", "_")
        path = os.path.join(shots_dir, f"{{safe_name}}.png")
        try:
            png = page.screenshot(full_page=True)
            # Save to file
            with open(path, "wb") as f:
                f.write(png)
            # Attach to Allure report if available
            try:
                import allure
                allure.attach(png, name="screenshot", attachment_type=allure.attachment_type.PNG)
            except ImportError:
                pass
        except Exception:
            pass


@pytest.fixture
def page(page: Page):
    _stealth.use_sync(page)
    page.set_default_timeout(20000)
    yield page{sleep_line}
    # Runtime sleep override from Run Tests page (AUTOTEST_SLEEP_MS env var)
    if not {bool(sleep_ms)}:
        _env_sleep = int(os.environ.get("AUTOTEST_SLEEP_MS", 0))
        if _env_sleep > 0:
            import time as _time
            _time.sleep(_env_sleep / 1000)
{auth_fixture}
'''


def build_pytest_ini(autotest_path: str) -> str:
    return """[pytest]
addopts = --json-report --json-report-file=.report.json -v
testpaths = tests
"""
