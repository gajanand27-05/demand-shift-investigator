"""Offline test of the agent loop's control flow (no model, no Zetaris): a scripted provider drives a run where
SQL fails once and is repaired, the critic sends one hypothesis back for revision and adds a missing one, and
the H-MEM write-back is recalled by the next investigation."""
import json

import pytest

from agent import llm, main
from agent import tools_zetaris as z
from engines.hmem.hmem import HMEM

ROWS = {"columns": ["pickup_date", "trips"], "rows": [["2024-07-27", "66660"], ["2024-07-31", "109582"]]}
PROFILE = {"columns": ["pickup_date", "pickup_dow", "trips", "first_pickup", "last_pickup", "hours_covered"],
           "rows": [["2024-07-27", "Saturday", "66660", "2024-07-27 11:57:01", "2024-07-27 23:59:58", "13"],
                    ["2024-07-31", "Wednesday", "109582", "2024-07-31 00:00:00", "2024-07-31 23:59:59", "24"]]}


class FakeModel:
    def __init__(self):
        self.calls = []
        self.critic_rounds = 0
        self.sql_calls = 0

    def __call__(self, role, system, user, temperature=0.2):
        self.calls.append((role, user))
        if role == "planner":
            j = {"question_restated": "q", "metric": "trips/day", "focus": "2024-07-27", "baseline": "2024-07-28..31",
                 "hypotheses": [{"id": "H1", "claim": "Saturday is a lower-demand weekday pattern", "test": "trips by dow"},
                                {"id": "H2", "claim": "Rain suppressed demand", "test": "join weather"}],
                 "memory_used": []}
        elif role == "analyst" and '"sql"' in system:
            self.sql_calls += 1
            sql = "SELEC broken" if self.sql_calls == 1 else "SELECT pickup_date, COUNT(*) FROM investigator.trips GROUP BY 1"
            j = {"sql": sql, "why": "compare days"}
        elif role == "analyst":
            j = {"finding": "Jul 27 had 66660 trips vs 109582 on Jul 31", "key_numbers": {"jul27": 66660}, "direction": "supports"}
        elif role == "critic":
            self.critic_rounds += 1
            if self.critic_rounds == 1:
                j = {"reviews": [{"id": "H1", "verdict": "revise", "reason": "Jul 27 covers only 13 hours",
                                  "revision_request": "compare the same hours (12:00-23:59) only"},
                                 {"id": "H2", "verdict": "reject", "reason": "no rain on Jul 27", "revision_request": ""}],
                     "missing_hypothesis": {"claim": "Partial-day coverage explains most of the gap", "test": "hours covered"},
                     "data_caveats": ["2024-07-27 data starts at 11:57, so whole-day totals undercount that day"],
                     "decision": "another_round"}
            else:
                j = {"reviews": [{"id": "H1", "verdict": "accept", "reason": "like-for-like hours", "revision_request": ""},
                                 {"id": "H3", "verdict": "accept", "reason": "coverage explains the gap", "revision_request": ""}],
                     "missing_hypothesis": None, "data_caveats": [], "decision": "approve"}
        elif role == "reporter":
            j = {"answer": "Mostly a coverage gap.", "causes": [{"cause": "partial day", "impact": "large", "evidence": ["Q3"],
                                                                "confidence": 0.8}],
                 "rejected_explanations": ["rain"], "data_caveats": ["Jul 27 partial"], "recommendation": "compare like hours",
                 "confidence": 0.8, "limitations": ["one week of data"]}
        else:
            raise AssertionError(role)
        return {"text": json.dumps(j), "json": j, "model": "fake-nemotron", "latency_s": 0.0, "cached": False}


def fake_post(sql, limit):
    if "SELEC " in sql:
        raise RuntimeError("Zetaris 400: syntax error")
    return PROFILE if "hours_covered" in sql else ROWS


@pytest.fixture
def offline(monkeypatch, tmp_path):
    fake = FakeModel()
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(z, "_post", fake_post)
    monkeypatch.setattr(z, "_cache", llm.Cache(tmp_path / "cache.json"))
    monkeypatch.setenv("SAMPLE_MODE", "false")
    return fake, tmp_path


def test_loop_revises_retries_and_remembers(offline):
    fake, tmp = offline
    hmem = HMEM(path=tmp / "mem.json")
    case = main.Investigation("Why was demand low on 2024-07-27?", main.Trace(quiet=True), hmem).run()

    hs = case["hypotheses"]
    assert hs["H1"]["status"] == "accepted" and len(hs["H1"]["attempts"]) == 2   # revised after critique
    assert hs["H2"]["status"] == "rejected"
    assert hs["H3"]["origin"] == "critic" and hs["H3"]["status"] == "accepted"   # critic-added branch
    assert [r for r, _ in fake.calls].count("critic") == 2                       # Planner<->Analyst<->Critic loop
    assert any("failed in Zetaris" in u for r, u in fake.calls if r == "analyst")  # SQL error fed back for repair
    assert case["report"]["answer"]
    assert all(q["sql"].lower().startswith("select") for q in case["queries"].values())

    # Next investigation recalls the caveat the critic found (H-MEM across sessions).
    nxt = main.Investigation("Why did demand drop on Saturday 2024-07-27?", main.Trace(quiet=True), HMEM(path=tmp / "mem.json"))
    nxt.recall()
    assert "11:57" in nxt.case["memory"]["context"]


def test_guard_rejects_unsafe_sql():
    for bad in ["DROP VIEW investigator.trips", "SELECT * FROM NY_TAXI.hvv", "SELECT 1; SELECT 2",
                "INSERT INTO investigator.trips VALUES (1)"]:
        with pytest.raises(z.SqlRejected):
            z.guard(bad)
    assert z.guard("SELECT pickup_date FROM investigator.trips GROUP BY 1;").endswith("GROUP BY 1")
