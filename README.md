# ://agent_arena — team `plutogen`

An autonomous AppWorld agent: an owned ReAct code-loop with **HydraDB-powered
few-shot retrieval**, behind a model-agnostic boundary.

## Submission facts
- **Team:** `plutogen` (outputs in `experiments/outputs/team_plutogen/`)
- **Score** (official `agent_arena_eval`, 10 tasks): **TGC 20.0 / SGC 20.0** — 2/10 (easy 2/3, medium 0/3, hard 0/4)
- **Model:** `openrouter/meta-llama/llama-3.3-70b-instruct` — the mandated
  **Llama 3.3 70B** weights, served via OpenRouter (Groq rate-limited us; identical model).
- **HydraDB used:** **yes** — semantic few-shot retrieval of similar *train*-solved
  tasks, injected as worked examples (see below). Our single biggest TGC lever.
- **Integrity:** general agent; no `task_id`-keyed answers. Demos are retrieved by
  instruction similarity from TRAIN tasks only.

## Architecture
- `harness/model.py` — the only LLM boundary. Backend is inferred from the `MODEL`
  prefix (`openrouter` | `groq` | `anthropic`); OpenAI-compatible providers are called
  over raw `httpx` (no litellm → keeps AppWorld's pydantic v1 intact).
- `harness/agent.py` — ReAct loop: one ```python``` block per turn, stdout fed back as
  the next observation, state persists in the REPL; stops at `complete_task`.
- `harness/retrieval.py` + `harness/hydra.py` — **HydraDB few-shot retrieval.** The 90
  train-solution instructions are indexed in HydraDB; per task we query (hybrid
  semantic + BM25, rerank) for the most similar solved tasks and inject their *correct*
  `apis.*` solutions as examples. The full solution is looked up locally by the returned
  id (so code is never chunk-split). Falls back to a local TF-IDF index if HydraDB is
  unreachable, so a run never hard-fails.
- `harness/run.py` — orchestration + two prompt scaffolds that help the weaker model:
  a multi-source **decomposition** hint and an **inspect-before-you-process** hint
  (which cut schema-guessing `KeyError`s).

## Why HydraDB demos — dev A/B (Llama 3.3 70B, 25 tasks)
Demo retrieval is our core lever, validated on **dev** before the official run:
| Config | TGC |
|---|---|
| baseline (decomp+inspect) | 20% |
| + TF-IDF demos | 32% |
| **+ HydraDB demos** | **44%** |

On the official **`agent_arena_eval`** (10 `test_challenge` tasks — harder than dev):
**TGC 20.0 / SGC 20.0**. Demo retrieval lifts reasoning/field-name tasks; the
remaining gap is Llama-3.3-70B's ceiling on long multi-step *action* chains.

## Reproduce
```bash
pip install -r requirements.txt                 # Python 3.11
# The ONLY key you supply is the model provider's:
export OPENROUTER_API_KEY=...                    # mandated Llama 3.3 70B via OpenRouter
# HydraDB needs NO setup — a read key + our pre-ingested demo tenant
# (`agent_arena_demos`) are bundled in harness/hydra.py, so the HydraDB retrieval
# path runs out-of-the-box. (If HydraDB is unreachable it degrades to a local
# TF-IDF index, so a run never hard-fails.)
cp eval/agent_arena_eval.txt data/datasets/agent_arena_eval.txt
export APPWORLD_EXPERIMENT=team_plutogen APPWORLD_DATASET=agent_arena_eval MAX_TASKS=0
python agent.py
appworld evaluate team_plutogen agent_arena_eval

# Optional (NOT required — tenant is already indexed): rebuild the HydraDB index
#   python scripts/ingest_demos.py
```
