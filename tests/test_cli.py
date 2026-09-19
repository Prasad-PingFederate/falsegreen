from __future__ import annotations

import json
from pathlib import Path
import pytest
from falsegreen.cli import main

def test_cli_clean_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    test_file = tmp_path / "test_clean.py"
    test_file.write_text("""
def test_addition():
    assert 1 + 1 == 2
""", encoding="utf-8")

    exit_code = main([str(tmp_path), "--format", "json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["trust_score"] == 100
    assert len(data["findings"]) == 0

def test_cli_fail_under_trigger(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    test_file = tmp_path / "test_bad.py"
    test_file.write_text("""
def test_no_assertions():
    x = 10
    y = 20
""", encoding="utf-8")

    exit_code = main([str(tmp_path), "--fail-under", "95"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "below the required 95" in captured.err

def test_cli_fail_on_severity(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    test_file = tmp_path / "test_bad.py"
    test_file.write_text("""
def test_mock_typo():
    mock = None
    mock.assert_called_once
""", encoding="utf-8")

    exit_code = main([str(tmp_path), "--fail-on", "high"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "finding(s) at or above 'high'" in captured.err

def test_cli_sarif_output_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    test_file = tmp_path / "test_bad.py"
    test_file.write_text("""
def test_tautology():
    assert 1 == 1
""", encoding="utf-8")

    output_sarif = tmp_path / "results.sarif"
    exit_code = main([str(tmp_path), "--format", "sarif", "--output", str(output_sarif)])
    assert exit_code == 0
    assert output_sarif.exists()
    sarif_data = json.loads(output_sarif.read_text(encoding="utf-8"))
    assert sarif_data["version"] == "2.1.0"
    assert len(sarif_data["runs"][0]["results"]) > 0

def test_cli_falsegreenignore(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    ignore_file = tmp_path / ".falsegreenignore"
    ignore_file.write_text("test_ignored.py\n", encoding="utf-8")

    ignored_file = tmp_path / "test_ignored.py"
    ignored_file.write_text("def test_bad():\n    pass\n", encoding="utf-8")

    valid_file = tmp_path / "test_valid.py"
    valid_file.write_text("def test_good():\n    assert True is not False\n", encoding="utf-8")

    exit_code = main([str(tmp_path), "--format", "json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data["findings"]) == 0
