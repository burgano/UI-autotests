"""
Builds prompts for Claude. Keeps context under 3000 tokens by using
the structured frontend summary instead of raw source files.
"""

import json
import os


PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "prompts")


def _load(name: str) -> str:
    path = os.path.join(PROMPTS_DIR, name)
    if os.path.isfile(path):
        return open(path, encoding="utf-8").read()
    return ""


def build_generate_tests(
    autotest_path: str,
    url: str,
    route_info: dict,
    existing_test_names: list[str],
    include_positive: bool,
    include_negative: bool,
    base_url: str,
    login: str = "",
    password: str = "",
    login_url: str = "/login",
    page_objects_known: list[str] = None,
    existing_file_content: str = "",
    max_positive: int = 0,
    max_negative: int = 0,
) -> str:
    skip_section = ""
    if existing_file_content:
        skip_section = (
            f"\n\n## Existing test file\n"
            f"The file already exists. Your job:\n"
            f"1. Compare the CURRENT PAGE ANALYSIS above with what the existing tests cover.\n"
            f"2. Identify UI elements, fields, or interactions present on the page NOW "
            f"that are NOT covered by any existing test.\n"
            f"3. If you find uncovered cases - add tests ONLY for those. "
            f"Keep all existing tests intact, do not rename or remove them.\n"
            f"4. If everything is already covered - output exactly the word SKIP and do nothing.\n\n"
            f"```python\n{existing_file_content[:4000]}\n```"
        )
    elif existing_test_names:
        skip_section = (
            "\n\nALREADY COVERED - do NOT recreate these tests:\n"
            + "\n".join(f"  - {t}" for t in existing_test_names)
        )

    po_section = ""
    if page_objects_known:
        po_section = (
            "\n\nEXISTING PAGE OBJECTS (reuse, do not recreate):\n"
            + "\n".join(f"  - {po}" for po in page_objects_known)
        )

    test_types = []
    if include_positive:
        limit = f" - generate UP TO {max_positive} tests" if max_positive > 0 else ""
        test_types.append(f"positive (happy path, valid inputs, expected button behavior){limit}")
    if include_negative:
        limit = f" - generate UP TO {max_negative} tests" if max_negative > 0 else ""
        test_types.append(
            f"negative (empty required fields, invalid formats, boundary values min/max, "
            f"auth guard if applicable){limit}"
        )

    auth_note = ""
    if login:
        auth_note = f"\nCredentials available: login='{login}', password='***', login_url='{login_url}'. Use the logged_in_page fixture for protected routes."

    route_summary = json.dumps(route_info, indent=2, ensure_ascii=False)

    return f"""{_load("system_prompt.txt")}

## Task: Generate Playwright Python tests

Target URL: {base_url.rstrip("/")}{url}
Endpoint path: {url}
Autotest project directory: {autotest_path}

## Route analysis (extracted from frontend source)
{route_summary}

## Test types to generate
{chr(10).join(f"  - {t}" for t in test_types)}
{auth_note}{skip_section}{po_section}

## Naming convention
Test function names must follow: test_<Section>_<Function>_<WhatWeCheck>
Example: test_Login_Submit_ValidCredentials, test_Login_Submit_EmptyPassword

## Instructions
1. Write all tests to: {autotest_path}/tests/test_{_url_to_filename(url)}.py
2. Import Page from playwright.sync_api
3. Use conftest.py fixtures: page, logged_in_page (if auth required), fake, BASE_URL
4. Each test must be independent (no shared state)
5. Add a one-line docstring per test describing what is verified
6. Use page.get_by_role / get_by_label / get_by_placeholder selectors (prefer accessible locators)
7. After writing the file, output a summary: list of test function names created

## Playwright Python rules (CRITICAL — follow all of these)

### Assertions — always use expect(), never raw assert
- `expect(locator).to_be_visible()` — NOT `assert locator.is_visible()`
- `expect(locator).to_have_text("...")` — NOT `assert locator.inner_text() == "..."`
- `expect(locator).to_contain_text("...")` — NOT `assert "..." in locator.inner_text()`
- `expect(locator).to_have_value("...")` — NOT `assert locator.input_value() == "..."`
- `expect(locator).to_have_count(0)` — NOT `assert locator.count() == 0`
- `expect(locator).not_to_have_count(0)` — NOT `assert locator.count() > 0`
- `expect(locator).to_be_enabled()` — NOT `assert not locator.is_disabled()`
- `expect(locator).to_be_checked()` — NOT `assert locator.is_checked()`
- `expect(locator).to_have_attribute("attr", "val")` — NOT `assert locator.get_attribute("attr") == "val"`
- `expect(page).to_have_url("...")` — NOT `assert page.url == "..."`
- `expect(page).to_have_title("...")` — NOT `assert page.title() == "..."`
- Reason: `expect()` auto-waits and retries up to 5s; raw assert is a one-shot snapshot that races with async DOM updates

### "No results" scenarios
- Prefer checking for a visible empty-state message: `expect(page.get_by_text("No results")).to_be_visible()`
- Only use `expect(locator).to_have_count(0)` if the app renders no empty-state message at all

### Locators — priority order
1. `get_by_role("button", name="Submit")` — best: reflects ARIA, resilient to style changes
2. `get_by_label("Email")` — for form inputs with associated labels
3. `get_by_placeholder("Enter email")` — for inputs without visible labels
4. `get_by_text("Sign in")` — for non-interactive elements (div, span, p)
5. `get_by_alt_text("Logo")` — for images
6. `get_by_test_id("submit-btn")` — when data-testid attributes exist
7. CSS / XPath — LAST RESORT ONLY; avoid selectors tied to DOM structure (nth-child, deep chains)

### Locator anti-patterns to avoid
- NEVER use `page.locator("div > div:nth-child(3) > span")` — breaks on DOM changes
- NEVER use `page.locator("xpath=//div[@class='container']//button")` — fragile and cannot pierce Shadow DOM
- AVOID `.first` / `.last` / `.nth()` unless elements are truly identical and order is guaranteed
- AVOID `page.locator(".btn-primary")` when a role locator is available

### Interactions — let Playwright auto-wait
- NEVER add `time.sleep()` — Playwright waits for actionability automatically before every action
- NEVER add `page.wait_for_timeout(ms)` for element readiness — use `expect(locator).to_be_visible()` before interacting if needed
- Use `page.wait_for_load_state("domcontentloaded")` only right after `page.goto()` when the page is heavy
- Use `locator.fill("text")` for inputs — NOT `locator.click(); locator.type("text")`
- Use `locator.select_option("value")` for `<select>` dropdowns — NOT clicking individual options

### Test isolation
- Each test must be fully independent — no shared state between tests
- Never rely on execution order; each test starts fresh from `page.goto(BASE_URL)`
- Use `fake` fixture (Faker) for dynamic test data instead of hardcoded strings that may conflict
"""


def build_diagnose_failure(
    test_name: str,
    test_file: str,
    error_output: str,
    url: str,
    rerun_results: list[bool],
) -> str:
    flaky = any(rerun_results) and not all(rerun_results)
    classification = "FLAKY" if flaky else "CONSISTENT_FAILURE"

    return f"""{_load("system_prompt.txt")}

## Task: Diagnose test failure

Test: {test_name}
File: {test_file}
URL: {url}
Classification: {classification}
Rerun results (True=pass): {rerun_results}

## Error output
{error_output[:3000]}

## Instructions
1. Read the test file at: {test_file}
2. Determine root cause: test bug (bad selector, wrong assertion) OR app bug (unstable behavior)
3. If test bug: fix the test in place
4. If app bug: add a comment in the test: # KNOWN_ISSUE: <description>
5. Output a one-line diagnosis: FIXED_TEST | APP_BUG | FLAKY_TIMING | SELECTOR_ISSUE
"""


def build_fix_failing_tests(
    test_file: str,
    failure_details: str,
    url: str,
) -> str:
    return f"""{_load("system_prompt.txt")}

## Task: Fix failing Playwright tests

Test file: {test_file}
URL under test: {url}

## Failing tests and their errors

{failure_details}

## Instructions

1. Read the test file at: {test_file}
2. Read conftest.py in the project root to understand fixtures (read only, do not modify it)
3. For each failing test above identify the root cause from the error message:
   - Wrong selector (element not found, wrong locator) - fix the selector
   - Wrong assertion (expected value doesn't match actual) - fix the assertion
   - Missing wait or timing issue - add expect().to_be_visible() or wait_for_selector
   - Wrong navigation flow (wrong URL, missing step) - fix the navigation
4. Fix only the failing tests in-place. Do NOT touch passing tests. Do NOT add new tests.
5. If a test is catching a real app bug (not a test mistake), add # KNOWN_ISSUE: <description> and adjust the assertion to match actual behavior so the test passes.

## Playwright rules to apply when fixing
- Replace `assert locator.count() == 0` → `expect(locator).to_have_count(0)`
- Replace `assert locator.is_visible()` → `expect(locator).to_be_visible()`
- Replace `assert locator.inner_text() == "..."` → `expect(locator).to_have_text("...")`
- Replace `assert locator.input_value() == "..."` → `expect(locator).to_have_value("...")`
- Replace `assert page.url == "..."` → `expect(page).to_have_url("...")`
- Remove any `time.sleep()` or `page.wait_for_timeout()` — add `expect(locator).to_be_visible()` instead if needed
- Replace CSS/XPath selectors with role/label/placeholder locators where possible
"""


def build_analyze_project_structure(autotest_path: str, file_tree: str) -> str:
    return f"""{_load("system_prompt.txt")}

## Task: Analyze existing autotest project structure

Autotest project directory: {autotest_path}

## File tree
{file_tree}

## Instructions

Analyze the project and respond with a JSON object only (no markdown, no explanation):

{{
  "test_files": ["relative/path/to/test_file.py", ...],
  "tests_dir": "relative path to tests folder, e.g. tests/ or e2e/ or src/tests/",
  "framework": "pytest+playwright | pytest | other",
  "has_conftest": true | false,
  "conftest_path": "relative path to conftest.py or null",
  "fixtures_detected": ["page", "logged_in_page", ...],
  "covered_endpoints": ["/login", "/dashboard", ...],
  "existing_test_count": 42,
  "notes": "any important observations about structure, conflicts, or non-standard patterns"
}}

Rules:
- Infer covered_endpoints from test filenames: test_login.py -> /login, test_home.py -> /
- If tests use classes (class TestLogin), still count them as covered
- covered_endpoints must list only paths (starting with /), not full URLs
- If the project is empty or has no test files, return empty lists and existing_test_count: 0
- Return ONLY the JSON object, nothing else
"""


def build_analyze_frontend(frontend_path: str, framework: str) -> str:
    return f"""{_load("system_prompt.txt")}

## Task: Analyze frontend project structure

Framework: {framework}
Frontend path: {frontend_path}

List all user-facing routes/pages you can identify from the source code.
For each route output: path, main user actions available, form fields present.
Be concise - maximum 20 lines total.
"""


def _url_to_filename(url: str) -> str:
    """Convert /dashboard/overview -> dashboard_overview"""
    return url.strip("/").replace("/", "_").replace("-", "_") or "home"
