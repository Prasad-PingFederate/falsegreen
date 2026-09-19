"""Tests for the Robot Framework detector.

Robot is the language where "this test asserts nothing" is the normal accident
rather than an unusual one, because nothing in the syntax distinguishes a step
that verifies from a step that merely acts. So these tests are mostly about the
two directions of that judgement: a real assertion must be recognised however it
is reached, and a test that only drives the application must be caught.

The false-negative direction matters more than the false-positive one here. A
detector that misses assertions would report healthy suites as broken and be
switched off within a day.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from falsegreen.detectors.robot import (
    normalize,
    parse,
    scan_robot_file,
    split_cells,
    step_is_assertion,
)
from falsegreen.models import Rule, ScanResult


def scan(tmp_path: Path, body: str) -> ScanResult:
    f = tmp_path / "suite.robot"
    f.write_text(body, encoding="utf-8")
    result = ScanResult(root=tmp_path)
    scan_robot_file(f, result)
    return result


def rules(result: ScanResult) -> set:
    return {f.rule for f in result.findings}


def named(result: ScanResult, name: str):
    return next(t for t in result.tests if t.name == name)


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Should Be Equal", "should be equal"),
        ("should_be_equal", "should be equal"),
        ("  Should   Be  Equal  ", "should be equal"),
    ],
)
def test_keyword_names_normalize(raw: str, expected: str):
    """Robot treats case, spacing and underscores as insignificant."""
    assert normalize(raw) == expected


def test_cells_split_on_two_spaces_not_one():
    """A single space is part of a keyword name; two or more separate cells."""
    assert split_cells("Input Text    id=user    admin") == ["Input Text", "id=user", "admin"]


def test_pipe_separated_format_is_understood():
    assert split_cells("| Input Text | id=user | admin |") == ["Input Text", "id=user", "admin"]


def test_trailing_comments_are_stripped_but_hashes_in_values_survive():
    body = """*** Test Cases ***
Uses A Hash
    Should Be Equal    ${a}    c#d    # trailing note
"""
    blocks = parse(body)
    step = blocks["tests"][0].steps[0]
    assert "c#d" in step.args
    assert not any("trailing note" in a for a in step.args)


# ---------------------------------------------------------------------------
# Recognising assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "keyword",
    [
        "Should Be Equal",
        "Should Contain",
        "Should Not Be Empty",
        "Page Should Contain",
        "Element Should Be Visible",
        "Wait Until Element Is Visible",
        "Table Column Should Contain",
        "Fail",
    ],
)
def test_assertion_keywords_are_recognised(keyword: str):
    """Missing any of these would report a healthy suite as assertion-free."""
    from falsegreen.detectors.robot import Step

    assert step_is_assertion(Step(keyword, [], 1)), f"{keyword} not seen as an assertion"


@pytest.mark.parametrize("keyword", ["Open Browser", "Click Button", "Input Text", "Log", "Sleep"])
def test_action_keywords_are_not_assertions(keyword: str):
    from falsegreen.detectors.robot import Step

    assert not step_is_assertion(Step(keyword, [], 1))


# ---------------------------------------------------------------------------
# The core judgement
# ---------------------------------------------------------------------------


def test_a_test_that_only_drives_the_app_is_flagged(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Drive And Hope
    Open Browser    ${URL}    chrome
    Click Button    Login
""")
    assert Rule.NO_ASSERTIONS in rules(result)
    assert not named(result, "Drive And Hope").is_trustworthy


def test_a_test_with_a_real_assertion_is_left_alone(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Login Works
    Open Browser    ${URL}    chrome
    Page Should Contain    Welcome
""")
    assert result.findings == []
    assert named(result, "Login Works").is_trustworthy


def test_assertions_reached_through_a_user_keyword_count(tmp_path: Path):
    """The test itself has no Should step; the keyword it calls does.

    Without transitive resolution every well-factored Robot suite - which is
    most of them - would be reported as entirely assertion-free.
    """
    result = scan(tmp_path, """*** Test Cases ***
Uses Helper
    Verify Dashboard Loaded

*** Keywords ***
Verify Dashboard Loaded
    Element Should Be Visible    id=dash
""")
    assert Rule.NO_ASSERTIONS not in rules(result)
    assert named(result, "Uses Helper").is_trustworthy


def test_transitive_resolution_follows_more_than_one_hop(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Uses Helper
    Outer Keyword

*** Keywords ***
Outer Keyword
    Inner Keyword

Inner Keyword
    Should Be Equal    ${a}    ${b}
""")
    assert named(result, "Uses Helper").is_trustworthy


def test_mutually_recursive_keywords_do_not_hang(tmp_path: Path):
    """Robot permits this; a suite must not be rejected for it."""
    result = scan(tmp_path, """*** Test Cases ***
Uses Helper
    Ping

*** Keywords ***
Ping
    Pong

Pong
    Ping
""")
    assert result.files_scanned == 1
    assert Rule.NO_ASSERTIONS in rules(result)


def test_a_helper_named_verify_that_never_asserts_is_reported(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Uses Helper
    Verify Nothing Really

*** Keywords ***
Verify Nothing Really
    Log    pretending
""")
    assert Rule.ASSERTION_FREE_HELPER in rules(result)


# ---------------------------------------------------------------------------
# Robot-specific ways to discard a failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrapper", ["Run Keyword And Ignore Error", "Run Keyword And Return Status"]
)
def test_wrappers_that_discard_a_failure_are_flagged(tmp_path: Path, wrapper: str):
    """These exist to swallow failures, which is fine - around an assertion it is not."""
    result = scan(tmp_path, f"""*** Test Cases ***
Swallows
    {wrapper}    Page Should Contain    Welcome
""")
    assert Rule.SWALLOWED_ASSERTION in rules(result)
    assert not named(result, "Swallows").is_trustworthy


def test_an_assertion_inside_try_except_is_flagged(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Tries
    TRY
        Page Should Contain    Welcome
    EXCEPT    message
        Log    swallowed
    END
""")
    assert Rule.SWALLOWED_ASSERTION in rules(result)


def test_a_conditional_assertion_is_flagged_as_optional(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Maybe Checks
    Run Keyword If    ${flag}    Page Should Contain    Welcome
""")
    assert Rule.OPTIONAL_ASSERTION in rules(result)


def test_sleep_is_flagged_but_does_not_invalidate_the_test(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Sleeps
    Sleep    5s
    Page Should Contain    Welcome
""")
    assert Rule.SLEEP_INSTEAD_OF_WAIT in rules(result)
    assert named(result, "Sleeps").is_trustworthy


def test_comparing_a_value_with_itself_is_a_tautology(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Tautology
    Should Be Equal    ${x}    ${x}
""")
    assert Rule.TAUTOLOGICAL_ASSERTION in rules(result)


def test_a_skipped_test_is_recorded_as_disabled(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Skipped One
    [Tags]    robot:skip
    Log    nothing
""")
    assert Rule.DISABLED_TEST in rules(result)
    assert named(result, "Skipped One").is_disabled


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_templated_tests_are_judged_by_their_template(tmp_path: Path):
    """A data-driven test has no steps of its own; the template does the work."""
    result = scan(tmp_path, """*** Test Cases ***
Good Data Test
    [Template]    Check Login
    admin    secret

*** Keywords ***
Check Login
    [Arguments]    ${user}    ${pw}
    Should Be Equal    ${user}    admin
""")
    assert Rule.NO_ASSERTIONS not in rules(result)


def test_a_template_that_never_asserts_is_flagged(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Empty Data Test
    [Template]    Just Log
    admin    secret

*** Keywords ***
Just Log
    [Arguments]    ${user}    ${pw}
    Log    ${user}
""")
    assert Rule.NO_ASSERTIONS in rules(result)


def test_continuation_lines_are_joined(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Wrapped
    Should Be Equal
    ...    ${actual}
    ...    ${expected}
""")
    assert Rule.NO_ASSERTIONS not in rules(result)


def test_variables_and_settings_sections_are_ignored(tmp_path: Path):
    """Only Test Cases / Tasks / Keywords can contain an assertion."""
    result = scan(tmp_path, """*** Settings ***
Library    SeleniumLibrary

*** Variables ***
${URL}    https://example.com

*** Test Cases ***
Real Test
    Page Should Contain    Welcome
""")
    assert len(result.tests) == 1
    assert result.findings == []


def test_tasks_section_is_treated_as_tests(tmp_path: Path):
    """RPA suites use *** Tasks *** for the same construct."""
    result = scan(tmp_path, """*** Tasks ***
Do The Thing
    Open Browser    ${URL}    chrome
""")
    assert len(result.tests) == 1
    assert Rule.NO_ASSERTIONS in rules(result)


def test_assignment_targets_are_not_mistaken_for_keywords(tmp_path: Path):
    result = scan(tmp_path, """*** Test Cases ***
Assigns
    ${value} =    Get Text    id=field
    Should Be Equal    ${value}    expected
""")
    assert Rule.NO_ASSERTIONS not in rules(result)


def test_an_unreadable_file_is_recorded_not_raised(tmp_path: Path):
    missing = tmp_path / "nope.robot"
    result = ScanResult(root=tmp_path)
    scan_robot_file(missing, result)

    assert result.files_scanned == 0
    assert len(result.errors) == 1
