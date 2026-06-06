"""The model boundary — the only place that talks to an LLM provider.

Backends behind ONE interface (`complete(system, messages) -> str`):
  - openrouter : OpenAI-compatible REST via httpx — the scoring backend now
                 (openrouter/meta-llama/llama-3.3-70b-instruct = the mandated
                 Llama 3.3 70B, routed across providers to dodge rate limits).
  - groq       : OpenAI-compatible REST via httpx (groq/llama-3.3-70b-versatile).
No litellm anywhere — it pulls pydantic v2 and breaks the appworld engine
(pydantic v1). Backend is inferred from the MODEL provider prefix, so the rest
of the harness never changes when we swap providers.
"""
from __future__ import annotations

import os
import time

import httpx

# provider prefix -> (chat-completions URL, api-key env var)
_OPENAI_COMPAT = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
}


def _infer_backend(model_id: str) -> str:
    head = model_id.split("/", 1)[0].lower()
    if head in _OPENAI_COMPAT:
        return head  # "groq" | "openrouter"
    return "openrouter"


class Model:
    """One LLM turn behind a stable interface, with retries + usage tracking."""

    def __init__(
        self,
        model_id: str | None = None,
        max_tokens: int | None = None,
        reasoning: str | None = None,
    ) -> None:
        self.model_id = model_id or os.environ.get(
            "MODEL", "openrouter/meta-llama/llama-3.3-70b-instruct"
        )
        self.backend = _infer_backend(self.model_id)
        self.max_tokens = max_tokens or int(os.environ.get("MAX_OUTPUT_TOKENS", "2048"))
        self.temperature = float(os.environ.get("TEMPERATURE", "0"))
        self.reasoning = reasoning if reasoning is not None else (os.environ.get("REASONING") or None)

        # running usage totals
        self.calls = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self.cache_write = 0
        self.cache_read = 0
        self.rate_limited = 0  # number of 429s we rode out
        self.last_ratelimit: dict[str, str] = {}

        if self.backend in _OPENAI_COMPAT:
            self._url, key_env = _OPENAI_COMPAT[self.backend]
            key = os.environ.get(key_env)
            if not key:
                where = "openrouter.ai/keys" if self.backend == "openrouter" else "console.groq.com"
                raise RuntimeError(f"{key_env} is not set (get one at {where})")
            self._provider_model = self.model_id.split("/", 1)[1] if "/" in self.model_id else self.model_id
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            if self.backend == "openrouter":  # optional attribution headers
                headers["HTTP-Referer"] = os.environ.get("OPENROUTER_REFERER", "https://localhost/agent_arena")
                headers["X-Title"] = os.environ.get("OPENROUTER_TITLE", "agent_arena")
            self._http = httpx.Client(timeout=httpx.Timeout(120.0), headers=headers)
        else:
            raise RuntimeError(f"Unsupported MODEL provider for scored run: {self.model_id}")

    # --- public interface ----------------------------------------------------

    def complete(self, system: str, messages: list[dict], cache_system: bool = True) -> str:
        if self.backend in _OPENAI_COMPAT:
            return self._complete_openai(system, messages)
        raise RuntimeError(f"Unsupported model backend: {self.backend}")

    # --- openai-compatible (openrouter / groq) -------------------------------

    def _complete_openai(self, system: str, messages: list[dict]) -> str:
        payload = {
            "model": self._provider_model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        data = self._post_with_retry(payload)
        choice = data["choices"][0]
        text = (choice.get("message", {}).get("content") or "")
        usage = data.get("usage", {}) or {}
        self.calls += 1
        self.in_tokens += usage.get("prompt_tokens", 0) or 0
        self.out_tokens += usage.get("completion_tokens", 0) or 0
        return text

    def _post_with_retry(self, payload: dict, tries: int = 8) -> dict:
        for attempt in range(tries):
            try:
                resp = self._http.post(self._url, json=payload)
            except httpx.TransportError:
                if attempt == tries - 1:
                    raise
                time.sleep(min(2 ** attempt, 30))
                continue
            # capture rate-limit telemetry from headers
            for h in ("retry-after", "x-ratelimit-remaining-requests",
                      "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"):
                if h in resp.headers:
                    self.last_ratelimit[h] = resp.headers[h]
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == tries - 1:
                    resp.raise_for_status()
                self.rate_limited += resp.status_code == 429
                # honor Retry-After when present, else exponential backoff
                wait = resp.headers.get("retry-after")
                delay = float(wait) if wait else min(2 ** attempt, 30)
                time.sleep(min(delay, 60))
                continue
            # 4xx other than 429 = a real bug; surface the body
            raise RuntimeError(f"{self.backend} {resp.status_code}: {resp.text[:300]}")
        raise RuntimeError(f"{self.backend}: exhausted retries")

    # --- diagnostics ---------------------------------------------------------

    def usage_summary(self) -> str:
        base = (f"calls={self.calls} in={self.in_tokens} out={self.out_tokens}")
        if self.backend in _OPENAI_COMPAT:
            rl = self.last_ratelimit
            extra = f" rate_limited={self.rate_limited}"
            if "x-ratelimit-remaining-tokens" in rl:
                extra += f" tok_left={rl['x-ratelimit-remaining-tokens']}"
            if "x-ratelimit-remaining-requests" in rl:
                extra += f" req_left={rl['x-ratelimit-remaining-requests']}"
            return base + extra
        return base
