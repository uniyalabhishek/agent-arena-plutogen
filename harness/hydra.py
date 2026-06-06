"""Minimal HydraDB client over raw httpx (NO hydradb-sdk — it pulls pydantic v2
and would break AppWorld's pydantic-v1 engine, same trap as litellm).

Covers exactly what the demo index needs: create tenant -> poll ready -> ingest
knowledge -> poll indexed -> query. Endpoints/fields per docs.hydradb.com:
  base https://api.hydradb.com, headers Authorization: Bearer <key> + API-Version: 2,
  response envelope {success,data,error,meta} (we unwrap .data).
"""
from __future__ import annotations

import json
import os
import time

import httpx


class HydraClient:
    def __init__(self, token: str | None = None,
                 base_url: str = "https://api.hydradb.com", timeout: float = 60.0) -> None:
        token = token or os.environ.get("HYDRA_DB_API_KEY")
        if not token:
            raise RuntimeError("HYDRA_DB_API_KEY is not set (get one from HydraDB)")
        # Note: NO global Content-Type — JSON calls use json=, the multipart ingest
        # call lets httpx set its own boundary.
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "API-Version": "2"},
        )

    # --- low-level ----------------------------------------------------------
    def _unwrap(self, resp: httpx.Response) -> dict:
        resp.raise_for_status()
        body = resp.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def _post_json(self, path: str, payload: dict, tries: int = 5) -> dict:
        for attempt in range(tries):
            resp = self._http.post(path, json=payload)
            if resp.status_code in (429, 500, 503) and attempt < tries - 1:
                time.sleep(min(2 ** attempt, 20))
                continue
            return self._unwrap(resp)
        return self._unwrap(resp)

    def _get(self, path: str, params: dict) -> dict:
        return self._unwrap(self._http.get(path, params=params))

    # --- tenant -------------------------------------------------------------
    def create_tenant(self, tenant_id: str, schema: list[dict] | None = None) -> dict:
        payload = {"tenant_id": tenant_id, "tenant_metadata_schema": schema or []}
        try:
            return self._post_json("/tenants", payload)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:  # already exists -> fine
                return {"status": "exists", "tenant_id": tenant_id}
            raise

    def tenant_ready(self, tenant_id: str) -> bool:
        data = self._get("/tenants/status", {"tenant_id": tenant_id})
        return bool((data.get("infra") or {}).get("ready_for_ingestion"))

    def wait_tenant(self, tenant_id: str, timeout: float = 180.0, interval: float = 4.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.tenant_ready(tenant_id):
                    return True
            except httpx.HTTPStatusError:
                pass
            time.sleep(interval)
        return False

    # --- ingest -------------------------------------------------------------
    def ingest_knowledge(self, tenant_id: str, items: list[dict],
                         sub_tenant_id: str = "", upsert: bool = True) -> dict:
        """Ingest pre-extracted app_knowledge sources (no binary files) as multipart.

        Auto-fills the required per-item tenant_id/sub_tenant_id so callers only
        supply id/title/type/content/metadata."""
        items = [{"tenant_id": tenant_id, "sub_tenant_id": sub_tenant_id, **it} for it in items]
        files = {
            "type": (None, "knowledge"),
            "tenant_id": (None, tenant_id),
            "sub_tenant_id": (None, sub_tenant_id),
            "upsert": (None, "true" if upsert else "false"),
            "app_knowledge": (None, json.dumps(items)),
        }
        resp = self._http.post("/context/ingest", files=files)
        return self._unwrap(resp)

    def context_status(self, tenant_id: str, ids: list[str], sub_tenant_id: str = "") -> dict:
        # `ids` must be a REPEATED query param (ids=a&ids=b), not comma-joined —
        # httpx encodes a list value that way. Comma-joining => one bogus id.
        return self._get("/context/status", {
            "tenant_id": tenant_id, "sub_tenant_id": sub_tenant_id, "ids": ids,
        })

    def wait_indexed(self, tenant_id: str, ids: list[str], timeout: float = 300.0,
                     interval: float = 5.0, ready_states=("graph_creation", "completed")) -> dict:
        """Poll until all ids reach a searchable state (or time out). Returns last status map."""
        end = time.time() + timeout
        last: dict[str, str] = {}
        while time.time() < end:
            data = self.context_status(tenant_id, ids)
            last = {s["id"]: s.get("indexing_status", "?") for s in (data.get("statuses") or [])}
            if last and all(st in ready_states or st in ("errored", "failed")
                            for st in last.values()):
                return last
            time.sleep(interval)
        return last

    # --- query --------------------------------------------------------------
    def query(self, tenant_id: str, query: str, k: int = 2,
              mode: str = "thinking", query_by: str = "hybrid") -> list[dict]:
        """Return the ranked `chunks` list (each has id, relevancy_score, chunk_content)."""
        payload = {
            "tenant_id": tenant_id, "query": query, "type": "knowledge",
            "query_by": query_by, "mode": mode, "max_results": k,
            "graph_context": False,
        }
        data = self._post_json("/query", payload)
        return data.get("chunks", []) or []
