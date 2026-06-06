"""Parallel multi-process runner for a full-split submission.

Launches NUM_PROCESSES copies of agent.py, each handling a disjoint stride of
task_ids (PROCESS_INDEX::NUM_PROCESSES), all writing to the SAME experiment dir
(unique task subdirs — AppWorld's documented safe-parallelism pattern: separate
processes, one world per process). Then evaluates the whole experiment so the
submission folder contains evaluations/<split>.json.

168 test_normal tasks serial ≈ 2.3h; at N=6 ≈ ~25-45 min depending on model.
Server-side prompt caching is shared across processes (identical system prompt),
so concurrency doesn't multiply the cached-token cost.

Usage:
  NUM_PROCESSES=6 MODEL=claude-sonnet-4-6 APPWORLD_DATASET=test_normal \
  APPWORLD_EXPERIMENT=team_x MAX_TASKS=0 .venv/bin/python scripts/run_parallel.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")


def main() -> None:
    n = int(os.environ.get("NUM_PROCESSES", "4"))
    exp = os.environ.get("APPWORLD_EXPERIMENT")
    if not exp:
        sys.exit("set APPWORLD_EXPERIMENT")
    ds = os.environ.get("APPWORLD_DATASET", "test_normal")
    evaluate = os.environ.get("EVALUATE", "1") == "1"

    logdir = os.path.join(_ROOT, "logs")
    os.makedirs(logdir, exist_ok=True)

    procs = []
    for i in range(n):
        env = dict(os.environ, NUM_PROCESSES=str(n), PROCESS_INDEX=str(i))
        logpath = os.path.join(logdir, f"{exp}_p{i}.log")
        logf = open(logpath, "w")
        p = subprocess.Popen(
            [sys.executable, "agent.py"], cwd=_ROOT, env=env,
            stdout=logf, stderr=subprocess.STDOUT,
        )
        procs.append((i, p, logf))
        print(f"launched shard {i + 1}/{n} pid={p.pid} -> logs/{exp}_p{i}.log", flush=True)

    t0 = time.time()
    rc_total = 0
    for i, p, logf in procs:
        p.wait()
        logf.close()
        rc_total |= p.returncode
        print(f"shard {i + 1}/{n} exited rc={p.returncode} (+{time.time() - t0:.0f}s)", flush=True)

    print(f"\nall shards done in {time.time() - t0:.0f}s (rc={rc_total}).", flush=True)
    max_tasks = int(os.environ.get("MAX_TASKS", "0"))
    if evaluate and max_tasks == 0:
        # Full split -> write evaluations/<split>.json (+ .txt) for submission.
        print(f"evaluating full split '{ds}' for '{exp}' ...", flush=True)
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))
        from appworld import evaluate_dataset
        agg = evaluate_dataset(exp, ds)
        print(f"AGGREGATE: {agg}", flush=True)
    elif evaluate:
        print(f"partial run (MAX_TASKS={max_tasks}); grade a subset with:\n"
              f"  .venv/bin/python scripts/grade.py {exp} {ds}", flush=True)
    print(f"Output folder: experiments/outputs/{exp}/  (zip this to submit)", flush=True)


if __name__ == "__main__":
    main()
