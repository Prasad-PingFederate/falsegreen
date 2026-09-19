"""Generate a rich GitHub Step Summary for CI runs."""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict


def generate_summary(xml_path: str, summary_file: str | None = None) -> str:
    if not os.path.exists(xml_path):
        content = "### ⚠️ Test Results Not Found\nNo `test-results.xml` was generated during this run."
        if summary_file:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(content + "\n")
        return content

    tree = ET.parse(xml_path)
    root = tree.getroot()

    suite = root.find("testsuite") if root.tag == "testsuites" else root
    if suite is None and root.tag == "testsuite":
        suite = root

    total_tests = int(suite.attrib.get("tests", 0)) if suite is not None else 0
    failures = int(suite.attrib.get("failures", 0)) if suite is not None else 0
    errors = int(suite.attrib.get("errors", 0)) if suite is not None else 0
    skipped = int(suite.attrib.get("skipped", 0)) if suite is not None else 0
    time_taken = float(suite.attrib.get("time", 0.0)) if suite is not None else 0.0
    passed = total_tests - (failures + errors + skipped)

    repo_name = os.getenv("GITHUB_REPOSITORY", "falsegreen")
    status_badge = "✅ **ALL TESTS PASSED**" if (failures == 0 and errors == 0) else "❌ **TEST SUITE FAILED**"

    lines = [
        f"## 🎭 {repo_name} Automation & CI Test Summary",
        f"",
        f"{status_badge} • ⏱️ **Duration:** `{time_taken:.2f}s`",
        f"",
        f"| Metric | Count | Status |",
        f"| :--- | :---: | :--- |",
        f"| **Total Tests** | `{total_tests}` | 🎯 |",
        f"| **Passed** | `{passed}` | 🟢 Passed |",
        f"| **Failed** | `{failures}` | {'🔴 Failed' if failures > 0 else '⚪ None'} |",
        f"| **Errors** | `{errors}` | {'⚠️ Errors' if errors > 0 else '⚪ None'} |",
        f"| **Skipped** | `{skipped}` | {'🟡 Skipped' if skipped > 0 else '⚪ None'} |",
        f"",
        f"### 📋 Test Suite Breakdown",
        f"",
        f"| Test Suite / Module | Tests | Passed | Failed | Status |",
        f"| :--- | :---: | :---: | :---: | :--- |",
    ]

    module_stats = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0, "time": 0.0})
    for tc in root.iter("testcase"):
        cls = tc.attrib.get("classname", "unknown")
        mod_name = cls.split(".")[-1] + ".py" if "." in cls else cls
        module_stats[mod_name]["total"] += 1
        t_val = float(tc.attrib.get("time", 0.0))
        module_stats[mod_name]["time"] += t_val

        has_failure = tc.find("failure") is not None or tc.find("error") is not None
        if has_failure:
            module_stats[mod_name]["failed"] += 1
        else:
            module_stats[mod_name]["passed"] += 1

    for mod_name, stats in sorted(module_stats.items()):
        mod_status = "✅ Pass" if stats["failed"] == 0 else f"❌ {stats['failed']} Fail"
        lines.append(
            f"| `{mod_name}` | `{stats['total']}` | `{stats['passed']}` | `{stats['failed']}` | {mod_status} |"
        )

    lines.append("")
    lines.append("---")
    lines.append(f"*Automated with Playwright Python & Pytest • Workflow Run in GitHub Actions*")

    markdown = "\n".join(lines)

    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")

    return markdown


if __name__ == "__main__":
    xml_arg = sys.argv[1] if len(sys.argv) > 1 else "test-results.xml"
    gh_summary = os.getenv("GITHUB_STEP_SUMMARY")
    res = generate_summary(xml_arg, gh_summary)
    if not gh_summary:
        print(f"Generated summary for {xml_arg}")
