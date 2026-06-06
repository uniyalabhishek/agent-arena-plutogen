"""Run the harness over an AppWorld split and write a valid output folder.

Env vars (same contract as the starter):
  ANTHROPIC_API_KEY    in ../.env
  MODEL                e.g. claude-sonnet-4-6 | claude-haiku-4-5-20251001 | claude-opus-4-8
  REASONING            ""|low|medium|high  (extended thinking)
  APPWORLD_DATASET     dev | test_normal | test_challenge
  APPWORLD_EXPERIMENT  unique team/run id -> experiments/outputs/<id>/
  MAX_TASKS            0 = all
  MAX_INTERACTIONS     per-task step cap (default 40)

Feature flags (default OFF — enable per-run once measured to help on Groq):
  INJECT_CATALOG       inject the full 457-API catalog into the system prompt
  SELF_VERIFY          require a verification code block before complete_task
  MODEL                openrouter/meta-llama/llama-3.3-70b-instruct (scored) |
                       groq/llama-3.3-70b-versatile | anthropic/claude-* (local dev)
  MAX_OUTPUT_TOKENS    per-reply cap (default 2048); TEMPERATURE (default 0)
"""
from __future__ import annotations

import os
import time

from dotenv import load_dotenv

_HERE = os.path.dirname(__file__)
load_dotenv(dotenv_path=os.path.join(_HERE, "..", ".env"))

from appworld import AppWorld, load_task_ids  # noqa: E402  (after dotenv)

from harness.agent import ReActAgent  # noqa: E402
from harness.model import Model  # noqa: E402
from harness.retrieval import build_default  # noqa: E402


# --- prompt fragments (kept here, in the orchestration layer; the agent stays
# content-agnostic and just appends whatever addendum it is handed) -----------

_CATALOG_INTRO = (
    "\n\n# Full API catalog\n"
    "Below is every available API as `app.api_name: description`. You therefore do NOT need to call "
    "`apis.api_docs.show_app_descriptions` or `apis.api_docs.show_api_descriptions` to discover APIs — "
    "go straight to `apis.api_docs.show_api_doc(app_name=..., api_name=...)` to confirm the parameters "
    "and response schema of the specific APIs you'll use, then call them.\n\n"
)

_DECOMP = (
    "\n\n**Multi-source decomposition (the #1 cause of wrong answers):**\n"
    "When a task says 'across', 'from all', 'in any of', or names several sources "
    "(e.g. 'across my song, album AND playlist libraries', 'on Venmo and Splitwise'), "
    "you MUST gather from EVERY named source separately, then combine — answering from "
    "only the first/most-obvious source is the most common mistake. Procedure:\n"
    "1. List every distinct source the task names.\n"
    "2. Retrieve matching items from EACH source (follow pagination to the very end).\n"
    "3. Merge and de-duplicate by a stable id.\n"
    "4. ONLY THEN filter / rank / aggregate / take top-N.\n"
    "For Spotify, a song can live in your song library, inside a saved album, OR inside "
    "a playlist — enumerate all three. For money/totals, sum across every account named.\n"
)

_INSPECT = (
    "\n\n**Inspect data before processing it (prevents KeyErrors and silent wrong answers):**\n"
    "API response field names cannot be guessed. Before you loop over or index into ANY "
    "API result, FIRST print one sample to see its real structure: `print(result[0])` for "
    "a list, `print(result)` for a dict. Then use the EXACT keys you observed.\n"
    "- If you get a KeyError, do NOT switch to `.get(key, default)` to silence it — that "
    "hides the bug and yields a wrong answer. Instead print the element, read the real key, "
    "and use it.\n"
    "- The same logical thing can have different key names from different APIs. When you "
    "merge items from multiple sources, print one item from EACH source and reconcile the "
    "keys before combining. De-duplicate by the actual id field.\n"
)

_SELF_VERIFY = (
    "\n\n**E. Verify before completing (do this on every task):**\n"
    "Before calling `apis.supervisor.complete_task`, run one final verification code block:\n"
    "- Re-derive the answer a second, independent way (or re-read the underlying data) and `assert` it "
    "matches what you are about to submit.\n"
    "- If the task changed state (send / create / update / delete / pay / add / remove), read the "
    "affected records back through the APIs and confirm the change is actually present and correct.\n"
    "- Recheck the answer against the request: minimal value, numbers as digits not words, correct "
    "order/units, no extra prose. For list answers, confirm both the count and the exact membership.\n"
    "Only once this verification passes, call `complete_task`.\n"
)


def build_preamble() -> str:
    path = os.path.join(_HERE, "prompts", "react_instructions.txt")
    with open(path) as f:
        text = f.read()
    # Everything before the final "now solve the real task" line is the cached preamble
    # (role intro + worked example + key instructions). We build the task message ourselves.
    marker = "Using these APIs, now generate code to solve the actual task:"
    return text.split(marker)[0].rstrip()


def build_addendum() -> tuple[str, list[str]]:
    """Assemble the static system addendum from enabled feature flags."""
    # Default OFF: the mandated Groq backend has NO prompt caching, so a 9k-token
    # catalog is sent uncached on every call (rate-limit + cost hit). Enable a
    # feature only once it's measured to help ON GROQ. (On cached Anthropic dev
    # they were ~free; that no longer holds.)
    addendum = ""
    features: list[str] = []
    if os.environ.get("DECOMP_HINT", "1") == "1":  # default ON: generic, measured-helpful
        addendum += _DECOMP
        features.append("decomp")
    if os.environ.get("INSPECT_HINT", "1") == "1":  # default ON: prevents schema-guess KeyErrors
        addendum += _INSPECT
        features.append("inspect")
    if os.environ.get("SELF_VERIFY", "0") == "1":
        addendum += _SELF_VERIFY
        features.append("self_verify")
    if os.environ.get("INJECT_CATALOG", "0") == "1":
        with open(os.path.join(_HERE, "prompts", "api_catalog.txt")) as f:
            catalog = f.read()
        addendum += _CATALOG_INTRO + catalog
        features.append("catalog")
    return addendum, features


def main() -> None:
    dataset = os.environ.get("APPWORLD_DATASET", "dev")
    experiment = os.environ.get("APPWORLD_EXPERIMENT", "team_demo")
    max_tasks = int(os.environ.get("MAX_TASKS", "0"))
    max_steps = int(os.environ.get("MAX_INTERACTIONS", "40"))
    # Mandated model = Llama 3.3 70B; routed via OpenRouter (Groq rate-limited us).
    model_id = os.environ.get("MODEL", "openrouter/meta-llama/llama-3.3-70b-instruct")
    # Multi-process sharding: each process handles a disjoint stride of tasks,
    # all writing to the same experiment dir (unique task subdirs). See
    # scripts/run_parallel.py. Defaults (1 / 0) = single-process, unchanged.
    num_proc = int(os.environ.get("NUM_PROCESSES", "1"))
    proc_idx = int(os.environ.get("PROCESS_INDEX", "0"))

    task_ids = load_task_ids(dataset)
    if max_tasks:
        task_ids = task_ids[:max_tasks]
    if num_proc > 1:
        task_ids = task_ids[proc_idx::num_proc]

    model = Model(model_id=model_id)
    addendum, features = build_addendum()
    retriever = None
    if os.environ.get("INJECT_DEMOS", "1") == "1":  # default ON: +24 TGC on Llama via HydraDB demos
        retriever = build_default(k=int(os.environ.get("DEMO_K", "2")))
        if retriever is not None:
            features.append(f"demos(k={retriever.k})")
    agent = ReActAgent(model, preamble=build_preamble(), max_steps=max_steps,
                       system_addendum=addendum, retriever=retriever)
    shard = f" [shard {proc_idx + 1}/{num_proc}]" if num_proc > 1 else ""
    print(f"Running '{experiment}'{shard} on {len(task_ids)} '{dataset}' tasks with {model_id} "
          f"(reasoning={model.reasoning}, max_steps={max_steps}, features={features or ['none']})", flush=True)

    t0 = time.time()
    completed = 0
    for i, task_id in enumerate(task_ids, 1):
        with AppWorld(task_id=task_id, experiment_name=experiment) as world:
            try:
                ok = agent.solve(world)
            except Exception as e:  # never let one task kill the run
                ok = False
                print(f"[{i}/{len(task_ids)}] {task_id}  ! {type(e).__name__}: {str(e)[:160]}", flush=True)
            else:
                completed += ok
                print(f"[{i}/{len(task_ids)}] {task_id}  {'✓' if ok else '✗ max_steps'}  "
                      f"({model.usage_summary()})", flush=True)

    dt = time.time() - t0
    print(f"\nDone in {dt:.0f}s. completed_loop={completed}/{len(task_ids)} "
          f"(note: loop-completion != graded TGC). Outputs in experiments/outputs/{experiment}/", flush=True)
    print(f"Evaluate with:  appworld evaluate {experiment} {dataset}", flush=True)


if __name__ == "__main__":
    main()
