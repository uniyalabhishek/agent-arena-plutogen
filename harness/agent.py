"""The ReAct code-agent loop — our owned core.

Each turn: the model writes ONE python block, the env executes it, and the
stdout/traceback comes back as the next observation. State persists in the
env's IPython shell across turns. We stop when the task calls complete_task.

The loop is content-agnostic: it appends whatever `system_addendum` it is given
(API catalog, self-verify rules, demos) without knowing what's in it. That keeps
the model boundary (model.py) and the prompt boundary (run.py) cleanly separate.
"""
from __future__ import annotations

import json
import re

# Prefer a complete ```python ... ``` block; fall back to a partial/open one.
_FULL_CODE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)
_PARTIAL_CODE = re.compile(r"```(?:python)?\s*\n(.*)", re.S)


def find_code_block(text: str) -> str | None:
    """Return the python code from a reply, or None if there is no code block.

    Returning None (rather than the raw prose) lets the loop *nudge* the model
    instead of executing English as Python — a common failure when the model
    narrates without emitting a block.
    """
    m = _FULL_CODE.search(text)
    if m:
        return m.group(1).strip()
    m = _PARTIAL_CODE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _render_preamble(preamble: str, world) -> str:
    """Fill the react-instructions placeholders for this task."""
    s = world.task.supervisor
    app_descriptions = json.dumps(world.task.app_descriptions, indent=1)
    return (
        preamble
        .replace("{{ main_user.first_name }}", str(s.first_name))
        .replace("{{ main_user.last_name }}", str(s.last_name))
        .replace("{{ main_user.email }}", str(s.email))
        .replace("{{ main_user.phone_number }}", str(s.phone_number))
        .replace("{{ app_descriptions }}", app_descriptions)
    )


_NO_CODE_NUDGE = (
    "I don't see a python code block in your last message. Respond with exactly ONE "
    "```python ... ``` block that makes progress on the task, or that calls "
    "apis.supervisor.complete_task(...) if you are done."
)


class ReActAgent:
    def __init__(
        self,
        model,
        preamble: str,
        max_steps: int = 40,
        max_obs_chars: int = 3000,
        system_addendum: str = "",
        retriever=None,
    ) -> None:
        self.model = model
        self.preamble = preamble
        self.max_steps = max_steps
        self.max_obs_chars = max_obs_chars
        # Static text appended to every system prompt (catalog, self-verify, ...).
        self.system_addendum = system_addendum
        # Optional per-task few-shot demo retriever (.render(instruction) -> str).
        self.retriever = retriever

    def _observation(self, output: str) -> str:
        out = output if len(output) <= self.max_obs_chars else (
            output[: self.max_obs_chars] + "\n...[output truncated]"
        )
        return f"Output:\n```\n{out}\n```"

    def solve(self, world) -> bool:
        """Run the loop on one task. Returns True if the task was completed."""
        system = _render_preamble(self.preamble, world) + self.system_addendum
        if self.retriever is not None:
            # Per-task: retrieve similar solved train tasks as worked examples.
            system += self.retriever.render(world.task.instruction)
        s = world.task.supervisor
        task_msg = (
            "Using these APIs, now generate code to solve the actual task:\n\n"
            f"My name is: {s.first_name} {s.last_name}. My personal email is {s.email} "
            f"and phone number is {s.phone_number}.\n"
            f"Task: {world.task.instruction}\n\n"
            "Begin. Remember: exactly one python code block per turn."
        )
        messages: list[dict] = [{"role": "user", "content": task_msg}]

        no_code_streak = 0
        for _ in range(self.max_steps):
            reply = self.model.complete(system, messages)
            code = find_code_block(reply)

            if code is None:
                # Model narrated without emitting a block — nudge rather than
                # executing prose as Python.
                no_code_streak += 1
                messages.append({"role": "assistant", "content": reply.strip() or "(no content)"})
                messages.append({"role": "user", "content": _NO_CODE_NUDGE})
                if no_code_streak >= 3:
                    return False  # stuck narrating; bail rather than burn steps
                continue

            no_code_streak = 0
            output = world.execute(code)
            messages.append({"role": "assistant", "content": f"```python\n{code}\n```"})
            messages.append({"role": "user", "content": self._observation(output)})
            if world.task_completed():
                return True
        return False
