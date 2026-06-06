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
import os
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

_COMPLETE_TASK_RE = re.compile(r"apis\.supervisor\.complete_task\s*\(")
_ANSWER_NONE_RE = re.compile(r"complete_task\s*\(\s*(?:answer\s*=\s*)?(?:None|null)?\s*\)")
_FAIL_RE = re.compile(r"complete_task\s*\([^)]*status\s*=\s*['\"]fail['\"]")
_MUTATING_API_RE = re.compile(
    r"apis\.(?!api_docs|supervisor)\w+\.\w*"
    r"(add|archive|buy|cancel|create|delete|disable|enable|insert|move|order|"
    r"pay|place|purchase|remove|request|schedule|send|set|start|stop|update|write)"
    r"\w*\s*\(",
    re.I,
)
_READBACK_RE = re.compile(
    r"(assert\s+|apis\.(?!api_docs|supervisor)\w+\.\w*"
    r"(find|get|list|read|search|show)\w*\s*\()",
    re.I,
)


def _completion_guard(
    code: str,
    traits: dict,
    mutation_seen: bool,
    verified_after_mutation: bool,
    seen_code: str,
) -> str | None:
    """Return a nudge if a proposed complete_task call is premature.

    This is a generic safety valve for weak models. It does not inspect task IDs
    or expected answers; it only enforces the AppWorld contract implied by the
    instruction.
    """
    if not _COMPLETE_TASK_RE.search(code):
        return None
    if _FAIL_RE.search(code):
        return (
            "Do not exit with status='fail' yet. The AppWorld tasks are intended "
            "to be solvable. Inspect the relevant API docs/data, choose a concrete "
            "next action, and continue with exactly one python code block."
        )
    if traits.get("needs_answer") and _ANSWER_NONE_RE.search(code):
        return (
            "This is an answer task, but your complete_task call has no concrete "
            "answer. Compute the minimal requested value first, print it, and then "
            "call apis.supervisor.complete_task(answer=value)."
        )
    if traits.get("mutates_state"):
        workflow = traits.get("workflow")
        all_code = seen_code + "\n" + code
        required_api_nudge = _required_workflow_api_nudge(workflow, all_code)
        if required_api_nudge is not None:
            return required_api_nudge
        code_has_mutation = bool(_MUTATING_API_RE.search(code))
        code_has_readback = bool(_READBACK_RE.search(code))
        code_has_assert = "assert " in code or "raise AssertionError" in code
        if not (mutation_seen or code_has_mutation):
            return (
                "This task requires a state change, but I have not seen a write API "
                "call yet. Do not complete. Inspect the exact write API docs, perform "
                "the requested mutation, read back the affected records, then complete."
            )
        if not (verified_after_mutation or (code_has_readback and code_has_assert)):
            return (
                "Before complete_task, verify the state change with assertions, not just prints. "
                "Run one code block that reads back the affected app records through app APIs and "
                "uses assert statements on the exact changed ids/fields/counts. Then complete."
            )
    return None


def _required_workflow_api_nudge(workflow: str | None, all_code: str) -> str | None:
    """Require the core mutation API for broad workflow families.

    This is still generic: it depends only on workflow/app family, never task id,
    final answer, or private expected values.
    """
    if workflow == "shopping_order" and "apis.amazon.place_order" not in all_code:
        return (
            "This is an Amazon buy/order/purchase task. Cart changes are not enough. "
            "Inspect `amazon.place_order`, place the order with a real address and payment card, "
            "then read back the created order/order_items and assert the product ids, quantities, "
            "and address before complete_task."
        )
    if workflow == "gmail_draft_or_send":
        lower = all_code.lower()
        if "draft" in lower and "apis.gmail.create_draft" not in all_code and "apis.gmail.update_draft" not in all_code:
            return (
                "This Gmail workflow requires draft records. Sending emails is not enough when "
                "the task asks for drafts or scheduled reminder emails. Use `gmail.create_draft` "
                "or `gmail.update_draft`, then read back the drafts and assert subject/body/"
                "recipients/scheduled_send_at before complete_task."
            )
    if workflow == "phone_alarm" and "apis.phone.update_alarm" not in all_code:
        return (
            "This phone alarm task requires updating existing alarms. Inspect `phone.update_alarm`, "
            "update the matching alarm ids, then read back those same ids and assert their enabled/"
            "time/label fields before complete_task."
        )
    if workflow == "money_splitwise_venmo":
        lower = all_code.lower()
        if "splitwise" in lower and "apis.splitwise.record_expense" not in all_code:
            return (
                "This Splitwise workflow requires `splitwise.record_expense`; downloading or reading "
                "bills is not enough. Record the expenses, read back the created expenses/shares, "
                "and assert group, payer, descriptions, amounts, and members before complete_task."
            )
    return None


class ReActAgent:
    def __init__(
        self,
        model,
        preamble: str,
        max_steps: int = 40,
        max_obs_chars: int = 3000,
        system_addendum: str = "",
        retriever=None,
        playbook_renderer=None,
    ) -> None:
        self.model = model
        self.preamble = preamble
        self.max_steps = max_steps
        self.max_obs_chars = max_obs_chars
        # Static text appended to every system prompt (catalog, self-verify, ...).
        self.system_addendum = system_addendum
        # Optional per-task few-shot demo retriever (.render(instruction) -> str).
        self.retriever = retriever
        # Optional per-task generic workflow router (.render(instruction) -> str).
        self.playbook_renderer = playbook_renderer

    def _observation(self, output: str) -> str:
        out = output if len(output) <= self.max_obs_chars else (
            output[: self.max_obs_chars] + "\n...[output truncated]"
        )
        return f"Output:\n```\n{out}\n```"

    def solve(self, world) -> bool:
        """Run the loop on one task. Returns True if the task was completed."""
        system = _render_preamble(self.preamble, world) + self.system_addendum
        instruction = world.task.instruction
        traits = {}
        if self.playbook_renderer is not None:
            system += self.playbook_renderer(instruction)
            try:
                from harness.playbooks import expected_workflow, infer_task_traits
                traits = infer_task_traits(instruction)
                traits["workflow"] = expected_workflow(instruction)
            except Exception:
                traits = {}
        if self.retriever is not None:
            # Per-task: retrieve similar solved train tasks as worked examples.
            system += self.retriever.render(instruction)
        s = world.task.supervisor
        task_msg = (
            "Using these APIs, now generate code to solve the actual task:\n\n"
            f"My name is: {s.first_name} {s.last_name}. My personal email is {s.email} "
            f"and phone number is {s.phone_number}.\n"
            f"Task: {instruction}\n\n"
            "Begin. Remember: exactly one python code block per turn."
        )
        messages: list[dict] = [{"role": "user", "content": task_msg}]

        no_code_streak = 0
        mutation_seen = False
        verified_after_mutation = False
        seen_code = ""
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
            guard = None
            if os.environ.get("COMPLETE_GUARD", "1") == "1":
                guard = _completion_guard(code, traits, mutation_seen, verified_after_mutation, seen_code)
            if guard is not None:
                messages.append({"role": "assistant", "content": f"```python\n{code}\n```"})
                messages.append({"role": "user", "content": guard})
                continue

            code_has_mutation = bool(_MUTATING_API_RE.search(code))
            code_has_readback = bool(_READBACK_RE.search(code))
            code_has_assert = "assert " in code or "raise AssertionError" in code
            output = world.execute(code)
            code_succeeded = "Traceback" not in output and "Execution failed" not in output
            seen_code += "\n" + code
            if code_has_mutation and code_succeeded:
                mutation_seen = True
                verified_after_mutation = code_has_readback and code_has_assert
            elif mutation_seen and code_has_readback and code_has_assert and code_succeeded:
                verified_after_mutation = True
            messages.append({"role": "assistant", "content": f"```python\n{code}\n```"})
            messages.append({"role": "user", "content": self._observation(output)})
            if world.task_completed():
                return True
        return False
