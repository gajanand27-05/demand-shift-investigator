"""Power layer: Meterless Nightshift (OpenAI-compatible) as the primary AI provider, pinned to a Nemotron lane.

Every call is recorded to a replay cache so that SAMPLE_MODE=true reruns a recorded investigation with no
model or network access (the judges' fallback path). Keys come only from the environment.
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "data" / "sample" / "llm_cache.json"
TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "120"))
MAX_RETRIES = 2


def sample_mode() -> bool:
    return os.environ.get("SAMPLE_MODE", "false").lower() in ("1", "true", "yes")


class Cache:
    """Content-addressed replay cache shared by the LLM and SQL layers."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    @staticmethod
    def key(*parts) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:24]

    def get(self, k):
        return self.data.get(k)

    def put(self, k, v):
        self.data[k] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")


_cache = Cache(CACHE_PATH)
_client = None
# Sequence position within the current run: SAMPLE_MODE replays a recorded run call-by-call, so a replay does
# not depend on prompt text that varies with the memory state.
RUN = {"id": None, "n": 0}


def seq_key(kind: str) -> str:
    RUN["n"] += 1
    return Cache.key("seq", RUN["id"], RUN["n"], kind)


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(
            base_url=os.environ["MODEL_BASE_URL"],
            api_key=os.environ["MODEL_API_KEY"],
            timeout=TIMEOUT_S,
            max_retries=0,
            default_headers={
                "meterless-app": os.environ.get("METERLESS_APP", "open-agents-hackathon"),
                "meterless-scope": "user",
            },
        )
    return _client


def model_for(role: str) -> str:
    """Per-role lane override (e.g. NEMOTRON_MODEL_CRITIC), else the pinned Nemotron lane."""
    return os.environ.get(f"NEMOTRON_MODEL_{role.upper()}") or os.environ["NEMOTRON_MODEL"] if not sample_mode() \
        else os.environ.get("NEMOTRON_MODEL", "sample")


def parse_json(text: str):
    """Tolerant JSON parse: strips <think> blocks and markdown fences, takes the outermost object."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model reply: " + text[:200])
    return json.loads(text[start:end + 1])


def chat(role: str, system: str, user: str, temperature: float = 0.2) -> dict:
    """One model call -> {"text", "json", "model", "latency_s", "cached"}. JSON is requested and parsed."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    k = Cache.key("chat", role, messages)
    sk = seq_key("chat:" + role)
    if sample_mode():
        hit = _cache.get(sk) or _cache.get(k)
        if hit is None:
            raise RuntimeError(f"SAMPLE_MODE: no recorded reply for this {role} prompt (run once live to record)")
        return {**hit, "cached": True}
    primary = model_for(role)
    fallback = os.environ.get("NEMOTRON_FALLBACK_MODEL")
    # Nemotron reasoning can burn the whole token budget "thinking"; answers here are short JSON, so thinking is off
    # unless LLM_THINKING=true (Nightshift passes chat_template_kwargs through to the lane).
    extra = {} if os.environ.get("LLM_THINKING", "false").lower() == "true" else         {"chat_template_kwargs": {"enable_thinking": False}}
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        model = fallback if (fallback and attempt == MAX_RETRIES) else primary   # last try: fallback lane
        t0 = time.time()
        try:
            resp = client().chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                    max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "2000")),
                                                    extra_body=extra)
            text = resp.choices[0].message.content or ""
            out = {"text": text, "json": parse_json(text), "model": getattr(resp, "model", model),
                   "latency_s": round(time.time() - t0, 2), "attempt": attempt}
            _cache.put(k, out)
            if RUN["id"]:
                _cache.put(sk, out)
            return {**out, "cached": False}
        except Exception as e:  # timeout, HTTP error, or unparseable JSON -> retry with a nudge
            last_err = e
            messages = messages[:2] + [{"role": "user", "content":
                                        f"Your previous reply failed ({type(e).__name__}: {str(e)[:120]}). "
                                        "Reply with ONLY one valid JSON object."}] if attempt == 0 else messages
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{role}: model call failed after {MAX_RETRIES + 1} attempts: {last_err}")
