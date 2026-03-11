# Inbox Agent Bot

> Tired of a "watch later" list you open once a year?

You save links, jot down ideas, and forward articles all week — then forget about them.
**Inbox Agent Bot** is a personal AI that lives in Telegram and turns that noise into signal.

Send it anything throughout the week: articles, URLs, random thoughts, topics you're curious about.
You configure your interests via `/setup`, it filters out the noise, and every Sunday automatically delivers a **polished weekly digest** — grouped by theme, written like a magazine, saved straight to your Obsidian vault. You can also trigger it on demand with `/generate`.

**The idea:** a *thought mapper* that knows what you care about, cuts through the clutter, and hands you a curated weekly read instead of an ever-growing backlog.

---

## How it works

```
You send stuff all week          Sunday night (or /generate)
───────────────────────          ───────────────────────────

📄 Article URL       ──┐         Filter  →  remove noise & duplicates
💡 Topic / idea      ──┤──> DB   Cluster →  group into 3-6 themes
📝 Random note       ──┘         Research→  fill knowledge gaps
                                 Write   →  magazine-quality articles
                                 Edit    →  assemble final digest
                                 Translate → your language (optional)
                                      │
                                      ▼
                              📖 YYYY-Www.md in Obsidian
```

Every agent is LLM-powered and personalised to your interests (set up via `/setup`). The pipeline runs in English for quality, with an optional translation step at the end.

---

## Quick Start

```bash
cp .env.example .env
# Fill in: TELEGRAM_BOT_TOKEN, TELEGRAM_USER_IDS, ANTHROPIC_API_KEY

# Docker (recommended)
docker-compose up

# Or directly
pip install -r requirements.txt
python -m src.main
```

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Introduction |
| `/generate` | Run the digest pipeline now (alias: `/digest`) |
| `/items` | List collected items |
| `/delete` | Remove an item |
| `/setup` | Configure your interest profile (multi-step wizard) |
| `/language` | Choose digest language — RU/EN (alias: `/lang`) |
| `/status` | Pipeline status |
| `/logs` | Agent step logs |
| `/cost` | Token usage and cost |
| `/estimate` | Predict generation cost before running |
| `/provider` | Switch LLM provider (Anthropic/OpenAI) |
| `/week` | Current week info |

---

## Tech Stack

- **Python 3.11+** (3.12 in Docker)
- **Telegram Bot API** via python-telegram-bot
- **Anthropic Claude / OpenAI** via protocol-based LLM abstraction with structured output (tool use)
- **SQLite** via aiosqlite
- **readability-lxml + BeautifulSoup** for article extraction
- **Docker** for deployment
- **pytest + pytest-asyncio** for testing

## Configuration

Set LLM provider, models, schedule, and vault path via environment variables. Customize interests and writing style in `user_profile.json`. See `.env.example` for all options.

## Project Structure

```
src/
├── main.py              # Entry point
├── config.py            # Config from .env + user_profile.json
├── obsidian_writer.py   # Writes digest to vault
├── agents/              # BaseAgent + Collector, Filter, Clusterer, Researcher, Writer, Editor, Translator, Profiler
├── content/             # Text classification & URL parsing (incl. Twitter/X)
├── db/                  # Async SQLite (items, pipeline_runs, step_logs, settings)
├── llm/                 # LLMProvider protocol (Anthropic, OpenAI) with structured output
├── pipeline/            # Orchestrator, scheduler, status updates
└── telegram/            # Bot commands & handlers

prompts/                 # One .txt file per agent (edit these to change behavior)
tests/                   # pytest suite — MockLLMProvider, 17 tests across agents & provider
user_profile.json        # Your interests, language, style preferences
```

## License

Private project.
