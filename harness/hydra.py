"""Small HydraDB client over raw httpx.

We avoid hydradb-sdk because this AppWorld stack is pydantic-v1-sensitive. The
client covers only what the demo index needs and supports both HydraDB endpoint
families observed during the event:

- v2 LLM docs: /tenants, /context/ingest, /context/status, /query
- visible API reference: /tenants/create, /ingestion/upload_knowledge,
  /ingestion/verify_processing, /recall/full_recall
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable

import httpx


# --- Bundled HydraDB credential ------------------------------------------------
# Committed so evaluators run the HydraDB retrieval path with ZERO setup against
# our pre-ingested demo tenant. Throwaway hackathon key — rotate/revoke after
# judging. Override anytime via the HYDRA_DB_API_KEY env var.
_BUNDLED_HYDRA_KEY = 'sk_test_qvFAN7BbHuSH.QzBFd9nLhO7RkQKYrSaVy6-xDOhg5DAqxz8GuvwC7SI'


class HydraClient:
    def __init__(self, token: str | None = None,
                 base_url: str = "https://api.hydradb.com", timeout: float = 60.0) -> None:
        token = token or os.environ.get("HYDRA_DB_API_KEY") or _BUNDLED_HYDRA_KEY
        if not token:
            raise RuntimeError("HYDRA_DB_API_KEY is not set (get one from HydraDB)")
        headers = {"Authorization": f"Bearer {token}"}
        api_version = os.environ.get("HYDRA_API_VERSION", "2")
        if api_version:
            headers["API-Version"] = api_version
        self._http = httpx.Client(base_url=base_url, timeout=timeout, headers=headers)
        self.dialect = os.environ.get("HYDRA_API_DIALECT", "auto").lower()

    # --- low-level ----------------------------------------------------------
    def _unwrap(self, resp: httpx.Response) -> dict:
        resp.raise_for_status()
        body = resp.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def _post_json(self, path: str, payload: dict, tries: int = 5, params: dict | None = None) -> dict:
        for attempt in range(tries):
            resp = self._http.post(path, json=payload, params=params)
            if resp.status_code in (429, 500, 503) and attempt < tries - 1:
                time.sleep(min(2 ** attempt, 20))
                continue
            return self._unwrap(resp)
        return self._unwrap(resp)

    def _post_multipart(self, path: str, fields: dict, tries: int = 5) -> dict:
        for attempt in range(tries):
            resp = self._http.post(path, files=fields)
            if resp.status_code in (429, 500, 503) and attempt < tries - 1:
                time.sleep(min(2 ** attempt, 20))
                continue
            return self._unwrap(resp)
        return self._unwrap(resp)

    def _get(self, path: str, params: dict) -> dict:
        return self._unwrap(self._http.get(path, params=params))

    def _try_dialects(self, v2: Callable[[], dict], v1: Callable[[], dict]) -> dict:
        if self.dialect == "v2":
            return v2()
        if self.dialect == "v1":
            return v1()
        try:
            return v2()
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (404, 422):
                raise
            return v1()

    # --- tenant -------------------------------------------------------------
    def create_tenant(self, tenant_id: str, schema: list[dict] | None = None) -> dict:
        payload = {"tenant_id": tenant_id, "tenant_metadata_schema": schema or []}
        try:
            return self._try_dialects(
                lambda: self._post_json("/tenants", payload),
                lambda: self._post_json("/tenants/create", payload),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                return {"status": "exists", "tenant_id": tenant_id}
            raise

    def tenant_ready(self, tenant_id: str) -> bool:
        def v2() -> dict:
            data = self._get("/tenants/status", {"tenant_id": tenant_id})
            return {"ready": bool((data.get("infra") or {}).get("ready_for_ingestion"))}

        def v1() -> dict:
            data = self._get("/tenants/infra/status", {"tenant_id": tenant_id})
            infra = data.get("infra") or {}
            vectors = infra.get("vectorstore_status")
            vectors_ready = all(vectors) if isinstance(vectors, list) else True
            return {
                "ready": bool(
                    infra.get("scheduler_status")
                    and infra.get("graph_status")
                    and vectors_ready
                )
            }

        return bool(self._try_dialects(v2, v1).get("ready"))

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
                         sub_tenant_id: str = "default", upsert: bool = True) -> dict:
        """Ingest pre-extracted app_knowledge sources as multipart."""
        normalized = []
        for item in items:
            row = {"tenant_id": tenant_id, "sub_tenant_id": sub_tenant_id, **item}
            if "tenant_metadata" in row and "metadata" not in row:
                row["metadata"] = row["tenant_metadata"]
            if "metadata" in row and "tenant_metadata" not in row:
                row["tenant_metadata"] = row["metadata"]
            row.setdefault("kind", "custom")
            row.setdefault("provider", "appworld_demo")
            row.setdefault("external_id", row.get("id"))
            row.setdefault("fields", {
                "kind": "custom",
                "body": (row.get("content") or {}).get("text", ""),
            })
            normalized.append(row)

        def v2() -> dict:
            return self._post_multipart("/context/ingest", {
                "type": (None, "knowledge"),
                "tenant_id": (None, tenant_id),
                "sub_tenant_id": (None, sub_tenant_id),
                "upsert": (None, "true" if upsert else "false"),
                "app_knowledge": (None, json.dumps(normalized)),
            })

        def v1() -> dict:
            return self._post_multipart("/ingestion/upload_knowledge", {
                "tenant_id": (None, tenant_id),
                "sub_tenant_id": (None, sub_tenant_id),
                "upsert": (None, "true" if upsert else "false"),
                "app_knowledge": (None, json.dumps(normalized)),
            })

        return self._try_dialects(v2, v1)

    def context_status(self, tenant_id: str, ids: list[str], sub_tenant_id: str = "default") -> dict:
        def v2() -> dict:
            return self._get("/context/status", {
                "tenant_id": tenant_id,
                "sub_tenant_id": sub_tenant_id,
                "ids": ids,
            })

        def v1() -> dict:
            return self._post_json(
                "/ingestion/verify_processing",
                {},
                params={
                    "tenant_id": tenant_id,
                    "sub_tenant_id": sub_tenant_id,
                    "file_ids": ids,
                },
            )

        return self._try_dialects(v2, v1)

    def wait_indexed(self, tenant_id: str, ids: list[str], timeout: float = 300.0,
                     interval: float = 5.0, ready_states=("graph_creation", "completed", "success")) -> dict:
        """Poll until all ids reach a searchable state or timeout. Returns last status map."""
        end = time.time() + timeout
        last: dict[str, str] = {}
        while time.time() < end:
            data = self.context_status(tenant_id, ids)
            last = {
                s.get("id") or s.get("file_id"): s.get("indexing_status", "?")
                for s in (data.get("statuses") or [])
                if s.get("id") or s.get("file_id")
            }
            terminal = ready_states + ("errored", "failed")
            if len(last) >= len(ids) and all(st in terminal for st in last.values()):
                return last
            time.sleep(interval)
        return last

    # --- query --------------------------------------------------------------
    def query(self, tenant_id: str, query: str, k: int = 2, mode: str = "thinking",
              query_by: str = "hybrid", sub_tenant_id: str = "default",
              metadata_filters: dict | None = None) -> list[dict]:
        """Return ranked chunks. Supports both v2 /query and v1 /recall/full_recall."""
        def v2() -> dict:
            payload = {
                "tenant_id": tenant_id,
                "sub_tenant_id": sub_tenant_id,
                "query": query,
                "type": "knowledge",
                "query_by": query_by,
                "mode": mode,
                "max_results": k,
                "graph_context": False,
            }
            if metadata_filters:
                payload["metadata_filters"] = metadata_filters
            return {"chunks": self._post_json("/query", payload).get("chunks", []) or []}

        def v1() -> dict:
            payload = {
                "tenant_id": tenant_id,
                "sub_tenant_id": sub_tenant_id,
                "query": query,
                "max_results": k,
                "mode": mode,
                "alpha": "auto",
                "graph_context": False,
                "search_apps": True,
            }
            if metadata_filters:
                payload["metadata_filters"] = metadata_filters
            return {"chunks": self._post_json("/recall/full_recall", payload).get("chunks", []) or []}

        return self._try_dialects(v2, v1).get("chunks", [])
