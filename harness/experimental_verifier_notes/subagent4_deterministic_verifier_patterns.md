# Subagent 4: Deterministic Verifier Patterns

Scope: prompt and harness guard patterns for AppWorld that stay generic. Do not branch on `task_id`, dataset row, or known final answers.

## Current Signals

- `harness/run.py` already adds decomposition and schema-inspection hints by default. The explicit self-verify addendum exists but is off unless `SELF_VERIFY=1`.
- The ReAct loop in `harness/agent.py` executes exactly one Python block per turn and stops as soon as `apis.supervisor.complete_task(...)` marks completion.
- Evaluation reports show repeat misses from invented API names, completion after failed or incomplete side effects, and wrong action type, especially cart-only Amazon work, sending Gmail email when a draft was required, and alarm/update tasks with no final state change.

## Generic Verification Prompt

Add this as a default-on verifier addendum, after the API/schema inspection hint:

```text
Before complete_task, run a final verifier block.

1. Decide whether the task is answer-only or state-changing.
   - Answer-only: compute the answer from read-only APIs, independently re-read or recompute it, assert both agree, then call complete_task(answer=<exact_value>).
   - State-changing: snapshot relevant records before mutation, perform the mutation, read the affected records back through public APIs, assert the exact delta and fields, then call complete_task(answer=None) or complete_task(status="success") with no prose answer.

2. Never call complete_task in a block that still contains unverified mutating API calls before it. The final block should be read-back checks, asserts, then complete_task.

3. If any API call raises, do not complete. First read the current state, determine which side effects already happened, then perform only the remaining idempotent work.

4. Do not invent API names. If an API name fails or is uncertain, call show_api_descriptions(app_name=...) and show_api_doc(app_name=..., api_name=...) before retrying.

5. Do not use a fixed page count as proof of completeness. Paginate until a page is empty or shorter than page_limit. Print one sample from each distinct source before indexing fields.

6. For multi-source tasks, verify every named source contributed: enumerate sources, gather each to exhaustion, merge by stable IDs, then aggregate.
```

## Harness Guards

- Pre-complete nudge: when code contains `complete_task`, reject it once unless the same block also contains at least one `assert` or an explicit `verified = True` assigned after read-back calls. This is generic and does not inspect task IDs.
- Exception guard: if the previous observation starts with `Execution failed`, append a stronger recovery instruction: "Do not complete next. Re-read state and reconcile partial side effects first."
- API-name guard: on observations containing `No API named`, append the exact app's available API list from `api_docs.show_api_descriptions` if already known, and require `show_api_doc` before the replacement call.
- Completion formatting guard: if the task contains mutating verbs (`buy`, `order`, `send`, `create`, `delete`, `update`, `disable`, `pay`, `request`, `add`, `remove`, `draft`, `schedule`), bias to `answer=None` and state read-back. If it asks `how many`, `what`, `which`, `list`, or `return`, bias to exact scalar/list answer and no mutation.

## Safe App-Specific Checks

- Amazon:
  - Valid names include `show_wish_list`, `add_product_to_cart`, `show_cart`, `place_order`, `show_orders`, `show_order`, `show_payment_cards`, `show_addresses`, `show_prime_subscriptions`.
  - For buy/order tasks, cart state is not success. Verify `place_order` returned `order_id`, then call `show_order(order_id=...)` and assert product IDs, quantities, address ID/text, payment card, price/type/rating constraints, and seller trust constraints if requested.
  - For wishlist tasks, snapshot wishlist item product IDs and quantities, then assert the new order's order items match that map. Avoid duplicate ordering by checking recent orders before retrying after an exception.
  - For Prime answer tasks, use `show_prime_subscriptions`, compare current task datetime against subscription start/end data, and return the rounded month count as a digit.

- Gmail:
  - Valid thread APIs are `show_inbox_threads`, `show_outbox_threads`, `show_thread`, `show_email`; there is no `show_emails` or `show_inbox`.
  - For draft tasks, snapshot `show_drafts`, mutate with `create_draft`/`update_draft`/`delete_draft`, then `show_draft` or re-list drafts and assert exact added/deleted IDs plus subject, stripped body, recipients, attachments, and `scheduled_send_at`.
  - For send tasks, verify the returned `sent_email_thread_id`/`sent_email_id` with `show_thread`/`show_email`. Do not use `send_email` when the task asks to draft or schedule.

- Phone:
  - Valid lookup APIs include `search_contacts`, `search_text_messages`, `show_text_message_window`, `show_text_message`, `show_alarms`, `show_alarm`, `update_alarm`, `send_text_message`.
  - For text tasks, verify the returned `text_message_id` with `show_text_message`, checking recipient phone number and exact message content.
  - For alarm tasks, snapshot all alarms, identify targets by label/time/date evidence, call `update_alarm(enabled=False, ...)`, then `show_alarm` and assert target alarms are disabled and no unrelated alarms changed.

- Splitwise:
  - Use `show_groups`, `show_group`, `record_expense`, `show_group_expenses`, and `show_expense`.
  - For group expenses, verify `expense_id` via `show_expense`, asserting `group_id`, description/note text, paid amount, participant emails/IDs, and per-person share amounts. Recompute equal splits from the task data and total, including the supervisor's share when requested.

- File system and attachments:
  - Valid APIs are `show_directory`, `show_file`, `create_directory`, `create_file`, `file_exists`, and `directory_exists`; there is no `read_file`.
  - After downloads or created files, verify both directory existence and file content/path before attaching or completing.

- SimpleNote / Spotify / Todoist:
  - SimpleNote: use `search_notes` then `show_note`; verify parsed schedule/content against the raw note text before creating downstream drafts or tasks.
  - Spotify: when tasks say songs, albums, and playlists, gather each library type separately to page exhaustion before merging.
  - Todoist: after create/update/delete/complete actions, re-list the relevant project/task and assert title, due date, assignee/collaborator, priority, and completion state.

## Failure Recovery Rules

- `No API named ...`: stop using that name; refresh app descriptions, inspect the replacement API doc, then retry with exact parameter names.
- `StopIteration` or empty search: print candidate counts and samples, broaden the query, search all relevant categories/pages, then filter locally.
- Parameter/type error: call `show_api_doc` for that API and retry once with exact parameters. Do not guess alternate parameter names.
- Partial side effect after exception: read state first. If the desired record exists, verify it and continue; if only part exists, finish the missing fields; if a duplicate risk exists, match by stable keys before creating another record.
- Max-step pressure: skip further broad exploration, do one compact read-back over already-identified IDs, assert the main success condition, then complete only if it passes.
