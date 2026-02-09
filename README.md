# Inbox Agent Bot

A Telegram bot that collects notes, links, and ideas throughout the week, then runs them through a multi-agent LLM pipeline to produce a structured weekly digest for Obsidian.

**Core principle:** Better to save something doubtful than delete something useful.

## Architecture

```
Telegram ──> Classify & Collect ──> SQLite
                                      │
                          weekly trigger / /generate
                                      │
                                      v
                               ┌─────────────┐
                               │  Clusterer   │  Group items into 3-6 topics
                               └──────┬──────┘
                                      v
                               ┌─────────────┐
                               │  Researcher  │  Research brief per cluster
                               └──────┬──────┘
                                      v
                               ┌─────────────┐
                               │   Writer     │  Article per cluster
                               └──────┬──────┘
                                      v
                               ┌─────────────┐
                               │   Editor     │  Assemble final digest
                               └──────┬──────┘
                                      v
                              Obsidian (YYYY-Www.md)
```

Every agent extends `BaseAgent`, which handles prompt loading from `prompts/`, LLM calls, cost tracking, and step logging. Agents that need speed use Sonnet; agents that need quality use Opus.

## Quick Start

```bash
cp .env.example .env
# Fill in: TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID, ANTHROPIC_API_KEY

# Docker (recommended)
docker-compose up

# Or directly
pip install -r requirements.txt
python -m src.main
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Introduction |
| `/generate` | Run the digest pipeline now |
| `/items` | List collected items |
| `/delete` | Remove an item |
| `/status` | Pipeline status |
| `/logs` | Agent step logs |
| `/cost` | Token usage and cost |
| `/week` | Current week info |

## Tech Stack

- **Python 3.11+** (3.12 in Docker)
- **Telegram Bot API** via python-telegram-bot
- **Anthropic Claude / OpenAI** via protocol-based LLM abstraction
- **SQLite** via aiosqlite
- **readability-lxml + BeautifulSoup** for article extraction
- **Docker** for deployment

## Configuration

Set LLM provider, models, schedule, and vault path via environment variables. Customize interests and writing style in `user_profile.json`. See `.env.example` for all options.

## Project Structure

```
src/
├── main.py              # Entry point
├── config.py            # Config from .env + user_profile.json
├── obsidian_writer.py   # Writes digest to vault
├── agents/              # BaseAgent + Collector, Clusterer, Researcher, Writer, Editor
├── content/             # Text classification & URL parsing
├── db/                  # Async SQLite (items, pipeline_runs, step_logs)
├── llm/                 # LLMProvider protocol (Anthropic, OpenAI)
├── pipeline/            # Orchestrator, scheduler, status updates
└── telegram/            # Bot commands & handlers

prompts/                 # One .txt file per agent (edit these to change behavior)
user_profile.json        # Your interests, language, style preferences
```

## License

Private project.
