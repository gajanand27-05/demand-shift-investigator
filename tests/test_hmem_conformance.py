"""Python port of the official H-MEM conformance runner (meterless/engines/hmem/conformance/runner.ts),
driven by the official vectors.json (Apache-2.0, vendored unchanged in engines/hmem/conformance/)."""
import json
from pathlib import Path

import pytest

from engines.hmem.hmem import HMEM

VECTORS = json.loads((Path(__file__).parents[1] / "engines/hmem/conformance/vectors.json").read_text())
T0 = VECTORS["clock"]
DAY = 24 * 60 * 60 * 1000
S = VECTORS["sections"]


def ids(section):
    return [c["id"] for c in S[section]]


@pytest.mark.parametrize("c", S["record-validation"], ids=ids("record-validation"))
def test_record_validation(c):
    h = HMEM(clock=lambda: T0)
    try:
        h.add(content=c["input"]["content"], source=c["input"]["source"])
        rejected = False
    except ValueError:
        rejected = True
    assert rejected == (c["expect"] == "reject")


@pytest.mark.parametrize("c", S["feedback-deltas"], ids=ids("feedback-deltas"))
def test_feedback_deltas(c):
    h = HMEM(clock=lambda: T0)
    m = h.add(content="feedback target record", confidence=c["start"], source="conformance")
    before = m["accessCount"]
    h.feedback(m["id"], c["kind"])
    after = h.store.get(m["id"])
    assert after["confidence"] == pytest.approx(c["expectConfidence"], abs=1e-9)
    if "expectAccessDelta" in c:
        assert after["accessCount"] - before == c["expectAccessDelta"]
    if c.get("expectTag"):
        assert c["expectTag"] in after["tags"]


@pytest.mark.parametrize("c", S["ranking-order"], ids=ids("ranking-order"))
def test_ranking_order(c):
    h = HMEM(clock=lambda: T0)
    by = {r["key"]: h.add(content=r["content"], confidence=r["confidence"], layer=r["layer"], source="conformance")
          for r in c["records"]}
    for r in c["records"]:
        if r.get("supersededByKey"):
            by[r["key"]]["supersededBy"] = by[r["supersededByKey"]]["id"]
    order = [x["memory"]["id"] for x in h.query(c["query"], threshold=0, topN=10)["memories"]]
    expected = [by[k]["id"] for k in c["expectOrder"]]
    for a, b in zip(expected, expected[1:]):
        assert order.index(a) < order.index(b)


@pytest.mark.parametrize("c", S["sleep-guardrails"], ids=ids("sleep-guardrails"))
def test_sleep_guardrails(c):
    now = [T0]
    h = HMEM(clock=lambda: now[0])
    by = {r["key"]: h.add(content=r["content"], source=r["source"], derivedFrom=r.get("derivedFrom")) for r in c["records"]}
    now[0] = T0 + c["ageDays"] * DAY
    before = sorted(m["id"] for m in h.store.all())
    preview = h.sleep.preview()
    assert sorted(m["id"] for m in h.store.all()) == before, "preview mutated the store"
    for k in c["expectArchived"]:
        assert by[k]["id"] in preview["toArchive"]
    for k in c["expectProtected"]:
        assert by[k]["id"] not in preview["toArchive"]
    report = h.sleep.execute(preview)
    for k in c["expectProtected"]:
        assert h.store.get(by[k]["id"]) is not None
    assert isinstance(report["backupId"], str) and report["backupId"]


@pytest.mark.parametrize("c", S["conflict-gate"], ids=ids("conflict-gate"))
def test_conflict_gate(c):
    h = HMEM(clock=lambda: T0)
    a = h.add(content=c["a"]["content"], confidence=c["a"]["confidence"], source="conformance")
    b = h.add(content=c["b"]["content"], confidence=c["b"]["confidence"], source="conformance")
    found = h.conflicts.scan()
    assert found, "opposing always/never pair on similar content must be detected"
    outcome = h.conflicts.autoResolve(found[0])
    assert outcome in ("resolved", "queued")
    if outcome == "resolved":
        assert any(h.store.get(x["id"]).get("supersededBy") for x in (a, b))
    assert h.store.get(a["id"]) and h.store.get(b["id"]), "conflict resolution must never delete records"


@pytest.mark.parametrize("c", S["ledger-audit"], ids=ids("ledger-audit"))
def test_ledger_audit(c):
    h = HMEM(clock=lambda: T0)
    m = h.add(content="audited record", confidence=c["start"], source="conformance")
    h.feedback(m["id"], c["kind"])
    hist = h.ledger.history(m["id"])
    assert [e["action"] for e in hist] == c["expectActions"]
    assert hist[-1]["previousState"]["confidence"] == pytest.approx(c["expectPrev"])
    assert hist[-1]["newState"]["confidence"] == pytest.approx(c["expectNew"])


@pytest.mark.parametrize("c", S["dream-boundary"], ids=ids("dream-boundary"))
def test_dream_boundary(c):
    h = HMEM(clock=lambda: T0)
    for r in c["records"]:
        h.add(content=r["content"], type=r["type"], source="conformance")
    count = len(h.store.all())
    proposals = h.dreaming.dream()
    assert proposals, "two related preferences must produce at least one proposal"
    assert len(h.store.all()) == count, "dreaming must not materialize anything before approval"
    created = h.dreaming.approve(proposals[0]["id"])
    if created:
        assert created["layer"] == "long_term"
        assert created["derivedFrom"]


def test_persistence_roundtrip(tmp_path):
    """Feature-level check: a memory written in one session is recalled in the next (H-MEM's purpose)."""
    path = tmp_path / "mem.json"
    h1 = HMEM(path=path)
    h1.add(content="Trips on 2024-08-01 are partial: data ends mid-afternoon, so that day is not a real demand drop",
           source="critic", layer="long_term", type="factual", confidence=0.9)
    h1.save()
    h2 = HMEM(path=path)
    hits = h2.query("why did demand drop on 2024-08-01")
    assert hits["memories"] and "partial" in hits["memories"][0]["memory"]["content"]
    assert "Recalled memory" in hits["context"]
