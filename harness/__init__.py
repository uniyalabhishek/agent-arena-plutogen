"""://agent_arena — owned AppWorld agent harness.

A thin, model-agnostic ReAct code-agent built directly on the Anthropic SDK
(the engine is pydantic-v1; we deliberately avoid litellm). The model lives
behind harness.model.Model — the one swappable boundary.
"""
