"""Generic AppWorld playbooks.

These are deliberately keyed by task language and app/workflow concepts, not by
task_id. The goal is to give the weaker scored model a stable execution skeleton
before it writes code.
"""
from __future__ import annotations

import re
import os
from dataclasses import dataclass

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Playbook:
    name: str
    apps: tuple[str, ...]
    mutates_state: bool
    needs_answer: bool | None
    guidance: str


_COMMON = """\
Execution contract:
1. Identify every app and entity type the task mentions or implies.
2. Get credentials with supervisor.show_account_passwords(), log in to every app you use, and inspect the exact API docs before calling an API.
3. For every paginated read, fetch all pages until an empty page or the documented end condition.
4. Print one representative result from each distinct API before processing it; use observed keys only.
5. Keep a compact local checklist of required outputs or state changes.
6. If the task changes state, perform the write through the exact app API, then read back the affected records through app APIs and assert the change is visible before complete_task.
7. If the task asks a question, complete_task(answer=<minimal value>). If it asks you to do something, complete_task() with no answer or answer=None.
"""


_PLAYBOOKS: list[Playbook] = [
    Playbook(
        name="shopping_order",
        apps=("amazon", "gmail", "file_system"),
        mutates_state=True,
        needs_answer=False,
        guidance="""\
Shopping/order workflow:
- Words like buy, order, purchase, get, checklist, wishlist, gift, delivery, seller, rating, price, product type mean this is an Amazon mutation task, not an answer-only task.
- Inspect amazon APIs for login, product search/show, cart/order creation, address/card selection, and any wishlist/checklist APIs.
- If product requirements are hidden in Gmail attachments or File System files, first retrieve/download/read those sources; do not guess product ids.
- Choose products by applying every constraint: product_type, price range, quantity, seller/trusted seller, rating, wishlist/checklist membership, and delivery address.
- Cart-only is usually insufficient when the instruction says buy/order/purchase. Verify by reading back the created order and its order_items.
""",
    ),
    Playbook(
        name="gmail_draft_or_send",
        apps=("gmail", "phone", "file_system"),
        mutates_state=True,
        needs_answer=False,
        guidance="""\
Gmail draft/send workflow:
- Distinguish draft/schedule from send. If the task says draft, create gmail.Draft records; do not send emails.
- Resolve recipients through contacts, Gmail threads, or existing messages before writing.
- For scheduled drafts, compute exact datetimes from the task using the current app/Python time, then read back subject, body, recipient_ids, and scheduled_send_at.
- Empty body means body="" after stripping, not omitted unless the API doc says omitted creates empty.
""",
    ),
    Playbook(
        name="phone_alarm",
        apps=("phone",),
        mutates_state=True,
        needs_answer=False,
        guidance="""\
Phone mutation workflow:
- For alarm/reminder tasks, inspect existing records first and update the matching records; do not only answer.
- For disable/enable/update/delete, read all candidate alarms/messages, select by exact time/date/label/relation constraints, perform the mutation, then read back the same ids.
- For text tasks, resolve the recipient from contacts and verify exactly one intended message was created with the requested content.
""",
    ),
    Playbook(
        name="phone_text",
        apps=("phone", "amazon", "gmail"),
        mutates_state=True,
        needs_answer=False,
        guidance="""\
Phone text workflow:
- Resolve the recipient from contacts and verify the exact target phone number.
- Build the full message content from source apps first; preserve units, decimals, and requested formatting exactly.
- Send once, then read back the message and assert recipient and content.
""",
    ),
    Playbook(
        name="money_splitwise_venmo",
        apps=("splitwise", "venmo", "file_system", "gmail"),
        mutates_state=True,
        needs_answer=None,
        guidance="""\
Money workflow:
- Splitwise group/member ids must come from Splitwise data, not phone contact ids unless the API docs explicitly connect them.
- For bills/expenses from files or messages, extract the exact descriptions, dates, amounts, group, payer, and member shares before writing.
- Verify totals: sum of shares equals expense amount, all required members are included exactly once per expense, and group_id matches the task's group.
- Venmo payment/request tasks require the correct counterparty and amount; read back transactions/requests after writing.
""",
    ),
    Playbook(
        name="answer_lookup",
        apps=(),
        mutates_state=False,
        needs_answer=True,
        guidance="""\
Answer-only lookup workflow:
- Do not call complete_task(answer=None) for a question. Compute a concrete answer and pass it as the minimal scalar/list requested.
- For counts/totals, collect all pages and all named sources before aggregating.
- Print the final computed value once, sanity-check type/format, then complete_task(answer=value).
""",
    ),
    Playbook(
        name="file_note_data",
        apps=("file_system", "simple_note", "gmail"),
        mutates_state=None,
        needs_answer=None,
        guidance="""\
File/note workflow:
- References to file system mean the file_system app, never local OS files.
- For create/update/delete requests, verify the file/note exists with expected path/title/content after mutation.
- For lookup tasks, read all matching candidate files/notes and inspect content before answering.
""",
    ),
    Playbook(
        name="spotify_library",
        apps=("spotify",),
        mutates_state=None,
        needs_answer=None,
        guidance="""\
Spotify workflow:
- A song can appear in song library, saved albums, playlists, or queue; enumerate every named source.
- For add/remove/play tasks, read back the library/playlist/player state after mutation.
- For counts/rankings, paginate all libraries and deduplicate by observed song/album/playlist ids.
""",
    ),
]


_MUTATION_TERMS = {
    "add", "archive", "buy", "cancel", "create", "delete", "disable", "draft",
    "enable", "move", "order", "pay", "place", "purchase", "remove", "request",
    "record", "schedule", "send", "set", "split", "start", "stop", "turn",
    "update", "write",
}
_QUESTION_TERMS = {
    "how", "what", "which", "who", "whom", "when", "where", "count", "number",
    "total", "sum", "average", "list", "find", "tell",
}


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _mentions_any(text: str, words: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(w in low for w in words)


def infer_task_traits(instruction: str) -> dict:
    toks = _tokens(instruction)
    lower = instruction.lower()
    mutates = bool(toks & _MUTATION_TERMS)
    asks_question = instruction.strip().endswith("?") or bool(toks & _QUESTION_TERMS)
    # Imperatives like "find the item and buy it" need mutation treatment even
    # though they contain lookup words.
    if _mentions_any(lower, ("buy", "order", "purchase", "send", "draft", "schedule", "disable", "enable", "record expense")):
        mutates = True
    return {"mutates_state": mutates, "needs_answer": asks_question and not mutates}


def route_playbook(instruction: str) -> Playbook:
    lower = instruction.lower()
    toks = _tokens(instruction)
    traits = infer_task_traits(instruction)
    if traits["needs_answer"]:
        return _PLAYBOOKS[5]
    if _mentions_any(lower, ("text message", "phone text", "sms", "send a text")) and not _mentions_any(lower, ("buy", "order", "purchase")):
        return _PLAYBOOKS[3]
    if _mentions_any(lower, ("amazon", "buy", "order", "purchase", "wishlist", "seller", "delivery", "product")):
        if _mentions_any(lower, ("buy", "order", "purchase", "wishlist", "delivery", "seller", "trusted")):
            return _PLAYBOOKS[0]
        return _PLAYBOOKS[6]
    if _mentions_any(lower, ("gmail", "email", "draft", "scheduled send", "recipient", "inbox")):
        return _PLAYBOOKS[1]
    if _mentions_any(lower, ("alarm", "reminder")):
        return _PLAYBOOKS[2]
    if _mentions_any(lower, ("splitwise", "venmo", "expense", "bill", "debt", "owe", "paid", "payment")):
        return _PLAYBOOKS[4]
    if _mentions_any(lower, ("file", "folder", "directory", "note", "simple note", "document")):
        return _PLAYBOOKS[6]
    if _mentions_any(lower, ("spotify", "playlist", "song", "album", "music", "queue")):
        return _PLAYBOOKS[7]
    if traits["needs_answer"] or not (toks & _MUTATION_TERMS):
        return _PLAYBOOKS[5]
    return _PLAYBOOKS[6]


def render_playbook(instruction: str) -> str:
    playbook = route_playbook(instruction)
    scope = os.environ.get("PLAYBOOK_SCOPE", "high_risk").lower()
    if scope == "off":
        return ""
    if scope == "high_risk" and playbook.name not in {
        "shopping_order", "gmail_draft_or_send", "phone_alarm", "money_splitwise_venmo"
    }:
        return ""
    traits = infer_task_traits(instruction)
    return (
        "\n\n# Selected generic playbook\n"
        f"Router choice: {playbook.name}\n"
        f"Likely apps: {', '.join(playbook.apps) if playbook.apps else 'discover from task/API docs'}\n"
        f"Task mutates state: {traits['mutates_state']}\n"
        f"Task needs answer: {traits['needs_answer']}\n\n"
        f"{_COMMON}\n{playbook.guidance}\n"
        "This playbook is generic. Use it to structure the solution, but derive all ids, values, dates, and records from app APIs in the current task.\n"
    )


def expected_workflow(instruction: str) -> str:
    return route_playbook(instruction).name
