"""Demand-change investigator: "why did NYC taxi demand change?" answered with evidence from Zetaris.

Agents share one case file (shared state) and loop, they do not run once in a fixed order:

  Profiler (tool) ─> Planner ─> Analyst ⇄ Zetaris SQL (retries on SQL errors)
                        ^           │
                        │           v
                        └──── Evidence Critic ── revise / reject / add hypothesis ──> Analyst (next round)
                                    │ approve
                                    v
                                 Reporter ─> report + H-MEM write-back (caveats, refuted hypotheses, findings)

H-MEM (Meterless engine, engines/hmem) is recalled before planning and written after the critic rules,
so the next investigation starts from what earlier ones learned.

Usage:  python -m agent.main "Why did taxi demand on Saturday 2024-07-27 look so low?" [--save NAME]
        python -m agent.main --feedback mem-xxxx:wrong
        SAMPLE_MODE=true python -m agent.main ...   (replays a recorded run, no model or network)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from agent import llm
from agent import tools_zetaris as z
from engines.hmem.hmem import HMEM

ROOT = Path(__file__).resolve().parents[1]
MEMORY_PATH = Path(os.environ.get("HMEM_PATH", ROOT / "data" / "memory" / "hmem.json"))
MAX_HYPOTHESES = 4
MAX_SQL_RETRIES = 2
MAX_ROUNDS = 3          # critic review rounds
MAX_LLM_CALLS = 40
MAX_WALL_S = 12 * 60

# ---------------------------------------------------------------------------------------------------
# Trace: every agent step is printed and saved, so the reasoning is visible.
# ---------------------------------------------------------------------------------------------------
COLORS = {"profiler": "36", "planner": "35", "analyst": "34", "zetaris": "32", "critic": "33",
          "reporter": "96", "memory": "95", "system": "90"}


class Trace:
    def __init__(self, quiet: bool = False):
        self.events: list[dict] = []
        self.t0 = time.time()
        self.quiet = quiet
        self.color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    def __call__(self, agent: str, kind: str, msg: str, **data):
        ev = {"t": round(time.time() - self.t0, 2), "agent": agent, "kind": kind, "msg": msg, **data}
        self.events.append(ev)
        if not self.quiet:
            tag = f"[{ev['t']:6.1f}s] {agent.upper():9} {kind:10}"
            if self.color:
                tag = f"\033[{COLORS.get(agent, '0')}m{tag}\033[0m"
            print(f"{tag} {msg}", flush=True)
        return ev


class Budget:
    def __init__(self, trace: Trace):
        self.calls, self.trace, self.t0 = 0, trace, time.time()

    def charge(self, role: str):
        self.calls += 1
        if self.calls > MAX_LLM_CALLS:
            raise RuntimeError(f"turn cap reached ({MAX_LLM_CALLS} model calls)")
        if time.time() - self.t0 > MAX_WALL_S:
            raise RuntimeError(f"wall-clock cap reached ({MAX_WALL_S}s)")


# ---------------------------------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------------------------------
PLANNER_SYS = """You are the Research Planner in a multi-agent investigation team for a NYC taxi fleet operations analyst.
Turn the analyst's question into a bounded investigation: the metric, the focus period and a fair baseline,
and 3-4 competing, testable hypotheses that could explain the change (e.g. calendar/day-of-week, weather,
geography/zone mix, airport traffic, time-of-day, data coverage/quality). Each hypothesis must be checkable
with ONE aggregate SQL query on the tables provided. Use recalled memory: do not re-test explanations memory
says were already refuted unless the question differs, and respect known data caveats.
Reply with ONLY a JSON object:
{"question_restated": str, "metric": str, "focus": str, "baseline": str,
 "hypotheses": [{"id": "H1", "claim": str, "test": str}], "memory_used": [memory ids you relied on]}"""

ANALYST_SQL_SYS = """You are the Data Analyst. Write ONE Spark SQL query (Zetaris Lightning) that tests the hypothesis.
Aggregate with GROUP BY; compare the focus period against the baseline in the same query when possible.
Never select raw trip rows. Use only the tables in the catalog.
Reply with ONLY a JSON object: {"sql": str, "why": str}"""

ANALYST_SUM_SYS = """You are the Data Analyst. Summarise what the query result shows about the hypothesis.
Quote the key numbers exactly as they appear in the result. Do not claim anything the rows do not show.
Reply with ONLY a JSON object:
{"finding": str (1-3 sentences with numbers), "key_numbers": {name: number}, "direction": "supports"|"refutes"|"inconclusive"}"""

CRITIC_SYS = """You are the Evidence Critic. Your job is to stop unsupported or misleading explanations reaching the analyst.
Be sceptical: accepting a flawed finding is worse than sending it back. For each finding check, explicitly:
1. Numbers: every number in the analyst's finding must appear in (or be computable from) the result rows. A wrong,
   invented or self-contradictory number (e.g. "higher (6100 vs 10068)") means "revise".
2. Scope: the data covers only the days in the coverage profile. Claims about a month, a season or "all Saturdays"
   from one or two days are over-generalised -> "revise" (ask to restate the claim for the days actually observed).
3. Test validity: does the SQL actually test the claim, with the right filters and a fair, like-for-like comparison
   (same hours covered, same hour window)? A partial day compared with a full day without normalising -> "revise".
4. Is a data coverage gap (partial days, missing hours) masquerading as a demand change? Is the effect size meaningful?
Verdicts: "accept" (evidence supports or cleanly refutes the claim), "revise" (the test is flawed; say exactly how
to fix it), "reject" (claim unsupported / evidence contradicts it). You may propose ONE missing hypothesis
if an important explanation was not tested. List data caveats you found (they will be remembered for future runs).
Reply with ONLY a JSON object:
{"reviews": [{"id": str, "verdict": "accept"|"revise"|"reject", "reason": str, "revision_request": str}],
 "missing_hypothesis": {"claim": str, "test": str} or null,
 "data_caveats": [str], "decision": "approve"|"another_round"}"""

REPORTER_SYS = """You are the Report Writer. Write the final answer for a taxi fleet operations analyst.
Use ONLY findings the critic accepted; cite them by their query ids (e.g. Q3). Rank causes by impact.
State rejected explanations briefly, data caveats, limitations and an actionable staffing/dispatch recommendation.
Reply with ONLY a JSON object:
{"answer": str (2-4 sentences), "causes": [{"cause": str, "impact": str, "evidence": [query ids], "confidence": number 0-1}],
 "rejected_explanations": [str], "data_caveats": [str], "recommendation": str, "confidence": number 0-1, "limitations": [str]}"""


# ---------------------------------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------------------------------
class Investigation:
    def __init__(self, question: str, trace: Trace, hmem: HMEM):
        self.q, self.trace, self.hmem = question, trace, hmem
        self.budget = Budget(trace)
        self.case = {"question": question, "profile": None, "memory": None, "plan": None,
                     "hypotheses": {}, "queries": {}, "rounds": [], "report": None}
        self.qn = 0

    # -- helpers -----------------------------------------------------------------------------------
    def ask(self, role: str, system: str, user: str) -> dict:
        self.budget.charge(role)
        r = llm.chat(role, system, user)
        self.trace(role, "model", f"{r['model']} replied in {r.get('latency_s', 0)}s"
                   + (" (replayed)" if r.get("cached") else ""), model=r["model"])
        return r["json"]

    def sql(self, agent: str, sql: str, purpose: str) -> tuple[str, dict]:
        self.qn += 1
        qid = f"Q{self.qn}"
        self.trace("zetaris", "query", f"{qid} ({agent}: {purpose})\n            {_one_line(sql)}", qid=qid, sql=sql)
        res = z.query(sql)
        self.trace("zetaris", "result", f"{qid} -> {len(res['rows'])} rows in {res.get('elapsed_s', 0)}s"
                   + (" (replayed)" if res.get("cached") else ""), qid=qid)
        self.case["queries"][qid] = {"sql": res["sql"], "columns": res["columns"], "rows": res["rows"][:60],
                                     "agent": agent, "purpose": purpose}
        return qid, res

    # -- steps -------------------------------------------------------------------------------------
    def profile(self):
        """Deterministic tool step: coverage per day, so nobody mistakes a data gap for a demand drop."""
        qid, res = self.sql("profiler", "SELECT pickup_date, pickup_dow, COUNT(*) AS trips, MIN(pickup_ts) AS first_pickup, "
                            "MAX(pickup_ts) AS last_pickup, COUNT(DISTINCT pickup_hour) AS hours_covered "
                            "FROM investigator.trips GROUP BY pickup_date, pickup_dow ORDER BY pickup_date",
                            "data coverage per day")
        self.case["profile"] = {"qid": qid, "table": z.preview(res)}
        partial = [r[0] for r in res["rows"] if int(r[5]) < 24]
        self.trace("profiler", "coverage", f"{len(res['rows'])} days; days with <24 hours of data: {partial or 'none'}")

    def recall(self):
        """Two H-MEM retrievals merged: one for the question, one aimed at known data caveats for its dates."""
        hits = self.hmem.query(self.q, actor="planner")
        dates = " ".join(re.findall(r"\d{4}-\d{2}-\d{2}", self.q))
        cav = self.hmem.query(f"data caveat coverage partial missing hours gap {dates}", actor="planner")
        seen, merged = set(), []
        for h in sorted(hits["memories"] + cav["memories"], key=lambda h: -h["score"]):
            if h["memory"]["id"] not in seen:
                seen.add(h["memory"]["id"])
                merged.append(h)
        merged = merged[:8]
        self.case["memory"] = {"context": HMEM.format_context(merged), "ids": [h["memory"]["id"] for h in merged],
                               "reason": hits["retrievalReason"] + " | + caveat recall", "strategy": hits["strategy"]}
        if merged:
            for h in merged:
                self.trace("memory", "recall", f"{h['score']:.2f} [{h['memory']['layer']}] {h['memory']['content'][:140]}",
                           memory_id=h["memory"]["id"], parts=h["parts"])
        else:
            self.trace("memory", "recall", "no relevant memories yet (first investigation of this kind)")

    def plan(self):
        user = (f"Question: {self.q}\n\n{z.CATALOG}\n\nData coverage profile ({self.case['profile']['qid']}):\n"
                f"{self.case['profile']['table']}\n\n{self.case['memory']['context'] or 'Recalled memory: none'}")
        p = self.ask("planner", PLANNER_SYS, user)
        self.case["plan"] = p
        self.trace("planner", "plan", f"metric={p.get('metric')} | focus={p.get('focus')} | baseline={p.get('baseline')}")
        for h in p.get("hypotheses", [])[:MAX_HYPOTHESES]:
            self.add_hypothesis(h["claim"], h.get("test", ""), h.get("id"))
        if p.get("memory_used"):
            self.trace("planner", "memory", f"plan relies on memories {p['memory_used']}")

    def add_hypothesis(self, claim: str, test: str, hid: str | None = None, origin: str = "planner"):
        hid = hid or f"H{len(self.case['hypotheses']) + 1}"
        while hid in self.case["hypotheses"]:
            hid = f"H{len(self.case['hypotheses']) + 1}"
        self.case["hypotheses"][hid] = {"id": hid, "claim": claim, "test": test, "origin": origin, "status": "open",
                                        "attempts": [], "critique": None}
        self.trace(origin, "hypothesis", f"{hid}: {claim}")

    def analyse(self, h: dict, revision: str | None = None):
        """Executor: write SQL, run it in Zetaris, repair on errors, then summarise the evidence."""
        base = (f"{z.CATALOG}\n\nInvestigation: {self.case['plan'].get('question_restated', self.q)}\n"
                f"Focus: {self.case['plan'].get('focus')} | Baseline: {self.case['plan'].get('baseline')}\n"
                f"Hypothesis {h['id']}: {h['claim']}\nSuggested test: {h['test']}\n")
        if revision:
            base += f"\nThe Evidence Critic rejected the previous test. Fix it as requested: {revision}\n"
            if h["attempts"]:
                base += f"Previous SQL:\n{h['attempts'][-1]['sql']}\n"
        error = None
        for attempt in range(MAX_SQL_RETRIES + 1):
            prompt = base + (f"\nYour previous SQL failed in Zetaris with: {error}\nWrite a corrected query." if error else "")
            out = self.ask("analyst", ANALYST_SQL_SYS, prompt)
            sql = out.get("sql", "")
            try:
                qid, res = self.sql("analyst", sql, f"test {h['id']}")
                break
            except Exception as e:
                error = str(e)[:300]
                self.trace("analyst", "retry", f"{h['id']} SQL failed (attempt {attempt + 1}): {error}")
        else:
            h["status"] = "failed"
            h["attempts"].append({"sql": sql, "error": error})
            self.trace("analyst", "give-up", f"{h['id']}: no working query after {MAX_SQL_RETRIES + 1} attempts; escalating to critic")
            return
        summ = self.ask("analyst", ANALYST_SUM_SYS,
                        f"Hypothesis {h['id']}: {h['claim']}\nSQL ({qid}):\n{res['sql']}\n\nResult:\n{z.preview(res)}")
        h["attempts"].append({"qid": qid, "sql": res["sql"], "finding": summ.get("finding"),
                              "key_numbers": summ.get("key_numbers"), "direction": summ.get("direction")})
        h["status"] = "analysed"
        self.trace("analyst", "finding", f"{h['id']} [{summ.get('direction')}] {summ.get('finding')}")

    def critique(self, rnd: int) -> dict:
        items = []
        for h in self.case["hypotheses"].values():
            if h["status"] in ("accepted", "rejected"):
                continue
            last = h["attempts"][-1] if h["attempts"] else {}
            rows = z.preview({"columns": self.case["queries"][last["qid"]]["columns"],
                              "rows": self.case["queries"][last["qid"]]["rows"]}, 15) if last.get("qid") else "(no result)"
            if last.get("qid"):
                q = self.case["queries"][last["qid"]]
                au = audit(last.get("finding") or "", q["columns"], q["rows"])
            else:
                au = {"ok": False, "unverified_numbers": [], "scope_flags": ["no result"]}
            h["audit"] = au
            if not au["ok"]:
                self.trace("critic", "audit", f"{h['id']}: numbers not in the result rows {au['unverified_numbers'] or '-'}; "
                           f"over-general scope {au['scope_flags'] or '-'}")
            verdict_txt = "passed" if au["ok"] else (f"FAILED - numbers not found in the result rows: {au['unverified_numbers']}; "
                                                     f"over-general scope words: {au['scope_flags']}")
            items.append(f"### {h['id']}: {h['claim']}\nQuery {last.get('qid', '-')}:\n{last.get('sql', '')}\n"
                         f"Result rows:\n{rows}\nAnalyst finding: {last.get('finding') or last.get('error')}\n"
                         f"Automatic evidence audit: {verdict_txt}")
        if not items:
            return {"decision": "approve", "reviews": []}
        user = (f"Question: {self.q}\nFocus: {self.case['plan'].get('focus')} | Baseline: {self.case['plan'].get('baseline')}\n\n"
                f"Data coverage profile:\n{self.case['profile']['table']}\n\n"
                f"{self.case['memory']['context'] or ''}\n\nReview round {rnd} of {MAX_ROUNDS}. Findings:\n\n" + "\n\n".join(items))
        c = self.ask("critic", CRITIC_SYS, user)
        self.case["rounds"].append(c)
        for r in c.get("reviews", []):
            h = self.case["hypotheses"].get(r.get("id"))
            if not h:
                continue
            h["critique"] = r
            verdict = r.get("verdict", "revise")
            au = h.get("audit") or {"ok": True}
            if verdict == "accept" and not au["ok"] and rnd < MAX_ROUNDS:
                verdict = "revise"   # guardrail: a finding that fails the evidence audit cannot be accepted
                r["reason"] = (f"evidence audit failed (unverified numbers {au['unverified_numbers']}, scope "
                               f"{au['scope_flags']}); critic had said: {r.get('reason')}")
                missing = f" (not found: {', '.join(au['unverified_numbers'])})" if au["unverified_numbers"] else ""
                r["revision_request"] = ("Restate the finding using only numbers that appear in the result rows" + missing +
                                         "; describe only the days actually observed, no month/season generalisation. "
                                         "If a number is needed that the query does not return, change the query to return it.")
            if verdict == "revise" and rnd >= MAX_ROUNDS:
                verdict = "reject"  # out of rounds: unproven claims do not reach the report
                r["reason"] = (r.get("reason") or "") + " (no rounds left to fix the test)"
            h["status"] = {"accept": "accepted", "reject": "rejected"}.get(verdict, "revise")
            self.trace("critic", verdict, f"{h['id']}: {r.get('reason')}"
                       + (f"\n            -> revision: {r.get('revision_request')}" if verdict == "revise" else ""))
        for cav in c.get("data_caveats", []) or []:
            self.trace("critic", "caveat", cav)
        mh = c.get("missing_hypothesis")
        if mh and isinstance(mh, dict) and mh.get("claim") and rnd < MAX_ROUNDS \
                and sum(1 for h in self.case["hypotheses"].values() if h["origin"] == "critic") < 2:
            self.add_hypothesis(mh["claim"], mh.get("test", ""), origin="critic")
        return c

    def report(self):
        accepted = [h for h in self.case["hypotheses"].values() if h["status"] == "accepted"]
        rejected = [h for h in self.case["hypotheses"].values() if h["status"] != "accepted"]
        fmt = lambda h: (f"{h['id']} ({h['status']}): {h['claim']}\n  evidence {h['attempts'][-1].get('qid') if h['attempts'] else '-'}: "
                         f"{(h['attempts'][-1].get('finding') if h['attempts'] else '')}\n  critic: {(h['critique'] or {}).get('reason')}")
        caveats = [c for r in self.case["rounds"] for c in (r.get("data_caveats") or [])]
        user = (f"Question: {self.q}\nFocus: {self.case['plan'].get('focus')} | Baseline: {self.case['plan'].get('baseline')}\n\n"
                "ACCEPTED findings:\n" + ("\n".join(fmt(h) for h in accepted) or "(none)") +
                "\n\nNOT accepted:\n" + ("\n".join(fmt(h) for h in rejected) or "(none)") +
                "\n\nCritic data caveats:\n" + ("\n".join(f"- {c}" for c in caveats) or "(none)"))
        rep = self.ask("reporter", REPORTER_SYS, user)
        self.case["report"] = rep
        self.trace("reporter", "answer", rep.get("answer", ""))

    def remember(self):
        """H-MEM write-back: caveats and refuted explanations are what make the next run better."""
        mission = re.sub(r"\W+", "-", self.q.lower())[:60]
        written = []
        for r in self.case["rounds"]:
            for cav in r.get("data_caveats") or []:
                written.append(self.hmem.add(content=f"Data caveat: {cav}", source="critic", layer="long_term",
                                             type="factual", confidence=0.85, tags=["caveat", "domain:data_quality"],
                                             missionId=None, actor="critic"))
        for h in self.case["hypotheses"].values():
            last = h["attempts"][-1] if h["attempts"] else {}
            if h["status"] == "accepted":
                written.append(self.hmem.add(
                    content=f"Finding for '{self.q}': {h['claim']} -> {last.get('finding')} (evidence {last.get('qid')})",
                    source="plan_completion", layer="working", type="factual", confidence=0.75,
                    tags=["finding"], missionId=mission, actor="critic"))
            elif h["status"] == "rejected":
                written.append(self.hmem.add(
                    content=f"Refuted for '{self.q}': {h['claim']} - critic: {(h['critique'] or {}).get('reason')}",
                    source="critic", layer="working", type="factual", confidence=0.7, tags=["refuted"],
                    missionId=mission, actor="critic"))
        for conflict in self.hmem.conflicts.scan():
            outcome = self.hmem.conflicts.autoResolve(conflict)
            self.trace("memory", "conflict", f"{conflict['reason']} -> {outcome}")
        self.hmem.save()
        self.trace("memory", "write", f"stored {len(written)} memories (caveats, findings, refuted hypotheses) "
                   f"-> {MEMORY_PATH.relative_to(ROOT) if MEMORY_PATH.is_relative_to(ROOT) else MEMORY_PATH}")
        self.case["memory_written"] = [m["id"] for m in written]

    # -- the loop ----------------------------------------------------------------------------------
    def run(self) -> dict:
        self.trace("system", "start", f"question: {self.q}  (provider: Meterless Nightshift, "
                   f"model: {os.environ.get('NEMOTRON_MODEL', '?')}, sample_mode={llm.sample_mode()})")
        self.profile()
        self.recall()
        self.plan()
        for rnd in range(1, MAX_ROUNDS + 1):
            todo = [h for h in self.case["hypotheses"].values() if h["status"] in ("open", "revise")]
            for h in todo:
                self.analyse(h, revision=(h["critique"] or {}).get("revision_request") if h["status"] == "revise" else None)
            c = self.critique(rnd)
            pending = [h for h in self.case["hypotheses"].values() if h["status"] in ("open", "revise")]
            if not pending:
                self.trace("critic", "approve", f"round {rnd}: all hypotheses ruled on")
                break
            self.trace("system", "loop", f"round {rnd} -> {len(pending)} hypotheses go back to the analyst")
        self.report()
        self.remember()
        self.case["stats"] = {"llm_calls": self.budget.calls, "sql_queries": self.qn,
                              "rounds": len(self.case["rounds"]), "seconds": round(time.time() - self.trace.t0, 1)}
        self.trace("system", "done", json.dumps(self.case["stats"]))
        return self.case


SCOPE_WORDS = re.compile(r"\b(month|monthly|season|seasonal|all (?:saturdays|sundays|weekdays|weekends)|"
                         r"every (?:saturday|sunday|week)|typical(?:ly)?|in general|across july|july 2024 days)\b", re.I)


def audit(finding: str, columns: list, rows: list) -> dict:
    """Deterministic evidence audit, run before the critic: every number quoted in a finding must match a value in
    the query result (to rounding), and scope words must not generalise beyond the observed days."""
    vals = []
    for r in rows:
        for v in r:
            try:
                vals.append(float(str(v).replace(",", "")))
            except ValueError:
                pass
    text = re.sub(r"\d{4}-\d{2}-\d{2}|\b\d{1,2}:\d{2}\b|\b[HQ]\d+\b|wmo_code \d+|"
                  r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|"
                  r"\b(?:19|20)\d{2}\b", " ", finding or "", flags=re.I)
    unverified = []
    for m in re.finditer(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(\s*%)?", text):
        if m.group(2):                       # percentages are derived; the critic judges those
            continue
        x = float(m.group(1).replace(",", ""))
        if x < 25 and x == int(x):           # small counts (hours, days, ranks) are too ambiguous to audit
            continue
        if not any(abs(x - v) <= max(0.051, 0.005 * abs(v)) for v in vals):
            unverified.append(m.group(1))
    scope = sorted({w.lower() for w in SCOPE_WORDS.findall(finding or "")})
    return {"unverified_numbers": unverified, "scope_flags": scope, "ok": not unverified and not scope}


def _one_line(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()[:400]


# ---------------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------------
def render_markdown(case: dict) -> str:
    rep = case.get("report") or {}
    out = [f"# {case['question']}", "", f"**Answer.** {rep.get('answer', '')}", "",
           f"**Confidence:** {rep.get('confidence', '?')}", "", "## Causes (critic-accepted evidence only)"]
    for c in rep.get("causes", []):
        out.append(f"- **{c.get('cause')}** - {c.get('impact')} (evidence: {', '.join(c.get('evidence', []))}; "
                   f"confidence {c.get('confidence')})")
    out += ["", "## Recommendation", rep.get("recommendation", ""), "", "## Rejected explanations"]
    out += [f"- {x}" for x in rep.get("rejected_explanations", [])] or ["- none"]
    out += ["", "## Data caveats"] + ([f"- {x}" for x in rep.get("data_caveats", [])] or ["- none"])
    out += ["", "## Limitations"] + ([f"- {x}" for x in rep.get("limitations", [])] or ["- none"])
    out += ["", "## Hypotheses and critic rulings", "| id | origin | claim | status | evidence | critic |", "|---|---|---|---|---|---|"]
    for h in case["hypotheses"].values():
        last = h["attempts"][-1] if h["attempts"] else {}
        out.append(f"| {h['id']} | {h['origin']} | {h['claim']} | {h['status']} | {last.get('qid', '-')} | "
                   f"{((h['critique'] or {}).get('reason') or '').replace('|', '/')} |")
    out += ["", "## Evidence (every query ran in Zetaris)"]
    for qid, q in case["queries"].items():
        out += [f"### {qid} - {q['agent']}: {q['purpose']}", "```sql", q["sql"], "```",
                z.preview({"columns": q["columns"], "rows": q["rows"]}, 12).replace("\n", "  \n"), ""]
    if case.get("memory", {}).get("context"):
        out += ["## Memory recalled before planning (H-MEM)", case["memory"]["context"], ""]
    out += [f"_Stats: {case.get('stats')}_"]
    return "\n".join(out)


def save(case: dict, trace: Trace, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_markdown(case), encoding="utf-8")
    (out_dir / "case.json").write_text(json.dumps(case, indent=1, default=str), encoding="utf-8")
    with open(out_dir / "trace.jsonl", "w", encoding="utf-8") as f:
        for ev in trace.events:
            f.write(json.dumps(ev, default=str) + "\n")
    print(f"\nSaved report, case file and trace to {out_dir}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Why did NYC taxi demand change? (multi-agent, evidence from Zetaris)")
    ap.add_argument("question", nargs="?", help="the question to investigate")
    ap.add_argument("--save", help="save under output_examples/<NAME> (default: runs/<timestamp>)")
    ap.add_argument("--feedback", help="memory feedback, e.g. mem-abc123:wrong (helpful|not_helpful|wrong)")
    ap.add_argument("--memory", action="store_true", help="list stored memories")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    hmem = HMEM(path=MEMORY_PATH)
    if a.feedback:
        mid, kind = a.feedback.split(":")
        m = hmem.feedback(mid, kind)
        hmem.save()
        print(f"{mid}: confidence now {m['confidence']:.2f}, tags {m['tags']}")
        return
    if a.memory:
        for m in sorted(hmem.store.all(), key=lambda m: -m["confidence"]):
            print(f"{m['id']} [{m['layer']}|{m['confidence']:.2f}|{m['source']}] {m['content'][:150]}")
        return
    if not a.question:
        ap.error("give a question")
    out = ROOT / ("output_examples/" + a.save if a.save else f"runs/{datetime.now():%Y%m%d-%H%M%S}")
    snapshot = out / "memory_before.json"
    llm.RUN.update(id=re.sub(r"\W+", "-", a.question.lower()).strip("-"), n=0)
    if llm.sample_mode():
        # Replay from the memory state the run was recorded with; never write to the live memory file.
        hmem = HMEM(path=snapshot if snapshot.exists() else None)
        hmem.store.path = None
    elif a.save:
        out.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(MEMORY_PATH.read_text(encoding="utf-8") if MEMORY_PATH.exists() else '{"memories": [], "ledger": []}',
                            encoding="utf-8")
    trace = Trace(quiet=a.quiet)
    inv = Investigation(a.question, trace, hmem)
    try:
        case = inv.run()
    except Exception as e:  # recovery: keep everything gathered so far, and say what failed
        trace("system", "error", f"{type(e).__name__}: {e}")
        case = inv.case
        case["error"] = str(e)
        try:
            hmem.save()
        except Exception:
            pass
    if llm.sample_mode():
        out = out.parent / (out.name + "-replay") if a.save else out
    save(case, trace, out)
    if case.get("report"):
        print("\n" + "=" * 80 + "\n" + (case["report"].get("answer") or "") + "\n" + "=" * 80)
    return 0 if case.get("report") else 1


if __name__ == "__main__":
    sys.exit(main())
