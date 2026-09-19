"""Tests for SARIF v2.1.0 report generation."""

from __future__ import annotations

import json
from pathlib import Path

from falsegreen.detectors.python_ast import scan_python_file
from falsegreen.models import ScanResult
from falsegreen.report import sarif
from falsegreen.score import compute


def test_sarif_generation(tmp_path: Path):
    f = tmp_path / "test_sample.py"
    f.write_text(
        """
def test_broken_mock():
    mock.assert_called_once

def test_constant_trap():
    assert status == 200 or 201
""",
        encoding="utf-8",
    )
    result = ScanResult(root=tmp_path)
    scan_python_file(f, result)
    score = compute(result)

    sarif_text = sarif(result, score)
    data = json.loads(sarif_text)

    assert data["version"] == "2.1.0"
    assert "sarif-spec" in data["$schema"]

    runs = data["runs"]
    assert len(runs) == 1
    driver = runs[0]["tool"]["driver"]
    assert driver["name"] == "falsegreen"
    assert "Prasad-PingFederate/falsegreen" in driver["informationUri"]

    results = runs[0]["results"]
    rule_ids = {r["ruleId"] for r in results}
    assert "mock-assertion-typo" in rule_ids
    assert "constant-condition-trap" in rule_ids

    # Check locations and snippet
    mock_result = [r for r in results if r["ruleId"] == "mock-assertion-typo"][0]
    loc = mock_result["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "test_sample.py"
    assert loc["region"]["startLine"] > 0
    assert "mock.assert_called_once" in loc["region"]["snippet"]["text"]
