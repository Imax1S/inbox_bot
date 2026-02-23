# CLAUDE.md — AI Assistant Guide for Inbox Agent Bot

## Project Overview

An AI-powered Telegram bot that collects notes, links, and ideas throughout the week, then processes them through a multi-agent LLM pipeline to generate structured weekly digest notes for Obsidian.

**Core principle:** "Better to save something doubtful than delete something useful."

## Quick Reference

| Aspect | Details |
|--------|---------|
| Language | Python 3.11+ (3.12 in Docker) |
| Entry point | `python -m src.main` |
| Architecture | Multi-agent async LLM pipeline |
| Database | SQLite via aiosqlite |
| UI | Telegram bot |
| Output | Markdown files in Obsidian vault |
| Deployment | Docker / docker-compose |
| Tests | pytest + pytest-asyncio (run: `pytest tests/`) |

## How to Run

```bash
# Docker (recommended)
docker-compose up

# Direct
pip install -r requirements.txt
python -m src.main
```

The app requires a `.env` file — copy from `.env.example` and fill in secrets. Required variables: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`, `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` with `LLM_PROVIDER=openai`).

## Project Structure

```
src/
├── main.py                    # Entry point — wires all components
├── config.py                  # Dataclass-based config from .env + user_profile.json
├── obsidian_writer.py         # Writes digest to Obsidian vault as YYYY-Www.md
├── agents/                    # LLM-powered agents (all extend BaseAgent)
│   ├── base.py               # BaseAgent: prompt loading, LLM calling (_call_llm / _call_llm_structured), step logging
│   ├── collector.py           # Classifies & summarizes incoming messages (structured output)
│   ├── clusterer.py           # Groups items into 3-6 topic clusters (structured output)
│   ├── filter.py              # Filters irrelevant/duplicate items before digest generation (structured output)
│   ├── profiler.py            # Extracts user interests from free-form text for profile setup (structured output)
│   ├── researcher.py          # Produces research briefs per cluster
│   ├── writer.py              # Writes magazine-quality articles per cluster
│   ├── editor.py              # Assembles final weekly magazine
│   └── translator.py          # Translates final magazine to user's chosen language
├── content/
│   ├── text_classifier.py     # Regex-based message classification (ARTICLE/TOPIC_SEED/CONTEXT_NOTE)
│   └── url_parser.py          # Fetches & extracts article text (readability-lxml + BS4 fallback; Twitter/X via oEmbed → FxTwitter)
├── db/
│   ├── database.py            # Async SQLite interface with schema (items, pipeline_runs, step_logs, settings)
│   └── models.py              # Dataclasses: Item, Cluster, PipelineRun, StepLog, enums
├── llm/
│   └── provider.py            # LLMProvider protocol, AnthropicProvider, OpenAIProvider; generate() + generate_structured() (tool use), cost estimation
├── pipeline/
│   ├── orchestrator.py        # Runs multi-agent pipeline sequentially
│   ├── scheduler.py           # Weekly digest schedule (default: Sunday 23:00 Europe/Berlin)
│   └── status_updater.py      # Real-time Telegram progress updates
└── telegram/
    └── bot.py                 # DigestBot: commands (/start, /generate, /items, /delete, /setup, /language, /provider, /estimate, /status, /logs, /cost, /week); aliases: /digest→/generate, /lang→/language

prompts/                       # LLM system prompts (one .txt per agent)
├── collector.txt
├── clusterer.txt
├── filter.txt
├── profiler.txt
├── researcher.txt
├── writer.txt
├── editor.txt
├── translator.txt
└── system_prompt.txt          # Legacy prompt (Russian)

tests/                         # pytest suite: MockLLMProvider, 17 tests covering provider, base agent, collector, clusterer, filter, profiler
user_profile.json              # User interests, style prefs, language config (passed to agents)
data/                          # SQLite database storage (gitignored)
```

## Architecture

### Data Flow

1. User sends Telegram message (text, URL, or mixed)
2. `text_classifier.py` classifies it as ARTICLE, TOPIC_SEED, or CONTEXT_NOTE
3. If URL detected: `url_parser.py` fetches and extracts article content (Twitter/X URLs handled via oEmbed API first, with FxTwitter API as fallback; both support Twitter Articles/long-form posts)
4. `CollectorAgent` summarizes and tags the message via LLM
5. Item saved to SQLite `items` table
6. On weekly trigger (or `/generate`): `Orchestrator` runs the pipeline:
   - **Filter** → evaluates items for relevance, removes duplicates and noise (filter types: irrelevant, duplicate, noise, shallow); reports filtered items to user
   - **Clusterer** → groups remaining items into 3-6 topic clusters; unclustered items go into a "quick bites" list
   - **Researcher** → produces research briefs per cluster (fills gaps)
   - **Writer** → writes magazine-quality article per cluster
   - **Editor** → assembles final Markdown document
   - **Translator** → translates to user's language (conditional, skipped for English)
7. `ObsidianWriter` saves output to vault as `YYYY-Www.md`
8. User receives digest file in Telegram

### Agent System

All agents extend `BaseAgent` (in `src/agents/base.py`), which provides:
- Prompt loading from `prompts/` directory
- LLM invocation via `_call_llm()` (free-text) or `_call_llm_structured()` (tool use → typed dict)
- Step logging to database (tokens, cost, duration, errors)
- User profile injection into prompts

**Structured output agents** (Collector, Clusterer, Filter, Profiler) use `_call_llm_structured()` which calls `LLMProvider.generate_structured()` — an Anthropic tool-use / OpenAI function-call that returns a validated dict instead of raw JSON text. Writer, Researcher, Editor, and Translator still use free-text `_call_llm()`.

Agent parameters:

| Agent | Temperature | Max Tokens | Default Model |
|-------|-------------|------------|---------------|
| Collector | 0.3 | 1024 | Sonnet (fast) |
| Profiler | 0.3 | 4096 | Sonnet (fast) |
| Filter | 0.2 | 4096 | Sonnet (fast) |
| Clusterer | 0.3 | 2048 | Sonnet (fast) |
| Researcher | 0.7 | 2048 | Sonnet (fast) |
| Writer | 0.8 | 2048-8192 | Sonnet (quality) |
| Editor | 0.5 | 8192 | Sonnet (quality) |
| Translator | 0.3 | 16384 | Sonnet (fast) |

### Language & Translation

The pipeline runs entirely in English for better LLM reasoning quality. Translation is an optional final step:
- Users select their preferred language via `/language` (alias `/lang`) with inline keyboard buttons
- Preference is persisted in the `settings` table
- If the selected language is not English, the `TranslatorAgent` translates the final magazine as Step 5
- Currently supported: English, Russian

### Database Schema (SQLite)

Four tables in `src/db/database.py`:
- **items** — collected messages (type, raw_content, source_url, extracted_text, summary, tags, language, week_id, status)
- **pipeline_runs** — execution history (week_id, status, token totals, cost)
- **step_logs** — per-agent logs (agent, model, tokens, duration, errors)
- **settings** — key-value store for user preferences (`digest_language`, `user_profile`, `filtering_strictness`, `llm_provider`)

Indexes: `idx_items_week_id`, `idx_items_status`, `idx_step_logs_run_id`

## Configuration

All config is loaded via `src/config.py` from environment variables (`.env` file) and `user_profile.json`.

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `TELEGRAM_USER_ID` | Yes | — | Numeric Telegram user ID (access control) |
| `LLM_PROVIDER` | No | `anthropic` | `"anthropic"` or `"openai"` |
| `ANTHROPIC_API_KEY` | Conditional | — | Required if provider is anthropic |
| `OPENAI_API_KEY` | Conditional | — | Required if provider is openai |
| `OBSIDIAN_VAULT_PATH` | No | `/vault/life/weekly` | Output directory for digest files |
| `DB_PATH` | No | `data/digest.db` | SQLite database path |
| `COLLECTOR_MODEL` | No | per-provider default | Override model for Collector agent |
| `CLUSTERER_MODEL` | No | per-provider default | Override model for Clusterer agent |
| `RESEARCHER_MODEL` | No | per-provider default | Override model for Researcher agent |
| `WRITER_MODEL` | No | per-provider default | Override model for Writer agent |
| `EDITOR_MODEL` | No | per-provider default | Override model for Editor agent |
| `TRANSLATOR_MODEL` | No | per-provider default | Override model for Translator agent |
| `PROFILER_MODEL` | No | per-provider default | Override model for Profiler agent |
| `FILTER_MODEL` | No | per-provider default | Override model for Filter agent |
| `SCHEDULE_ENABLED` | No | `true` | Enable weekly auto-generation |
| `SCHEDULE_DAY` | No | `6` (Sunday) | Day of week (0=Mon, 6=Sun) |
| `SCHEDULE_HOUR` | No | `23` | Hour for scheduled generation |
| `SCHEDULE_MINUTE` | No | `0` | Minute for scheduled generation |
| `SCHEDULE_TIMEZONE` | No | `Europe/Berlin` | Timezone for schedule |

### Default Models

- **Anthropic:** `claude-sonnet-4-5-20250929` for all agents
- **OpenAI:** `gpt-4.1-mini` (fast), `gpt-4.1` (quality)

## Key Conventions

### Code Style
- **Async throughout** — all I/O operations use `async`/`await`
- **Dataclass-based config** — no raw dicts for configuration
- **Protocol-based LLM abstraction** — `LLMProvider` protocol in `src/llm/provider.py`
- **Prompts live in files** — all LLM system prompts in `prompts/*.txt`, not hardcoded
- **User profile is JSON** — edit `user_profile.json`, never hardcode preferences
- **Standard logging** — uses Python `logging` module, not print statements (except startup banner)

### File Conventions
- Obsidian output: `YYYY-Www.md` format with YAML frontmatter
- Database: SQLite with async access via aiosqlite
- All agent outputs are JSON (parsed from LLM responses) except Writer (Markdown), Researcher (plain text), and Translator (Markdown)

### What NOT to Do
- Never commit `.env` files or API keys
- Never commit SQLite database files (`data/*.db`)
- Never commit Obsidian vault content
- Never hardcode user preferences — use `user_profile.json`
- Never hardcode prompts — use `prompts/*.txt` files
- Run tests with `pytest tests/` (17 tests; `MockLLMProvider` in `tests/conftest.py` simulates LLM calls without real API calls)

### Dependencies
When adding dependencies, update `requirements.txt`. Current dependencies:
- `python-telegram-bot[job-queue]==21.7` — Telegram API
- `anthropic>=0.40.0` — Claude API
- `openai>=1.50.0` — OpenAI API
- `httpx>=0.27.0` — Async HTTP client
- `beautifulsoup4>=4.12.0` — HTML parsing
- `readability-lxml>=0.8.1` — Article extraction
- `lxml>=5.0.0` — HTML/XML processing
- `aiosqlite>=0.20.0` — Async SQLite
- `python-dotenv==1.0.1` — .env loading
- `pytest>=8.0.0` — test runner
- `pytest-asyncio>=0.24.0` — async test support

### Docker
- Base image: `python:3.12-slim`
- `PYTHONPATH=/app`
- Volumes: Obsidian vault mount + `./data` for SQLite persistence
- Adjust vault path in `docker-compose.yml` line 10 for your system

## Common Tasks

### Adding a New Agent
1. Create `src/agents/new_agent.py` extending `BaseAgent`
2. Add prompt file `prompts/new_agent.txt`
3. Wire into `Orchestrator` in `src/pipeline/orchestrator.py`
4. Add model override env var in `src/config.py` if needed

### Modifying Agent Behavior
Edit the corresponding prompt file in `prompts/`. Agent code handles I/O and parsing; the prompt defines the LLM's task.

### `/setup` Profile Wizard
Three-step `ConversationHandler` flow in `bot.py`:
1. **Text input** — user describes themselves in free form; `ProfilerAgent` extracts interest areas
2. **Area review** — inline keyboard shows each area with priority (🔴 High / 🟡 Medium / 🟢 Low / ⬜ Off); tapping cycles through priorities
3. **Strictness selection** — user picks Strict / Moderate / Relaxed filtering
Profile saved to `settings` table as JSON. Use `/cancel` to abort at any step.

### Adding a Telegram Command
Add a handler method to `DigestBot` in `src/telegram/bot.py` and register it in the `build()` method.

### Changing the Output Format
Edit `src/obsidian_writer.py` for file naming/frontmatter, or `prompts/editor.txt` for the content structure the Editor agent produces.
