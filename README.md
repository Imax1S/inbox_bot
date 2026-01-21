# 🧠 Inbox Agent Bot

**AI-powered knowledge filter and curator**

## The Idea

A Telegram bot that collects your notes, links, and ideas throughout the week, then processes them through Claude Sonnet 4 to create structured Markdown notes in Obsidian.

**How it works:**
- Send the bot any thoughts, links, YouTube videos, or questions
- Run processing once a week
- Get a Weekly Digest + themed notes in Obsidian

**Core principle:** Better to save something doubtful than delete something useful.

## Features

- ✅ Processes text notes and ideas
- ✅ Parses web articles via Jina Reader
- ✅ Extracts YouTube transcripts
- ✅ Filters by your interest profile (user_profile.json)
- ✅ Groups by topics and creates structured notes
- ✅ Adds tags and markers for further processing

## Tech Stack

- Python 3.11+
- Telegram Bot API (python-telegram-bot)
- Anthropic Claude Sonnet 4
- LangChain
- Jina Reader API
- youtube-transcript-api
- Docker

## Quick Start

```bash
# Set up environment variables in .env
# TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, JINA_API_KEY

# Run with Docker
docker-compose up

# Or run directly
pip install -r requirements.txt
python src/main.py
```

## Output Structure

Creates in Obsidian:
- **Weekly Digest** — weekly summary with statistics and note list
- **Themed notes** — separate files with ideas, article summaries, research

## Details

See [idea.md](idea.md) for full architecture description, filters, and roadmap.
