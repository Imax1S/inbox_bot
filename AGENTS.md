# Inbox Agent Bot — Agent Guide

- Purpose: Telegram bot that collects notes/links/ideas during the week and generates Obsidian-ready weekly digests via Claude Sonnet 4 (or OpenAI) using LangChain helpers.
- Runtime: Python 3.11+. Install deps with `pip install -r requirements.txt` or run `docker-compose up`. Recommended start command: `python -m src.main` (uses package imports).
- Secrets/config: copy `.env.example` → `.env` and set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`, `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` with `LLM_PROVIDER=openai`), optional `LLM_MODEL`, and `OBSIDIAN_VAULT_PATH` (default `/vault/life/weekly`). User interests live in `user_profile.json`; edit that instead of hardcoding.
- Key files: `src/main.py` (entry point wiring bot → agent → Obsidian), `src/config.py` (env + profile loader), `src/telegram_client.py` (commands: /start, /status, /digest, /clear; in-memory queue; access controlled by `TELEGRAM_USER_ID`), `src/llm_agent.py` (loads `prompts/system_prompt.txt`, formats weekly prompt, calls provider), `src/obsidian_writer.py` (creates weekly file `YYYY-Www.md` with YAML frontmatter). Prompts live under `prompts/`; Obsidian output path must exist or will be created.
- Flow: messages collected via bot → /digest builds prompt with week range → LLM returns Markdown → writer saves to vault. Queue resets after successful digest; persistence is not implemented yet.
- Conventions: keep Markdown Obsidian-friendly; avoid committing secrets or vault content; if adding deps, update `requirements.txt`. No automated tests; manual run is the current check.
