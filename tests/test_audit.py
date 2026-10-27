"""Unit tests for the deterministic evidence audit in agent/main.py (numbers in findings vs query rows, scope guard)."""
import pytest

from agent.main import audit


COLS = ["day", "trips", "trips_per_hour"]
ROWS = [["2024-07-27", "66660", "4612"], ["2024-07-31", "109582", "4111"]]


def test_audit_passes_when_numbers_match_rows():
    finding = "Jul 27 had 66660 trips vs 109582 on Jul 31; per hour 4612 vs 4111."
    r = audit(finding, COLS, ROWS)
    assert r["ok"] is True
    assert r["unverified_numbers"] == []
    assert r["scope_flags"] == []


def test_audit_flags_invented_numbers():
    finding = "Saturday had 6100 trips per hour vs 10068 on Wednesday."
    r = audit(finding, COLS, ROWS)
    assert r["ok"] is False
    assert "6100" in r["unverified_numbers"] or "10068" in r["unverified_numbers"]


def test_audit_flags_scope_generalisation():
    finding = "Across July 2024 days, all Saturdays are typically quieter with 66660 trips."
    r = audit(finding, COLS, ROWS)
    assert r["ok"] is False
    assert r["unverified_numbers"] == []
    assert any("saturday" in s or "july" in s or "typical" in s for s in r["scope_flags"])


def test_audit_skips_percentages_and_small_integers():
    finding = "Coverage was 13 hours on day 1; share of gap is 45% of the total 66660 trips."
    r = audit(finding, COLS, ROWS)
    assert r["ok"] is True
    assert "45" not in r["unverified_numbers"]
    assert "13" not in r["unverified_numbers"]


def test_audit_accepts_comma_formatted_numbers_in_finding():
    finding = "Total trips were 66,660 on the focus day."
    r = audit(finding, COLS, ROWS)
    assert r["ok"] is True


def test_audit_tolerance_for_rounding():
    finding = "Rate was 4612.04 trips per hour (rounded from query)."
    r = audit(finding, COLS, ROWS)
    assert r["ok"] is True


@pytest.mark.parametrize("finding", ["", None])
def test_audit_empty_finding(finding):
    r = audit(finding, COLS, ROWS)
    assert r["ok"] is True
    assert r["unverified_numbers"] == []


def test_audit_does_not_treat_query_ids_as_evidence_numbers():
    finding = "See evidence Q3: trips were 66660 on 2024-07-27."
    r = audit(finding, COLS, ROWS)
    assert r["ok"] is True
    assert "3" not in r["unverified_numbers"]
