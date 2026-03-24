# Plan: Digest feedback reactions (Phase 3)

## Context
Phase 1 (profile simplification) and Phase 2 (granular `/topic`, `/block`, `/unblock` commands) are complete. Phase 3 adds 👍/👎 inline buttons on individual articles in the digest, so the bot auto-adjusts interest area weights based on user feedback.

Currently the digest is sent as a single `.md` file. The user has no way to signal which topics were useful vs. not — weight tuning is entirely manual (`/topic weight`).

## Design

### Core flow
1. After sending the digest file, bot sends a short preview message per article with 👍/👎 inline buttons
2. User taps a button → bot adjusts matching interest area weights by ±0.03 (clamped 0.05–1.0)
3. Feedback is persisted in a new `article_feedback` DB table for future analytics

### Orchestrator return type change
`orchestrator.run()` currently returns `str | None` (file path). Change to return a `DigestResult` dataclass containing the file path + cluster/article metadata needed for feedback messages.

### Mapping clusters → interest areas
Each cluster contains items with `tags`. Match item tags against interest area keywords (case-insensitive). Matched area IDs are the weight-adjustment targets. Imperfect matching is acceptable.

### Callback data format
`fb:L:<6-char-hex>` / `fb:D:<6-char-hex>` — 15 bytes, well within Telegram's 64-byte limit. The hex ID maps to cluster metadata stored in-memory (`self._feedback_map`).

### Article previews
Send cluster title (bold) + first ~300 chars of article text (truncated at sentence boundary). Well under Telegram's 4096-char limit.

## Files to modify

### 1. `src/db/models.py` — add `DigestResult`
```python
@dataclass
class DigestResult:
    file_path: str
    clusters: list[Cluster]
    articles: dict[str, str]       # cluster_id → article markdown
    quick_bites_item_ids: list[str]
    items: list[Item]
```

### 2. `src/db/database.py` — add `article_feedback` table
- Add to `SCHEMA_SQL`:
  ```sql
  CREATE TABLE IF NOT EXISTS article_feedback (
      id TEXT PRIMARY KEY,
      week_id TEXT NOT NULL,
      cluster_id TEXT NOT NULL,
      cluster_title TEXT NOT NULL,
      feedback TEXT NOT NULL,
      matched_area_ids TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL
  );
  ```
- Add `save_article_feedback(...)` method
- Add `get_feedback_by_week(week_id)` method (for future use)

### 3. `src/pipeline/orchestrator.py` — return `DigestResult`
- Import `DigestResult` from models
- Change return type: `run() -> DigestResult | None`
- At end of pipeline, return `DigestResult(file_path, clusters, articles, quick_bites_item_ids, items)` instead of just `str(file_path)`

### 4. `src/telegram/bot.py` — main implementation
- Add `self._feedback_map: dict = {}` in `__init__`
- Add `_truncate_preview(text, max_len=300) -> str` helper
- Add `_map_cluster_to_areas(cluster, items, interest_areas) -> list[str]` helper
- Add `_send_feedback_messages(chat_id, context, result, profile)` method:
  - For each cluster: send title + preview + 👍/👎 buttons
  - `asyncio.sleep(0.3)` between sends to avoid rate limits
  - Quick bites get one combined message
- Add `_handle_feedback_callback(update, context)`:
  - Parse `fb:L:hex` / `fb:D:hex`
  - Pop from `_feedback_map`
  - Save to DB via `save_article_feedback`
  - Adjust matching area weights ±0.03 (clamped 0.05–1.0)
  - Call `_save_profile()` to persist + refresh filter agent
  - `query.answer("👍 Noted!")` + remove buttons from message
- Register `CallbackQueryHandler(pattern=r"^fb:")` in `build()`
- Update all 4 callers of `orchestrator.run()`:
  - `_handle_generate`: use `result.file_path` for send_document, then call `_send_feedback_messages`
  - `_handle_dryrun`: use `result.file_path`, skip feedback messages
  - `_handle_regenerate`: use `result.file_path`, send feedback messages
  - `scheduler.py:scheduled_generate`: use `result.file_path` for send_document, send feedback messages

### 5. `src/pipeline/scheduler.py` — update for new return type
- `result.file_path` instead of `result` in `open(result, "rb")`
- Optionally send feedback messages after scheduled digest (needs bot reference or skip for scheduler)

## Verification
1. `pytest tests/` — all existing tests pass (orchestrator return type change may need test updates)
2. `/generate` → digest file sent, then per-article preview messages with 👍/👎 buttons appear
3. Tap 👍 → weight increases by 0.03, buttons disappear, toast confirmation
4. Tap 👎 → weight decreases by 0.03
5. `/topic` → verify weights changed correctly
6. `/dryrun` → no feedback messages sent
7. Bot restart → old feedback buttons gracefully show "Feedback expired" on tap
