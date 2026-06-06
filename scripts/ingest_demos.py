"""One-time: index the 90 train demos into HydraDB for semantic retrieval.

We index ONLY each demo's instruction text (that's what a test task gives us to
match on); the full solution stays local in demo_bank.json and is looked up by id
at query time (avoids HydraDB chunk-splitting the code). Idempotent via upsert.

Usage: .venv/bin/python scripts/ingest_demos.py
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _ROOT)  # so `import harness.*` works when run from anywhere
from dotenv import load_dotenv  # noqa: E402

load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))

from harness.hydra import HydraClient  # noqa: E402
from harness.retrieval import HYDRA_TENANT  # noqa: E402

_SCHEMA = [
    {"name": "apps", "data_type": "VARCHAR", "enable_match": True,
     "enable_sparse_embedding": True, "max_length": 256},
    {"name": "app_sig", "data_type": "VARCHAR", "enable_match": True,
     "enable_sparse_embedding": True, "max_length": 256},
    {"name": "artifact_type", "data_type": "VARCHAR", "enable_match": True,
     "enable_sparse_embedding": True, "max_length": 64},
]


def main() -> None:
    demos = json.load(open(os.path.join(_ROOT, "harness", "prompts", "demo_bank.json")))
    c = HydraClient()
    print(f"tenant '{HYDRA_TENANT}': create ->", c.create_tenant(HYDRA_TENANT, _SCHEMA).get("status"))
    print("waiting for tenant infra ...", "ready" if c.wait_tenant(HYDRA_TENANT, timeout=180) else "TIMEOUT")

    items = []
    for d in demos:
        apps = sorted(d.get("required_apps") or [])
        app_sig = "|".join(apps)
        metadata = {"apps": ",".join(apps), "app_sig": app_sig, "artifact_type": "demo"}
        items.append({
            "id": d["task_id"],
            "title": d["instruction"][:160],
            "type": "text",
            "kind": "custom",
            "provider": "appworld_demo",
            "external_id": d["task_id"],
            "content": {"text": f"{d['instruction']}\nApps: {', '.join(apps)}"},
            "fields": {
                "kind": "custom",
                "data": {
                    "instruction": d["instruction"],
                    "required_apps": apps,
                    "required_apis": d.get("required_apis") or [],
                },
            },
            "metadata": metadata,
            "tenant_metadata": metadata,
            "additional_metadata": {
                "task_family": d["task_id"].rsplit("_", 1)[0],
                "required_apps": apps,
                "required_apis": d.get("required_apis") or [],
            },
        })

    ids = [d["task_id"] for d in demos]
    BATCH = 30
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        res = c.ingest_knowledge(HYDRA_TENANT, chunk)
        got = res.get("results", res)
        print(f"  ingested {i + len(chunk)}/{len(items)} (batch status: "
              f"{got[0].get('status') if isinstance(got, list) and got else 'ok'})", flush=True)

    print("indexing (poll until searchable) ...", flush=True)
    status = c.wait_indexed(HYDRA_TENANT, ids, timeout=420)
    from collections import Counter
    counts = Counter(status.values())
    print("index status:", dict(counts), f"({len(status)}/{len(ids)} reported)")

    # smoke query
    q = demos[0]["instruction"]
    hits = c.query(HYDRA_TENANT, q, k=3)
    print(f"\nsmoke query: {q[:60]!r}")
    for ch in hits:
        print(f"   {ch.get('source_id') or ch.get('id')}  score={ch.get('relevancy_score')}")


if __name__ == "__main__":
    main()
