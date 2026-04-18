"""
Re-runs failed tests to classify failures:
  FLAKY_TIMING   - passes on retry, likely timing/race condition
  SELECTOR_ISSUE - consistently fails with ElementNotFound -> test bug
  APP_BUG        - consistently fails with AssertionError -> possible app bug
  FLAKY_NETWORK  - timeout pattern
  UNKNOWN        - other
"""

import re
from app.config import FLAKINESS_RERUNS
from app.core import test_runner
from dataclasses import dataclass


@dataclass
class Diagnosis:
    test_name: str
    classification: str       # FLAKY_TIMING | SELECTOR_ISSUE | APP_BUG | FLAKY_NETWORK | UNKNOWN
    rerun_results: list[bool] # True = passed
    recommendation: str


def diagnose(
    autotest_path: str,
    failed_tests: list,      # list of TestResult
    log_callback=None,
    workers: int = 1,
) -> list[Diagnosis]:
    if not failed_tests:
        return []

    node_ids = list({t.name for t in failed_tests})

    if log_callback:
        log_callback(f"  [Flakiness] {len(node_ids)} failed test(s) will be rerun × {FLAKINESS_RERUNS}")

    # Map: test_name -> list of bool (pass/fail per rerun)
    rerun_map: dict[str, list[bool]] = {name: [] for name in node_ids}

    for i in range(FLAKINESS_RERUNS):
        if log_callback:
            log_callback(f"  [Flakiness] Rerun {i + 1}/{FLAKINESS_RERUNS} - running {len(node_ids)} test(s)...")

        result = test_runner.run_tests(
            autotest_path,
            specific_nodes=node_ids,
            log_callback=None,   # suppress per-test lines during reruns to avoid duplicate output
            workers=workers,
            timeout_ms=8000,     # shorter timeout for reruns - just enough to detect flakiness
            use_allure=False,    # don't overwrite allure results from the main run
        )

        passed_this_round = 0
        failed_this_round = 0
        for t in result.tests:
            if t.name in rerun_map:
                did_pass = t.outcome == "passed"
                rerun_map[t.name].append(did_pass)
                if did_pass:
                    passed_this_round += 1
                else:
                    failed_this_round += 1

        if log_callback:
            log_callback(
                f"  [Flakiness] Rerun {i + 1} result: "
                f"{passed_this_round} passed, {failed_this_round} failed"
            )

    # Classify each failed test
    diagnoses = []
    for test in failed_tests:
        rerun_results = rerun_map.get(test.name, [])
        classification = _classify(rerun_results, test.error_message or "")
        recommendation = _recommend(classification)

        short_name = test.name.split("::")[-1]
        if log_callback:
            icon = {
                "FLAKY_TIMING":      "⚡",
                "FLAKY_NETWORK":     "🌐",
                "SELECTOR_ISSUE":    "🔍",
                "HIDDEN_ELEMENT":    "👁",
                "ASSERTION_MISMATCH":"🔀",
                "APP_BUG":           "🐛",
            }.get(classification, "?")
            reruns_str = " ".join("✓" if r else "✗" for r in rerun_results)
            log_callback(f"  {icon} {short_name[:60]}  [{reruns_str}] -> {classification}")

        diagnoses.append(Diagnosis(
            test_name=test.name,
            classification=classification,
            rerun_results=rerun_results,
            recommendation=recommendation,
        ))

    return diagnoses


def _classify(rerun_results: list[bool], error_msg: str) -> str:
    total = len(rerun_results)

    # No rerun data - process was killed or report missing; classify by original error
    if total == 0:
        # Hidden element: locator resolves but element is not visible/editable
        if re.search(r"element is not visible|element is not editable|not visible.*not editable|waiting for.*to be visible.*editable", error_msg, re.IGNORECASE):
            return "HIDDEN_ELEMENT"
        if re.search(r"strict mode violation|locator\.(?:click|fill|check)|TimeoutError.*waiting for", error_msg, re.IGNORECASE):
            return "SELECTOR_ISSUE"
        # URL/title mismatch: exact value assertion failed on dynamic content
        if re.search(r"Page URL expected to be|Page title expected to be|unexpected value.*https?://", error_msg, re.IGNORECASE):
            return "ASSERTION_MISMATCH"
        if re.search(r"AssertionError|assert\s", error_msg, re.IGNORECASE):
            return "APP_BUG"
        if re.search(r"TimeoutError|net::ERR", error_msg, re.IGNORECASE):
            return "FLAKY_NETWORK"
        return "UNKNOWN"

    passed_count = sum(rerun_results)

    if passed_count > 0 and passed_count < total:
        if re.search(r"timeout|Timeout", error_msg, re.IGNORECASE):
            return "FLAKY_NETWORK"
        return "FLAKY_TIMING"

    if passed_count == total:
        return "FLAKY_TIMING"

    # All reruns failed — classify consistently failing tests
    if re.search(r"element is not visible|element is not editable|not visible.*not editable|waiting for.*to be visible.*editable", error_msg, re.IGNORECASE):
        return "HIDDEN_ELEMENT"
    if re.search(r"Page URL expected to be|Page title expected to be|unexpected value.*https?://", error_msg, re.IGNORECASE):
        return "ASSERTION_MISMATCH"
    if re.search(r"strict mode violation|locator\.(?:click|fill|check)|TimeoutError.*waiting for", error_msg, re.IGNORECASE):
        return "SELECTOR_ISSUE"
    if re.search(r"AssertionError|assert\s", error_msg, re.IGNORECASE):
        return "APP_BUG"
    if re.search(r"TimeoutError|net::ERR", error_msg, re.IGNORECASE):
        return "FLAKY_NETWORK"
    return "UNKNOWN"


def _recommend(classification: str) -> str:
    return {
        "FLAKY_TIMING":      "Add page.wait_for_load_state('networkidle') or increase timeout",
        "FLAKY_NETWORK":     "Check app stability - network timeouts detected during test runs",
        "SELECTOR_ISSUE":    "Fix selector - element not found. Claude will auto-fix this test",
        "HIDDEN_ELEMENT":    "Element exists but is hidden. Switch to the visible sibling (input vs textarea), or click the activation trigger first. Claude will auto-fix this test",
        "ASSERTION_MISMATCH":"Exact URL/title assertion fails on SPA dynamic content. Use re.compile() partial match. Claude will auto-fix this test",
        "APP_BUG":           "Possible bug in the app - assertion failed consistently. Manual review recommended",
        "UNKNOWN":           "Inspect error manually",
    }.get(classification, "Inspect error manually")
