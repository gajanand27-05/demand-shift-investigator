"""H-MEM (Meterless Human Memory engine), Python implementation of engines/hmem/AGENTS.md.

Implements the spec's load-bearing parts:
  - canonical memory record with provenance and lineage (source is mandatory)
  - tiered store (short_term / working / long_term) with JSON persistence
  - enrichment: domain, namespace, entities, relatedTo
  - hybrid retrieval score (section 6.1) and domain-grouped reinjection with trace metadata
  - feedback deltas (section 9.2) and an append-only trust ledger (section 9.1)
  - conflict detection + auto-resolve gate at 0.70; losers are marked supersededBy, never deleted
  - dreaming (proposals need approval) and sleep (preview -> execute with backup/restore)

Verified by tests/test_hmem_conformance.py against the official conformance vectors.
"""
from __future__ import annotations

import copy
import json
import math
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable

DAY_MS = 24 * 60 * 60 * 1000
LAYERS = ("short_term", "working", "long_term")
LAYER_WEIGHT = {"long_term": 1.0, "working": 0.8, "short_term": 0.6}
TYPES = ("personal", "factual", "preference", "general")
LEDGER_ACTIONS = {
    "create", "read", "update", "delete", "feedback", "promote", "demote", "merge",
    "conflict_detected", "conflict_resolved", "sleep_consolidate", "sleep_archive",
    "sleep_synthesize", "dream_proposed", "dream_approved", "dream_rejected", "restore",
}
FEEDBACK = {"helpful": 0.05, "not_helpful": -0.03, "wrong": -0.20}

# Operational defaults (section 13).
TOP_N = 5
THRESHOLD = 0.35
CONSOLIDATE_DAYS = 7
ARCHIVE_DAYS = 30
SYNTH_SIM = 0.82
DREAM_RELATED = 0.35
MAX_DREAM_PER_TYPE = 8
AUTO_RESOLVE_GATE = 0.70
ENTITY_CAP = 10
RELATED_CAP = 5
RECENCY_TAU_DAYS = 14

STOPWORDS = set(
    "a an the and or of to in on at for with by is are was were be been it this that these those "
    "we you they i he she as from not no do does did so than then there here what when why how which "
    "who whom into over under about after before up down out if".split()
)
OPPOSING = [("always", "never"), ("enable", "disable"), ("enabled", "disabled"), ("true", "false"),
            ("increase", "decrease"), ("increased", "decreased"), ("up", "down"), ("rise", "fall"),
            ("more", "fewer"), ("higher", "lower"), ("complete", "partial"), ("supported", "unsupported")]
DOMAINS = {
    "data_quality": {"partial", "missing", "coverage", "incomplete", "truncated", "gap", "quality", "caveat"},
    "demand": {"demand", "trips", "pickups", "rides", "volume", "drop", "spike", "surge"},
    "weather": {"rain", "weather", "precipitation", "storm", "temperature", "heat", "snow", "wind"},
    "geography": {"zone", "zones", "borough", "manhattan", "queens", "brooklyn", "bronx", "airport", "jfk", "laguardia"},
    "time": {"weekend", "weekday", "hour", "morning", "evening", "night", "saturday", "sunday", "monday",
             "tuesday", "wednesday", "thursday", "friday", "holiday"},
    "method": {"sql", "query", "method", "approach", "hypothesis", "baseline", "compare"},
}


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9_\-:.]*", text.lower()) if t not in STOPWORDS]


def embed(text: str) -> dict[str, float]:
    """Lightweight embedding: L2-normalised bag of word tokens + character trigrams."""
    feats: Counter = Counter()
    for t in tokens(text):
        feats["w:" + t] += 1.0
        padded = f"#{t}#"
        for i in range(len(padded) - 2):
            feats["c:" + padded[i:i + 3]] += 0.3
    norm = math.sqrt(sum(v * v for v in feats.values())) or 1.0
    return {k: v / norm for k, v in feats.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def infer_domain(text: str, tags: list[str] | None = None) -> str:
    toks = set(tokens(text)) | {t.split(":", 1)[-1] for t in (tags or [])}
    for tag in tags or []:
        if tag.startswith("domain:"):
            return tag.split(":", 1)[1]
    best, best_score = "general", 0
    for name, words in DOMAINS.items():
        score = len(toks & words)
        if score > best_score:
            best, best_score = name, score
    return best


def extract_entities(text: str) -> list[dict]:
    found: list[dict] = []
    for d in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text):
        found.append({"name": d, "type": "date"})
    for name in re.findall(r"\b([A-Z][a-zA-Z]+(?:[ /][A-Z][a-zA-Z]+)*)\b", text):
        if name.lower() not in STOPWORDS and len(name) > 2:
            found.append({"name": name.lower(), "type": "concept"})
    for t in tokens(text):
        for dom, words in DOMAINS.items():
            if t in words and dom in ("geography", "weather", "time"):
                found.append({"name": t, "type": dom})
    seen, out = set(), []
    for e in found:
        if e["name"] not in seen:
            seen.add(e["name"])
            out.append(e)
    return out[:ENTITY_CAP]


class TrustLedger:
    """Append-only audit log (section 9.1)."""

    def __init__(self, clock: Callable[[], int]):
        self.clock = clock
        self.entries: list[dict] = []

    def log(self, memory_id: str, action: str, actor: str = "system", previous=None, new=None, **details) -> dict:
        assert action in LEDGER_ACTIONS, action
        entry = {"memoryId": memory_id, "action": action, "actor": actor, "timestamp": self.clock(),
                 "previousState": copy.deepcopy(previous), "newState": copy.deepcopy(new), "details": details}
        self.entries.append(entry)
        return entry

    def history(self, memory_id: str) -> list[dict]:
        return [e for e in self.entries if e["memoryId"] == memory_id]

    def stats(self) -> dict:
        return dict(Counter(e["action"] for e in self.entries))


class MemoryStore:
    """Tiered persistence (section 3). long_term + working persist to JSON; short_term is session-only."""

    def __init__(self, clock: Callable[[], int], ledger: TrustLedger, path: str | Path | None = None):
        self.clock, self.ledger = clock, ledger
        self.path = Path(path) if path else None
        self._mem: dict[str, dict] = {}
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for m in data.get("memories", []):
                m.setdefault("embedding", embed(m["content"]))
                self._mem[m["id"]] = m
            self.ledger.entries.extend(data.get("ledger", []))
            self._decay()

    def _decay(self) -> None:
        """Forgetting curve for non-long-term records: confidence fades with time since last access."""
        now = self.clock()
        for m in self._mem.values():
            if m["layer"] != "long_term":
                idle_days = max(0.0, (now - m["lastAccessed"]) / DAY_MS)
                m["confidence"] = clamp01(m["confidence"] * math.exp(-idle_days / 60))

    def save(self) -> None:
        if not self.path:
            return
        keep = [{k: v for k, v in m.items() if k not in ("embedding", "score")}
                for m in self._mem.values() if m["layer"] in ("working", "long_term")]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"memories": keep, "ledger": self.ledger.entries}, indent=1), encoding="utf-8")

    def get(self, memory_id: str) -> dict | None:
        return self._mem.get(memory_id)

    def all(self) -> list[dict]:
        return list(self._mem.values())

    def put(self, m: dict) -> None:
        self._mem[m["id"]] = m

    def delete(self, memory_id: str, actor: str = "system") -> None:
        m = self._mem.pop(memory_id, None)
        if m:
            self.ledger.log(memory_id, "delete", actor, previous={"content": m["content"]})


class ConflictService:
    """Contradiction scan + resolution (section 10)."""

    def __init__(self, hmem: "HMEM"):
        self.h = hmem
        self.records: list[dict] = []

    @staticmethod
    def _strip(text: str, words: set[str]) -> str:
        return " ".join(t for t in tokens(text) if t not in words)

    def scan(self) -> list[dict]:
        found = []
        mems = [m for m in self.h.store.all() if not m.get("supersededBy")]
        for i, a in enumerate(mems):
            for b in mems[i + 1:]:
                if a.get("supersedes") == b["id"] or b.get("supersedes") == a["id"]:
                    continue  # intended replacement, not a conflict
                c = self._compare(a, b)
                if c:
                    found.append(c)
                    self.h.ledger.log(a["id"], "conflict_detected", details_other=b["id"], reason=c["reason"])
        self.records.extend(found)
        return found

    def _compare(self, a: dict, b: dict) -> dict | None:
        ta, tb = set(tokens(a["content"])), set(tokens(b["content"]))
        for x, y in OPPOSING:
            if (x in ta and y in tb) or (y in ta and x in tb):
                sim = cosine(embed(self._strip(a["content"], {x, y})), embed(self._strip(b["content"], {x, y})))
                if sim >= 0.6:
                    conf = 0.5 + 0.3 * sim
                    if {e["name"] for e in a.get("entities", [])} & {e["name"] for e in b.get("entities", [])}:
                        conf += 0.1
                    if a.get("domain") == b.get("domain"):
                        conf += 0.05
                    return {"id": "cf-" + uuid.uuid4().hex[:8], "a": a["id"], "b": b["id"], "type": "opposing_phrase",
                            "reason": f"'{x}' vs '{y}' on similar content", "confidence": clamp01(conf)}
        na = re.findall(r"\d+(?:\.\d+)?", a["content"])
        nb = re.findall(r"\d+(?:\.\d+)?", b["content"])
        if na and nb and na != nb:
            sim = cosine(embed(re.sub(r"\d+(?:\.\d+)?", "", a["content"])), embed(re.sub(r"\d+(?:\.\d+)?", "", b["content"])))
            if sim >= 0.9:
                return {"id": "cf-" + uuid.uuid4().hex[:8], "a": a["id"], "b": b["id"], "type": "numeric",
                        "reason": "same claim with different numbers", "confidence": clamp01(0.4 + 0.4 * sim)}
        return None

    def decide(self, conflict: dict) -> tuple[str, str, float]:
        """Weighted heuristics -> (winner_id, loser_id, decision_confidence)."""
        a, b = self.h.store.get(conflict["a"]), self.h.store.get(conflict["b"])
        now = self.h.clock()

        def strength(m: dict) -> float:
            recency = math.exp(-(now - m["timestamp"]) / (RECENCY_TAU_DAYS * DAY_MS))
            source_rel = 1.0 if m["source"] in ("user_correction", "critic") else 0.6
            return (0.45 * m["confidence"] + 0.15 * recency + 0.1 * min(1.0, m["accessCount"] / 5)
                    + 0.1 * min(1.0, len(m["content"]) / 200) + 0.2 * source_rel)

        sa, sb = strength(a), strength(b)
        winner, loser = (a, b) if sa >= sb else (b, a)
        margin = abs(sa - sb) / max(sa, sb, 1e-9)
        decision = clamp01(0.5 + margin) * conflict["confidence"] ** 0.25
        return winner["id"], loser["id"], decision

    def resolve(self, conflict: dict, strategy: str, actor: str = "human") -> None:
        a, b = self.h.store.get(conflict["a"]), self.h.store.get(conflict["b"])
        if strategy in ("keep_a", "keep_b"):
            winner, loser = (a, b) if strategy == "keep_a" else (b, a)
            prev = {"supersededBy": loser.get("supersededBy")}
            loser["supersededBy"] = winner["id"]
            winner["supersedes"] = loser["id"]
            self.h.ledger.log(loser["id"], "conflict_resolved", actor, previous=prev,
                              new={"supersededBy": winner["id"]}, strategy=strategy)
        elif strategy == "merge":
            merged = self.h.add(content=f"{a['content']} / {b['content']}", source="conflict_merge",
                                layer="long_term", derivedFrom=[a["id"], b["id"]])
            for m in (a, b):
                m["supersededBy"] = merged["id"]
                self.h.ledger.log(m["id"], "conflict_resolved", actor, new={"supersededBy": merged["id"]}, strategy="merge")
        elif strategy == "delete_both":
            self.h.store.delete(a["id"], actor)
            self.h.store.delete(b["id"], actor)
        else:  # keep_both
            self.h.ledger.log(a["id"], "conflict_resolved", actor, strategy="keep_both", other=b["id"])

    def autoResolve(self, conflict: dict) -> str:
        winner, loser, decision = self.decide(conflict)
        if decision < AUTO_RESOLVE_GATE:
            conflict["status"] = "queued"
            return "queued"
        self.resolve(conflict, "keep_a" if winner == conflict["a"] else "keep_b", actor="auto")
        conflict["status"] = "resolved"
        return "resolved"

    auto_resolve = autoResolve


class DreamingService:
    """Constructive synthesis (section 7). Proposals never materialize before approval."""

    def __init__(self, hmem: "HMEM", synthesizer: Callable[[str, list[dict]], str] | None = None):
        self.h = hmem
        self.synthesizer = synthesizer
        self.proposals: dict[str, dict] = {}

    def _related(self, a: dict, b: dict) -> float:
        sim = cosine(a["embedding"], b["embedding"])
        tag = len(set(a["tags"]) & set(b["tags"])) / max(1, len(set(a["tags"]) | set(b["tags"])))
        same = 1.0 if (a["type"] == b["type"] or a["domain"] == b["domain"]) else 0.0
        ent = len({e["name"] for e in a["entities"]} & {e["name"] for e in b["entities"]})
        return 0.4 * sim + 0.2 * tag + 0.25 * same + 0.15 * min(1.0, ent / 2)

    def cluster(self) -> list[list[dict]]:
        mems = [m for m in self.h.store.all() if not m.get("supersededBy")]
        clusters, used = [], set()
        for m in mems:
            if m["id"] in used:
                continue
            group = [m] + [o for o in mems if o["id"] != m["id"] and o["id"] not in used and self._related(m, o) >= DREAM_RELATED]
            if len(group) >= 2:
                clusters.append(group)
                used.update(x["id"] for x in group)
        return clusters

    def _text(self, kind: str, group: list[dict]) -> str:
        if self.synthesizer:
            try:
                return self.synthesizer(kind, group)
            except Exception:
                pass
        return f"{kind.capitalize()} from {len(group)} related memories: " + "; ".join(m["content"] for m in group[:4])

    def _propose(self, kind: str, group: list[dict], **extra) -> dict:
        p = {"id": "dp-" + uuid.uuid4().hex[:8], "type": kind, "content": self._text(kind, group),
             "derivedFrom": [m["id"] for m in group], "status": "proposed", **extra}
        self.proposals[p["id"]] = p
        self.h.ledger.log(p["id"], "dream_proposed", details_type=kind, derivedFrom=p["derivedFrom"])
        return p

    def dream(self) -> list[dict]:
        out: list[dict] = []
        for group in self.cluster()[:MAX_DREAM_PER_TYPE]:
            out.append(self._propose("insight", group))
        general = [m for m in self.h.store.all() if m["domain"] == "general" and m["type"] == "general"]
        for m in general[:MAX_DREAM_PER_TYPE]:
            dom = infer_domain(m["content"])
            if dom != "general":
                out.append(self._propose("domain", [m], suggestedDomain=dom))
        prefs = [m for m in self.h.store.all() if m["type"] in ("preference", "personal") or m["source"] == "user_correction"]
        if len(prefs) >= 2:
            out.append(self._propose("invariant", prefs[:MAX_DREAM_PER_TYPE]))
        return out

    def approve(self, proposal_id: str) -> dict | None:
        p = self.proposals[proposal_id]
        p["status"] = "approved"
        self.h.ledger.log(proposal_id, "dream_approved", actor="human")
        if p["type"] == "domain":
            for mid in p["derivedFrom"]:
                m = self.h.store.get(mid)
                if m:
                    prev = {"domain": m["domain"]}
                    m["domain"] = p["suggestedDomain"]
                    self.h.ledger.log(mid, "update", "human", previous=prev, new={"domain": m["domain"]})
            return None
        mtype = "preference" if p["type"] == "invariant" else "factual"
        return self.h.add(content=p["content"], source="dreaming", layer="long_term", type=mtype,
                          derivedFrom=p["derivedFrom"], provenance_origin="dream")

    def reject(self, proposal_id: str) -> None:
        self.proposals[proposal_id]["status"] = "rejected"
        self.h.ledger.log(proposal_id, "dream_rejected", actor="human")


class SleepService:
    """Consolidation + compaction (section 8). Preview first; execute takes a backup."""

    def __init__(self, hmem: "HMEM"):
        self.h = hmem
        self.backups: dict[str, list[dict]] = {}

    def _protected(self, m: dict, all_mems: list[dict]) -> bool:
        if m.get("derivedFrom") or m.get("supersedes") or m["source"] in ("dreaming", "conflict_merge"):
            return True
        refs = sum(1 for o in all_mems if m["id"] in (o.get("relatedTo") or []) or m["id"] in (o.get("derivedFrom") or []))
        return refs >= 2  # relationship hub

    def preview(self) -> dict:
        now, mems = self.h.clock(), self.h.store.all()
        to_consolidate, to_archive = [], []
        for m in mems:
            idle = (now - m["lastAccessed"]) / DAY_MS
            age = (now - m["timestamp"]) / DAY_MS
            referenced = any(m["id"] in (o.get("relatedTo") or []) + (o.get("derivedFrom") or []) for o in mems)
            if age >= ARCHIVE_DAYS and m["accessCount"] <= 2 and not referenced and not self._protected(m, mems):
                to_archive.append(m["id"])
            elif m["layer"] == "short_term" and idle >= CONSOLIDATE_DAYS:
                to_consolidate.append(m["id"])
        to_synth, used = [], set()
        for i, a in enumerate(mems):
            if a["id"] in used or a["id"] in to_archive:
                continue
            group = [a["id"]] + [b["id"] for b in mems[i + 1:] if b["id"] not in used
                                 and cosine(a["embedding"], b["embedding"]) >= SYNTH_SIM]
            if len(group) >= 2:
                to_synth.append(group)
                used.update(group)
        return {"toConsolidate": to_consolidate, "toArchive": to_archive, "toSynthesize": to_synth}

    def execute(self, preview: dict) -> dict:
        backup_id = "bk-" + uuid.uuid4().hex[:8]
        self.backups[backup_id] = copy.deepcopy(self.h.store.all())
        log = []
        for mid in preview["toConsolidate"]:
            m = self.h.store.get(mid)
            if m:
                m["layer"] = "long_term"
                self.h.ledger.log(mid, "sleep_consolidate", new={"layer": "long_term"})
                log.append(("consolidate", mid))
        for mid in preview["toArchive"]:
            m = self.h.store.get(mid)
            if m and not self._protected(m, self.h.store.all()):
                self.h.store._mem.pop(mid)
                self.h.ledger.log(mid, "sleep_archive", previous={"content": m["content"]})
                log.append(("archive", mid))
        for group in preview["toSynthesize"]:
            ms = [self.h.store.get(x) for x in group if self.h.store.get(x)]
            if len(ms) < 2:
                continue
            merged = self.h.add(content=max(ms, key=lambda m: len(m["content"]))["content"], source="sleep_synthesis",
                                layer="long_term", tags=sorted({t for m in ms for t in m["tags"]}),
                                derivedFrom=[m["id"] for m in ms], confidence=max(m["confidence"] for m in ms))
            for m in ms:
                self.h.store._mem.pop(m["id"], None)
            self.h.ledger.log(merged["id"], "sleep_synthesize", derivedFrom=merged["derivedFrom"])
            log.append(("synthesize", merged["id"]))
        self.h.store.save()
        return {"backupId": backup_id, "actions": log, "counts": dict(Counter(a for a, _ in log))}

    def restore(self, backup_id: str) -> None:
        self.h.store._mem = {m["id"]: copy.deepcopy(m) for m in self.backups[backup_id]}
        self.h.ledger.log(backup_id, "restore")
        self.h.store.save()


class HMEM:
    """Facade matching the conformance surface: add / query / feedback + store, ledger, sleep, dreaming, conflicts."""

    def __init__(self, clock: Callable[[], int] | None = None, path: str | Path | None = None,
                 synthesizer: Callable[[str, list[dict]], str] | None = None):
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.ledger = TrustLedger(self.clock)
        self.store = MemoryStore(self.clock, self.ledger, path)
        self.conflicts = ConflictService(self)
        self.dreaming = DreamingService(self, synthesizer)
        self.sleep = SleepService(self)

    # -- write path ---------------------------------------------------------------------------------
    def add(self, content: str, source: str, confidence: float = 0.7, layer: str = "working",
            type: str = "general", tags: list[str] | None = None, derivedFrom: list[str] | None = None,
            supersedes: str | None = None, provenance_origin: str | None = None, label: str | None = None,
            chatId: str | None = None, missionId: str | None = None, goalRunId: str | None = None,
            actor: str = "system") -> dict:
        if not source or not str(source).strip():
            raise ValueError("H-MEM: a memory record must have a source")
        if not content or len(content.strip()) < 3:
            raise ValueError("H-MEM: content too short")
        layer = layer if layer in LAYERS else "working"
        type = type if type in TYPES else "general"
        tags = list(tags or [])
        now = self.clock()
        domain = infer_domain(content, tags)
        m = {
            "id": "mem-" + uuid.uuid4().hex[:12], "content": content.strip(), "type": type, "layer": layer,
            "timestamp": now, "lastAccessed": now, "accessCount": 0, "tags": tags,
            "confidence": clamp01(confidence), "source": source,
            "provenance": {"origin": provenance_origin or source, "learnedAt": now, "label": label or f"learned from {source}"},
            "chatId": chatId, "missionId": missionId, "goalRunId": goalRunId,
            "domain": domain, "namespace": f"{domain}/{type}",
            "entities": extract_entities(content), "relatedTo": [], "supersedes": supersedes,
            "supersededBy": None, "derivedFrom": list(derivedFrom or []),
        }
        m["embedding"] = embed(m["content"])
        self._link(m)
        if supersedes and self.store.get(supersedes):
            self.store.get(supersedes)["supersededBy"] = m["id"]
        self.store.put(m)
        self.ledger.log(m["id"], "create", actor, new={"content": m["content"], "confidence": m["confidence"], "layer": layer})
        return m

    def _link(self, m: dict) -> None:
        names = {e["name"] for e in m["entities"]}
        for o in self.store.all():
            if len(m["relatedTo"]) >= RELATED_CAP:
                break
            shared = names & {e["name"] for e in o["entities"]}
            if shared and (o["domain"] == m["domain"] or len(shared) >= 2):
                m["relatedTo"].append(o["id"])
                if len(o["relatedTo"]) < RELATED_CAP:
                    o["relatedTo"].append(m["id"])

    def promote(self, memory_id: str, layer: str = "long_term", actor: str = "system") -> None:
        m = self.store.get(memory_id)
        prev = {"layer": m["layer"]}
        m["layer"] = layer
        self.ledger.log(memory_id, "promote", actor, previous=prev, new={"layer": layer})

    def feedback(self, memory_id: str, kind: str, actor: str = "user") -> dict:
        m = self.store.get(memory_id)
        if m is None or kind not in FEEDBACK:
            raise ValueError(f"bad feedback {kind!r} for {memory_id}")
        prev = {"confidence": m["confidence"], "accessCount": m["accessCount"], "tags": list(m["tags"])}
        m["confidence"] = round(clamp01(m["confidence"] + FEEDBACK[kind]), 10)
        if kind == "helpful":
            m["accessCount"] += 1
        if kind == "wrong" and "review" not in m["tags"]:
            m["tags"].append("review")
        self.ledger.log(memory_id, "feedback", actor, previous=prev,
                        new={"confidence": m["confidence"], "accessCount": m["accessCount"], "tags": list(m["tags"])}, kind=kind)
        return m

    # -- read path (reinjection) -------------------------------------------------------------------
    def score(self, m: dict, q_emb: dict, q_kw: set[str], q_domain: str, q_ents: set[str]) -> tuple[float, dict]:
        now = self.clock()
        mem_toks = set(tokens(m["content"]))
        semantic = clamp01(cosine(q_emb, m["embedding"]))
        keyword = len(q_kw & mem_toks) / len(q_kw | mem_toks) if q_kw else 0.0
        tag = len(q_kw & {t.split(":", 1)[-1] for t in m["tags"]}) / max(1, len(q_kw)) if m["tags"] else 0.0
        domain = 1.0 if q_domain != "general" and q_domain == m["domain"] else 0.0
        ents = {e["name"] for e in m["entities"]}
        entity = len(q_ents & ents) / max(1, len(q_ents)) if q_ents else 0.0
        layer = LAYER_WEIGHT[m["layer"]]
        recency = math.exp(-max(0, now - m["lastAccessed"]) / (RECENCY_TAU_DAYS * DAY_MS))
        superseded = 1.0 if m.get("supersededBy") else 0.0
        raw = (0.35 * semantic + 0.20 * keyword + 0.10 * tag + 0.10 * domain + 0.15 * entity
               + 0.05 * layer + 0.05 * recency - 0.20 * superseded)
        parts = dict(semantic=semantic, keyword=keyword, tag=tag, domain=domain, entity=entity,
                     layer=layer, recency=recency, superseded=superseded)
        return clamp01(raw) * m["confidence"], parts

    def query(self, text: str, threshold: float = THRESHOLD, topN: int = TOP_N, missionId: str | None = None,
              actor: str = "system") -> dict:
        q_emb = embed(text)
        counts = Counter(tokens(text))
        q_kw = {t for t, _ in counts.most_common(8)}
        q_domain = infer_domain(text)
        q_ents = {e["name"] for e in extract_entities(text)}
        scored = []
        for m in self.store.all():
            if missionId and m.get("missionId") not in (None, missionId):
                continue
            s, parts = self.score(m, q_emb, q_kw, q_domain, q_ents)
            if s >= threshold:
                scored.append({"memory": m, "score": round(s, 4), "parts": {k: round(v, 3) for k, v in parts.items()}})
        scored.sort(key=lambda r: r["score"], reverse=True)
        picked = scored[:topN]
        now = self.clock()
        for r in picked:
            r["memory"]["lastAccessed"] = now
            r["memory"]["accessCount"] += 1
            self.ledger.log(r["memory"]["id"], "read", actor, score=r["score"])
        strategy = "minimal" if len(picked) <= 1 else ("comprehensive" if len(picked) >= 4 else "focused")
        return {"memories": picked, "context": self.format_context(picked), "strategy": strategy,
                "retrievalReason": f"query domain={q_domain}, keywords={sorted(q_kw)}, entities={sorted(q_ents)}",
                "threshold": threshold}

    @staticmethod
    def format_context(picked: list[dict]) -> str:
        """Domain-grouped reinjection block (section 6.2)."""
        if not picked:
            return ""
        groups: dict[str, list[dict]] = {}
        for r in picked:
            groups.setdefault(r["memory"]["domain"], []).append(r)
        lines = ["## Recalled memory (H-MEM)"]
        for dom, items in groups.items():
            lines.append(f"### {dom}")
            for r in items:
                m = r["memory"]
                ents = ", ".join(e["name"] for e in m["entities"][:4])
                lines.append(f"- [{m['layer']}|conf {m['confidence']:.2f}|score {r['score']:.2f}] {m['content']}"
                             + (f"  (entities: {ents})" if ents else "") + f"  <{m['id']}>")
        return "\n".join(lines)

    # -- mining (section 4) -------------------------------------------------------------------------
    def mine(self, event_type: str, payload: str, extractor: Callable[[str, str], list[str]] | None = None,
             **kwargs) -> list[dict]:
        """Mine memories from an interaction event. Falls back to direct capture with no model."""
        items: list[str] = []
        if extractor:
            try:
                items = [s for s in extractor(event_type, payload) if isinstance(s, str) and len(s.strip()) >= 12]
            except Exception:
                items = []
        if not items:  # no-model fallback (4.3): keep a concise direct summary as short-term memory
            kwargs.setdefault("layer", "short_term")
            items = [payload.strip()[:300]]
        type_for = {"user_correction": "preference", "plan_completion": "factual", "model_response": "factual"}
        kwargs.setdefault("type", type_for.get(event_type, "general"))
        return [self.add(content=s, source=event_type, **kwargs) for s in items]

    def save(self) -> None:
        self.store.save()
