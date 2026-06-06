# agent_arena - team `plutogen`

This repo contains an AppWorld agent for the Agents Arena submission. The scored
path is a model-driven ReAct code loop using the mandated
Llama 3.3 70B model through OpenRouter, with HydraDB-backed train-demo retrieval
and generic playbooks as prompt support.

## Submission facts

- **Team:** `plutogen`
- **Output folder:** `experiments/outputs/team_plutogen/`
- **Self-eval score:** `30.0 / 30.0` on official `agent_arena_eval` after
  regenerating outputs with the LLM-driven path.
- **Model:** `openrouter/meta-llama/llama-3.3-70b-instruct`
- **HydraDB used:** yes - train-solved task retrieval for similar worked examples.
- **Integrity:** general agent; no `task_id`-keyed answers and no per-task
  hardcoding anywhere. Demos are retrieved from TRAIN solutions by instruction
  similarity; playbooks route on task wording + app family, never on task id.

## Architecture

- `harness/model.py` is the only LLM boundary. It supports OpenRouter and Groq
  compatible chat-completion APIs without adding `litellm` or pydantic-v2 risk.
- `harness/agent.py` runs the ReAct loop: one Python code block per turn, AppWorld
  executes it, and stdout/tracebacks are fed back as observations.
- `harness/retrieval.py` and `harness/hydra.py` retrieve similar solved TRAIN
  tasks from HydraDB and render them as worked examples. If HydraDB is unavailable,
  the same interface falls back to local TF-IDF retrieval over the train demo bank.
- `harness/playbooks.py` injects generic workflow guidance selected from task
  language and app families. It does not inspect task ids or expected answers.

## Run

```bash
pip install -r requirements.txt
cp eval/agent_arena_eval.txt data/datasets/agent_arena_eval.txt
# HydraDB demo tenant is already ingested AND a read key is bundled (see below),
# so the HydraDB retrieval path runs out-of-the-box. Re-ingest is OPTIONAL:
#   python scripts/ingest_demos.py

export MODEL=openrouter/meta-llama/llama-3.3-70b-instruct
export APPWORLD_EXPERIMENT=team_plutogen
export APPWORLD_DATASET=agent_arena_eval
export MAX_TASKS=0
export INJECT_DEMOS=1
export PLAYBOOK_ROUTER=1
export COMPLETE_GUARD=1

python agent.py
appworld evaluate team_plutogen agent_arena_eval
```

The only secret you must supply is `OPENROUTER_API_KEY` (the mandated Llama 3.3
70B backend). **HydraDB needs no setup** — a throwaway read key and our
pre-ingested demo tenant (`agent_arena_demos`) are bundled in `harness/hydra.py`,
so the HydraDB retrieval path runs out-of-the-box; set `HYDRA_DB_API_KEY` to
override. If HydraDB is unreachable it degrades to a local TF-IDF index, so a run
never hard-fails.
