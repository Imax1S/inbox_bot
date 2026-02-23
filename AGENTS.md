# Inbox Agent Bot — Agent Guide

- **Purpose:** Telegram bot that collects notes, links, and ideas throughout the week, then processes them through a multi-agent LLM pipeline to generate structured weekly digest notes for Obsidian.
- **Runtime:** Python 3.11+ (3.12 in Docker). Install deps with `pip install -r requirements.txt` or run `docker-compose up`. Start with `python -m src.main`.
- **Secrets/config:** Copy `.env.example` → `.env` and set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`, `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` with `LLM_PROVIDER=openai`). Vault path set via `OBSIDIAN_VAULT_PATH` (default `/vault/life/weekly`). User interests in `user_profile.json`; edit that file rather than hardcoding.

## Key Files

| File | Role |
|------|------|
| `src/main.py` | Entry point — wires bot, DB, agents, orchestrator, scheduler |
| `src/config.py` | Dataclass-based config from `.env` + `user_profile.json` |
| `src/telegram/bot.py` | `DigestBot` — all Telegram commands and message handlers |
| `src/agents/base.py` | `BaseAgent` — prompt loading, `_call_llm()` (free-text) / `_call_llm_structured()` (tool use), step logging |
| `src/agents/collector.py` | Classifies & summarizes incoming messages (structured output via tool use) |
| `src/agents/filter.py` | Filters irrelevant/duplicate items before clustering (structured output) |
| `src/agents/clusterer.py` | Groups items into 3-6 topic clusters + quick bites (structured output) |
| `src/agents/researcher.py` | Produces research briefs per cluster |
| `src/agents/writer.py` | Writes magazine-quality articles per cluster |
| `src/agents/editor.py` | Assembles the final weekly magazine |
| `src/agents/translator.py` | Translates magazine to user's chosen language |
| `src/agents/profiler.py` | Extracts user interests from free-form text (/setup) (structured output) |
| `src/content/text_classifier.py` | Regex-based message classification (ARTICLE/TOPIC_SEED/CONTEXT_NOTE) |
| `src/content/url_parser.py` | Fetches & extracts article text; special handling for Twitter/X via oEmbed + FxTwitter |
| `src/db/database.py` | Async SQLite interface (items, pipeline_runs, step_logs, settings) |
| `src/db/models.py` | Dataclasses: Item, Cluster, PipelineRun, StepLog, enums |
| `src/llm/provider.py` | `LLMProvider` protocol with `generate()` + `generate_structured()` (tool use/function call); `AnthropicProvider`, `OpenAIProvider`, cost estimation |
| `tests/conftest.py` | `MockLLMProvider` + shared fixtures for agent tests (no real API calls) |
| `tests/test_agents.py` | Happy-path & fallback tests for Collector, Clusterer, Filter, Profiler |
| `tests/test_base_agent.py` | Tests for `_call_llm_structured()` in BaseAgent |
| `tests/test_provider.py` | Tests for provider layer |
| `src/pipeline/orchestrator.py` | Runs multi-agent pipeline sequentially (Filter→Cluster→Research→Write→Edit→Translate) |
| `src/pipeline/scheduler.py` | Weekly digest schedule (default: Sunday 23:00 Europe/Berlin) |
| `src/pipeline/status_updater.py` | Real-time Telegram progress updates during pipeline run |
| `src/obsidian_writer.py` | Saves digest as `YYYY-Www.md` in Obsidian vault |
| `prompts/*.txt` | One LLM system prompt per agent — edit these to change agent behavior |
| `user_profile.json` | User interests, style prefs, language config (passed to agents) |

## Data Flow

1. User sends Telegram message → `text_classifier.py` classifies as ARTICLE/TOPIC_SEED/CONTEXT_NOTE
2. If URL: `url_parser.py` fetches article text (Twitter/X handled via oEmbed → FxTwitter fallback)
3. `CollectorAgent` summarizes & tags via LLM → item saved to SQLite `items` table
4. On weekly trigger or `/generate`: `Orchestrator` runs pipeline:
   - **Filter** — removes irrelevant, duplicate, shallow, or noisy items; reports filtered items to user
   - **Clusterer** — groups remaining items into 3-6 topic clusters + quick bites list
   - **Researcher** — research brief per cluster
   - **Writer** — magazine-quality article per cluster
   - **Editor** — assembles final Markdown document
   - **Translator** — translates to user's language (skipped if English)
5. `ObsidianWriter` saves `YYYY-Www.md` to vault; file sent to user via Telegram

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome + command list |
| `/generate` | Run digest pipeline now (alias: `/digest`) |
| `/items` | List this week's collected items |
| `/delete <id>` | Remove an item by short ID |
| `/setup` | Configure interest profile (multi-step wizard) |
| `/language` | Choose digest language — Russian/English (alias: `/lang`) |
| `/provider` | Switch LLM provider (Anthropic/OpenAI) |
| `/estimate` | Predict generation cost before running |
| `/status` | Last pipeline run status + token/cost summary |
| `/logs` | Last pipeline run step-by-step logs |
| `/cost` | Token usage & cost report with run history |
| `/week` | Current week info and item counts |
| `/cancel` | Cancel an in-progress `/setup` conversation |

## Database Schema

Four SQLite tables (see `src/db/database.py`):

- **items** — collected messages: `type`, `raw_content`, `source_url`, `extracted_text`, `summary`, `tags`, `language`, `week_id`, `status`
- **pipeline_runs** — execution history: `week_id`, `status`, token totals, `estimated_cost_usd`
- **step_logs** — per-agent logs: `agent`, `llm_model`, `input_tokens`, `output_tokens`, `duration`, `error`
- **settings** — key-value store: `digest_language`, `user_profile`, `filtering_strictness`, `llm_provider`

## Running Tests

```bash
pytest tests/
```

Uses `MockLLMProvider` (in `tests/conftest.py`) to simulate LLM responses without real API calls. 17 tests covering the provider layer, `BaseAgent._call_llm_structured()`, and all four structured-output agents (Collector, Clusterer, Filter, Profiler) — both happy path and fallback behavior.

## Conventions

- **Async throughout** — all I/O uses `async`/`await`
- **Prompts live in files** — edit `prompts/*.txt`, not agent code, to change LLM behavior
- **User profile is JSON** — edit `user_profile.json` or use `/setup`; never hardcode preferences
- **Standard logging** — use Python `logging`, not `print` (except startup banner)
- **Never commit** `.env`, `data/*.db`, or Obsidian vault content
- **When adding deps** update `requirements.txt`
