"""Build the SetBSR-lite demonstration bank from TRAIN ground truth.

For each train task we store (instruction, required_apps, compiled_solution_code_body,
answer, difficulty). `compiled_solution_code_body` is agent-usable code — it calls
real app APIs (apis.spotify.login(...), apis.supervisor.show_account_passwords(),
pagination loops) with no engine-internal helpers — so it can be injected verbatim
as a few-shot demonstration. Retrieval (by instruction similarity, later via HydraDB)
picks the top-k most similar train tasks to seed the agent before it solves a test task.

Legal under AppWorld rules: demos come from TRAIN (not test) and are retrieved by
similarity, not hardcoded per-task.

Usage: .venv/bin/python scripts/build_demo_bank.py
Output: harness/prompts/demo_bank.json
"""
from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
from dotenv import load_dotenv  # noqa: E402

load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))

from appworld import load_task_ids  # noqa: E402
from appworld.task import Task  # noqa: E402


def main() -> None:
    task_ids = load_task_ids("train")
    demos = []
    for i, tid in enumerate(task_ids, 1):
        try:
            t = Task.load(task_id=tid, load_ground_truth=True, ground_truth_mode="full")
            gt = t.ground_truth
            body = (gt.compiled_solution_code_body or "").strip()
            if not body:
                continue
            demos.append({
                "task_id": tid,
                "instruction": t.instruction,
                "required_apps": list(gt.required_apps or []),
                "required_apis": list(gt.required_apis or []),
                "difficulty": (gt.metadata or {}).get("difficulty"),
                "answer": str(gt.answer) if gt.answer is not None else None,
                "solution": body,
            })
        except Exception as e:
            print(f"  skip {tid}: {type(e).__name__}: {str(e)[:80]}", flush=True)
        if i % 15 == 0:
            print(f"  ...{i}/{len(task_ids)}", flush=True)

    out = os.path.join(_ROOT, "harness", "prompts", "demo_bank.json")
    with open(out, "w") as f:
        json.dump(demos, f, indent=1)
    sizes = [len(d["solution"]) for d in demos]
    print(f"wrote {out}: {len(demos)} demos, "
          f"solution chars min/med/max = {min(sizes)}/{sorted(sizes)[len(sizes)//2]}/{max(sizes)}")
    apps = {}
    for d in demos:
        for a in d["required_apps"]:
            apps[a] = apps.get(a, 0) + 1
    print("apps coverage:", dict(sorted(apps.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
