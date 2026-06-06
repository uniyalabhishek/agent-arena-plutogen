"""Few-shot demo retrieval — the lever for a weak model.

Given a task instruction, find the most similar TRAIN tasks we already have
correct solutions for (demo_bank.json, built from train ground truth) and render
them as worked examples. A weak model pattern-matches a correct example far better
than it reasons multi-source orchestration / exact field names from scratch.
(Measured: +12.5 TGC points on Llama-3.3-70B over a dev-24 A/B.)

Two interchangeable backends behind the SAME interface (`.render(instruction) -> str`):
  - DemoRetriever  : dependency-free TF-IDF cosine over instruction text. No sklearn
                     → no pydantic-v2 risk to the appworld engine. The always-works floor.
  - HydraRetriever : queries HydraDB (hybrid semantic + BM25 + rerank) for the match,
                     then looks the FULL solution up locally by id. Better ranking +
                     earns the hackathon bonus. Falls back to TF-IDF on any error.

Legal under AppWorld rules: demos come from TRAIN (never test), retrieved by
similarity — not hardcoded per task.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "my", "me",
    "is", "are", "with", "that", "this", "from", "by", "at", "as", "it", "i",
    "all", "any", "each", "get", "give", "list", "show", "what", "which", "who",
}


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


def render_demos(hits: list[tuple[float, dict]], max_solution_chars: int = 2600) -> str:
    """Format retrieved (score, demo) pairs into a few-shot block. Shared by both backends."""
    if not hits:
        return ""
    parts = [
        "\n\n# Worked examples from past solved tasks\n"
        "The following are SIMILAR tasks that were already solved correctly. "
        "Study them to see which APIs to call, the EXACT field names the responses "
        "use, how to paginate, how to de-duplicate, and how to format the final "
        "answer. Adapt the APPROACH to the current task — do NOT copy the specific "
        "values or assume the same items exist.\n"
    ]
    for n, (score, d) in enumerate(hits, 1):
        sol = d["solution"].strip()
        if len(sol) > max_solution_chars:
            sol = sol[:max_solution_chars] + "\n# ...(truncated)"
        parts.append(
            f"\n## Example {n} (similarity {score:.2f})\n"
            f"Task: {d['instruction']}\n"
            f"Correct solution:\n```python\n{sol}\n```\n"
        )
    return "".join(parts)


def _load_demos(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


class DemoRetriever:
    """TF-IDF cosine over instruction text. Dependency-free; the always-works floor."""

    def __init__(self, path: str, k: int = 2, min_score: float = 0.05,
                 max_solution_chars: int = 2600) -> None:
        self.k = k
        self.min_score = min_score
        self.max_solution_chars = max_solution_chars
        self.demos = _load_demos(path)

        n = len(self.demos)
        df: Counter[str] = Counter()
        self._doc_tf: list[Counter[str]] = []
        for d in self.demos:
            tf = Counter(_tokens(d["instruction"]))
            self._doc_tf.append(tf)
            for tok in tf:
                df[tok] += 1
        self._idf = {tok: math.log((n + 1) / (c + 1)) + 1.0 for tok, c in df.items()}
        self._doc_norm = [self._norm(tf) for tf in self._doc_tf]

    def _vec(self, tf: Counter[str]) -> dict[str, float]:
        return {tok: f * self._idf.get(tok, 0.0) for tok, f in tf.items()}

    def _norm(self, tf: Counter[str]) -> float:
        return math.sqrt(sum(w * w for w in self._vec(tf).values())) or 1.0

    def retrieve(self, instruction: str) -> list[tuple[float, dict]]:
        q_vec = self._vec(Counter(_tokens(instruction)))
        q_norm = math.sqrt(sum(w * w for w in q_vec.values())) or 1.0
        scored = []
        for i, d in enumerate(self.demos):
            d_vec = self._vec(self._doc_tf[i])
            dot = sum(w * d_vec.get(tok, 0.0) for tok, w in q_vec.items())
            scored.append((dot / (q_norm * self._doc_norm[i]), d))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [(s, d) for s, d in scored[: self.k] if s >= self.min_score]

    def render(self, instruction: str) -> str:
        return render_demos(self.retrieve(instruction), self.max_solution_chars)


class HydraRetriever:
    """HydraDB-backed retrieval. Queries Hydra for the match, looks the full solution
    up locally by id, and falls back to TF-IDF on any error (network/rate-limit)."""

    def __init__(self, client, tenant_id: str, path: str, k: int = 2,
                 min_score: float = 0.5, max_solution_chars: int = 2600,
                 fallback: "DemoRetriever | None" = None) -> None:
        self.client = client
        self.tenant_id = tenant_id
        self.k = k
        self.min_score = min_score
        self.max_solution_chars = max_solution_chars
        self.demos = _load_demos(path)
        self._by_id = {d["task_id"]: d for d in self.demos}
        # TF-IDF over the same bank, so a Hydra outage degrades gracefully (not to zero).
        self.fallback = fallback or DemoRetriever(path, k=k, max_solution_chars=max_solution_chars)
        self.hydra_calls = 0
        self.fallbacks = 0

    @staticmethod
    def _family(task_id: str) -> str:
        # "82e2fac_1" / "82e2fac_2" are near-identical variants -> same family.
        return task_id.rsplit("_", 1)[0]

    def retrieve(self, instruction: str) -> list[tuple[float, dict]]:
        # Ask for a larger candidate pool so reranking has room, then keep the
        # top-k UNIQUE families above the relevance threshold.
        try:
            chunks = self.client.query(self.tenant_id, instruction, k=max(6, 3 * self.k))
            self.hydra_calls += 1
        except Exception:
            self.fallbacks += 1  # Hydra unreachable -> TF-IDF floor (never zero)
            return self.fallback.retrieve(instruction)

        hits, seen_fam = [], set()
        for ch in chunks:
            d = self._by_id.get(ch.get("id"))
            if d is None:
                continue
            score = float(ch.get("relevancy_score", 0.0))
            if score < self.min_score:
                continue  # below threshold: better to inject nothing than mislead
            fam = self._family(ch["id"])
            if fam in seen_fam:
                continue
            seen_fam.add(fam)
            hits.append((score, d))
            if len(hits) >= self.k:
                break
        # On a successful query we trust the result, even if empty (uncovered app).
        return hits

    def render(self, instruction: str) -> str:
        return render_demos(self.retrieve(instruction), self.max_solution_chars)


# tenant used for the demo index
HYDRA_TENANT = os.environ.get("HYDRA_TENANT", "agent_arena_demos")


def build_default(k: int = 2):
    """Pick a retriever from env. RETRIEVER=hydra uses HydraDB (+ TF-IDF fallback);
    anything else (default) uses TF-IDF. Returns None if the demo bank is absent."""
    path = os.path.join(os.path.dirname(__file__), "prompts", "demo_bank.json")
    if not os.path.exists(path):
        return None
    if os.environ.get("RETRIEVER", "hydra").lower() == "hydra" and os.environ.get("HYDRA_DB_API_KEY"):
        try:
            from harness.hydra import HydraClient
            min_score = float(os.environ.get("HYDRA_MIN_SCORE", "0.5"))
            return HydraRetriever(HydraClient(), HYDRA_TENANT, path, k=k, min_score=min_score)
        except Exception:
            pass  # any setup failure -> fall through to TF-IDF
    return DemoRetriever(path, k=k)
