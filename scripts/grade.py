"""Grade an experiment and summarize TGC/SGC + failure modes.

Usage:
  .venv/bin/python scripts/grade.py <experiment> [split] [task_id ...]

Reports aggregate Task/Scenario Goal Completion AND, per task, which unit-test
requirements failed and their labels — so we see *why* tasks fail. Failure modes
are tallied two ways: by requirement text, and by label (the latter separates
answer/state-correctness failures from `no_op` collateral-damage failures).
"""
from __future__ import annotations

import os
import sys
from collections import Counter

_HERE = os.path.dirname(__file__)
_ROOT = os.path.join(_HERE, "..")
from dotenv import load_dotenv  # noqa: E402

load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))

from appworld import evaluate_tasks  # noqa: E402


def main() -> None:
    exp = sys.argv[1]
    split = sys.argv[2] if len(sys.argv) > 2 else "dev"
    out_tasks = os.path.join(_ROOT, "experiments", "outputs", exp, "tasks")
    task_ids = sys.argv[3:] or sorted(
        d for d in os.listdir(out_tasks) if os.path.isdir(os.path.join(out_tasks, d))
    )

    print(f"Grading '{exp}' ({split}): {len(task_ids)} tasks ...", flush=True)
    result = evaluate_tasks(task_ids, experiment_name=exp)
    agg = result.get("aggregate", {})
    ind = result.get("individual", {})

    rows = []
    req_modes: Counter[str] = Counter()
    label_modes: Counter[str] = Counter()
    by_difficulty: dict[str, list[int]] = {}
    for tid in task_ids:
        rec = ind.get(tid, {})
        success = bool(rec.get("success", False))
        diff = str(rec.get("difficulty", "?"))
        n_tests = rec.get("num_tests", "?")
        failures = rec.get("failures", []) or []
        for f in failures:
            req_modes[str(f.get("requirement", "?"))[:70]] += 1
            label_modes[str(f.get("label", "?"))] += 1
        by_difficulty.setdefault(diff, []).append(int(success))
        rows.append((tid, success, diff, n_tests, failures))

    print("\n--- per task ---")
    for tid, success, diff, n_tests, failures in rows:
        mark = "✓" if success else "✗"
        if success:
            print(f"  {mark} {tid} [d={diff}]")
        else:
            fl = [f"{f.get('label','?')}:{str(f.get('requirement','?'))[:45]}" for f in failures]
            print(f"  {mark} {tid} [d={diff}] {len(failures)}/{n_tests} failed -> {fl}")

    n = len(task_ids) or 1
    derived = sum(r[1] for r in rows)
    print("\n--- aggregate ---")
    print(f"  engine TGC={agg.get('task_goal_completion')}  SGC={agg.get('scenario_goal_completion')}")
    print(f"  derived TGC: {derived}/{len(task_ids)} = {derived / n:.3f}")
    print("  by difficulty: " + "  ".join(
        f"d{d}={sum(v)}/{len(v)}" for d, v in sorted(by_difficulty.items())
    ))
    if label_modes:
        print("\n--- failure labels (collateral-damage vs correctness) ---")
        for label, c in label_modes.most_common():
            print(f"  {c:>3}x  {label}")
    if req_modes:
        print("\n--- failure modes (by requirement) ---")
        for title, c in req_modes.most_common(15):
            print(f"  {c:>3}x  {title}")


if __name__ == "__main__":
    main()
