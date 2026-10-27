# Demand-Shift Investigator

**Track 2 — The Agent That Can Explain Why.** A multi-agent system that helps a **NYC taxi fleet operations analyst**
answer *"why did demand change?"* questions (e.g. *"Why was Saturday 2024-07-27 so much quieter than Wednesday?"*).
It investigates real trip and weather data through **Zetaris**, has every claim challenged by an Evidence Critic,
and returns a cited explanation plus a staffing / dispatch recommendation. It **remembers** what earlier
investigations learned (data caveats, refuted explanations) using the **Meterless H-MEM engine**, and its reasoning
runs on **NVIDIA Nemotron via Meterless Nightshift**.

Why it matters: ops teams staff drivers on demand forecasts. When demand moves, the costly mistake is acting on the
wrong explanation — or on a data artefact (a partial day looks like a "collapse"). This agent refuses to report a cause
it cannot back with a query.

## How it works

```
            ┌────────────── shared case file (hypotheses, queries, critiques, caveats) ──────────────┐
question ─> Profiler ─> H-MEM recall ─> Planner ─> Analyst ⇄ Zetaris SQL ─> Evidence Critic ─┬─> Reporter ─> report.md
            (coverage)   (Meterless)    (3-4 competing   (writes SQL, repairs   accept / reject │
                                         hypotheses)      it on errors)          / revise ──────┘ (next round, ≤3)
                                                                                 + adds missing hypotheses
                                                                                 + records data caveats ──> H-MEM write-back
```

- **Planner** turns the question into a metric, a focus period, a fair baseline and competing hypotheses
  (calendar, weather, zone mix, airports, time of day, data coverage). It uses recalled memory.
- **Analyst (executor)** writes one aggregate Spark SQL query per hypothesis and runs it **in Zetaris**. On a SQL
  error it gets the error back and repairs the query (≤2 retries); then it summarises the evidence with numbers.
- **Profiler (deterministic tool step)** measures hour-level coverage per day (an hour counts only with at least half its normal volume, so export cut-offs are caught), computes the **like-for-like hour window** shared by the days in the question, and states a verdict (e.g. "whole-day -59%, same hours +2.1%: coverage artefact") that every agent sees. Coverage facts are mined into H-MEM.
- **Evidence audit (deterministic)** runs before the critic: every number in a finding must appear in that query's result rows, and month/season generalisations from a few days are flagged. A finding that fails the audit cannot be accepted (the code overrides an "accept" to "revise").
- **Evidence Critic (evaluator)** checks each finding: does the SQL test the claim, do the numbers match the rows,
  is the comparison like-for-like (same hours covered), is a data gap masquerading as a demand change? It can
  **accept**, **reject** or send a hypothesis **back for revision** with instructions, and can **add a missing
  hypothesis** (branching). Unproven claims never reach the report.
- **Reporter** writes the answer from accepted evidence only, citing query ids, with confidence, rejected
  explanations, caveats, limitations and a recommendation.
- **H-MEM** (Meterless engine) is recalled before planning and written after the critic rules: data caveats go to
  long-term memory, accepted findings and refuted explanations to working memory. The next investigation starts
  from them. Users can correct memory (`--feedback <id>:wrong`), which lowers its confidence exactly as the spec says.

Guardrails: read-only, allow-listed SQL; row caps; HTTP timeouts on every external call; retry caps; ≤3 critic rounds;
≤40 model calls and a 9-minute wall-clock cap per run; every step is traced to the console and to `trace.jsonl`.

## Sponsor technology

| Sponsor | Where | What it does |
|---|---|---|
| **Zetaris** (data) | `agent/tools_zetaris.py`, `data/setup/` | The single data layer. Three sources are federated in Zetaris and joined in one SQL query: real TLC trips + taxi zones (local Postgres registered with `CREATE DATASOURCE`), and **real NYC daily weather from the Open-Meteo REST API registered as a SQL table** (`CREATE LIGHTNING REST TABLE`), flattened with a schema-store view. Every fact in a report is a reproducible Zetaris query, shown in the report. |
| **Meterless Engine: H-MEM** (build) | `engines/hmem/hmem.py`, `tests/test_hmem_conformance.py` | Python implementation of the H-MEM spec: canonical records with provenance (source mandatory), tiered store, hybrid retrieval score (spec §6.1, exact weights), domain-grouped reinjection, feedback deltas, append-only trust ledger, conflict detection with the 0.70 auto-resolve gate, dreaming with approval boundary, sleep with backup. **Passes the official Meterless conformance vectors** (vendored unchanged, Apache-2.0). Used in the live loop for recall and write-back. |
| **Meterless Nightshift** (power) | `agent/llm.py` | The app's **primary and only AI provider** (OpenAI-compatible, `meterless-app` + `meterless-scope: user` headers), runtime token from env. |
| **NVIDIA Nemotron** | `agent/llm.py`, `.env` | Every agent runs on **Nemotron 3.5 Lightning 30B-A3B as a local lane on the team's DGX Spark** (`local-runtime/...`, Nightshift receipt: `provider: local-runtime, kind: local`), with reasoning switched off for short JSON replies (`enable_thinking: false`, which fixed empty replies). The last retry falls back to **Nemotron 3 Ultra 550B** on NVIDIA's cloud, through the same Nightshift endpoint. |
| **Cursor** | development | The project was developed in Cursor (see the write-up for how). |

## Setup

Prerequisites: Docker Desktop, Python 3.11+, the local Zetaris platform running (`zetaris-platform/`, `./verify.sh` 9/9),
and Meterless Nightshift running with a Nemotron lane.

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt     # Windows; use .venv/bin on Linux/macOS
cp .env.example .env        # fill in the Zetaris API key + Nightshift URL, Runtime token, Nemotron model id
python smoke_test.py        # 3 PASS: Zetaris web, Zetaris SQL, Nemotron via Nightshift
python data/setup/setup_data.py   # one command: download, load Postgres, register sources + views in Zetaris (idempotent)
```

## Usage

```bash
python -m agent.main "Why was taxi demand on Saturday 2024-07-27 so much lower than on Wednesday 2024-07-31?"
python -m agent.main "Did the rain on 2024-07-31 change where people took taxis?" --save rain_jul31
python -m agent.main --list-examples              # print input_examples/questions.md
python -m agent.main --memory                     # what the agent has learned so far
python -m agent.main --feedback mem-xxxx:wrong    # correct a memory
SAMPLE_MODE=true python -m agent.main "<a question from output_examples>" --save <name>   # replay, no model/network
python -m pytest -q                               # H-MEM conformance + offline loop tests
```

Each run writes `report.md` (answer, causes with evidence ids, recommendation, rejected explanations, caveats,
hypothesis table, every SQL query with its result), `case.json` (the shared case file) and `trace.jsonl`.

## Results (real runs, all agents on local Nemotron on the DGX Spark)

| # | Question | What the agents found | Loop behaviour | Run |
|---|---|---|---|---|
| 01 | Why was Saturday 2024-07-27 so much lower than Wednesday 2024-07-31? | Raw gap -39.2%, but Saturday's data only covers 12:00-23:59. In the **same hours** Saturday was still **15.5% lower** (59,736 vs 70,674 valid trips): a real weekday-vs-weekend effect on top of a coverage gap. Rain and airports refuted. | Profiler computes the like-for-like window; critic rejects a misread weather finding, adds H4, rejects it when its SQL fails twice | 19 calls, 12 queries, 166 s |
| 02 | Thursday 2024-08-01 looks like it collapsed. What happened? | **No collapse**: the export stops at 14:00. Whole-day -59%, but 00:00-13:59 like-for-like **+2.1%** (39,778 vs 38,972). | **Recalls run 01's coverage caveat from H-MEM** before planning; 3 critic rounds; audit + critic reject unverifiable claims | 19 calls, 12 queries, 157 s |
| 03 | Did the rain on 2024-07-31 change demand vs dry 2024-07-30? | Counter-intuitive: the rainy day had **4.1% more** trips (98,655 vs 94,765), airport trips up too; no broad geographic shift. | Critic catches invented zone counts, sends them back twice, then rejects the failed query; adds a day-of-week confounder hypothesis | 23 calls, 14 queries, 240 s |

Each folder in `output_examples/` has `report.md` (answer, causes with query ids, rejected explanations, caveats,
every SQL query with its rows), `case.json` (the shared case file) and `trace.jsonl` (every agent step).

**Replay without any infrastructure:** `SAMPLE_MODE=true python -m agent.main "<question from the table>" --save 01_saturday_vs_wednesday`
replays the recorded run call-by-call from `data/sample/llm_cache.json` (no model, no Zetaris, no network) in under a second.

## Data

See `data/README.md`. Real NYC TLC yellow-taxi trips (500,000 trips, 2024-07-27 to 2024-08-01, from Zetaris's public
sample bucket), the 260 TLC taxi zones, and Open-Meteo historical weather (CC BY 4.0). No personal data.

## Repository map

| Path | What |
|---|---|
| `agent/main.py` | the multi-agent loop, prompts, trace, report rendering, CLI |
| `agent/llm.py` | Nightshift provider: retries, timeouts, JSON parsing, record/replay cache (SAMPLE_MODE) |
| `agent/tools_zetaris.py` | Zetaris REST SQL tool with guardrails, table catalog for the agents |
| `engines/hmem/` | H-MEM engine + official conformance vectors |
| `data/setup/` | SQL + one-command setup |
| `input_examples/`, `output_examples/` | sample questions and full outputs (report, case file, trace) |
| `tests/` | H-MEM conformance, evidence `audit()` unit tests, offline loop test, SQL guard test |

## Limitations

One week of trips (Jul 27 – Aug 1 2024; the first and last days are partial), city-level daily weather, no events or
holiday calendar yet. Answers are as good as the SQL the analyst writes; the critic reduces but does not eliminate
wrong reasoning. Pre-existing components: none (open-source libraries only; the H-MEM conformance vectors are
Meterless's, Apache-2.0).
