# 🧠 AI Knowledge Inbox Filter Agent (Inbox Agent Bot)

## 🎯 Project Goal
Create an **intelligent knowledge curator agent** that:
- Accepts unstructured data via Telegram
- Accumulates it throughout the week
- Once a week (on trigger) processes all messages using LLM
- Creates structured Markdown notes in Obsidian Vault

**Core principle:**
> ❗ Better to save something doubtful than delete something useful.

**Personal project goals:**
- Avoid procrastination (don't dive into studying every idea immediately)
- Accumulate ideas/questions/links and process them in batches
- Learn new technologies: LangChain, Anthropic API, Docker, agent systems

---

## 🏗️ Technical Architecture

### Tech Stack
- **Python 3.11+**
- **python-telegram-bot** — receiving messages from Telegram
- **Anthropic API (Claude Sonnet 4)** — processing and structuring knowledge
- **LangChain** — framework for working with LLMs (chains, prompts, parsing)
- **Jina Reader API** — parsing web pages to markdown
- **youtube-transcript-api** — fetching YouTube video transcripts (automatic and manual)
- **Docker** — containerization and environment isolation
- **SQLite** — local DB for processing history
- **JSON** — storing user_profile (interests and priorities)

### Operating Mode
**Trigger-based run (once a week):**
1. Start Docker container: `docker run inbox-agent-bot`
2. Bot connects to Telegram and retrieves all accumulated messages
3. Processes them through Claude Sonnet 4
4. Creates structured notes in Obsidian
5. Completes work and shuts down

**Queue storage:** while bot is offline, messages are stored on Telegram servers

### Paths and Configuration
- **Obsidian Vault:** `/Users/ionko/Documents/my_vault/life/weekly/`
- **Weekly digest:** `YYYY-Www_digest.md` (e.g., `2025-W03_digest.md`)
- **Themed notes:** separate `.md` files in the same folder

---

## 📥 Input Data Format

### Message Types
1. **Simple text notes** — thoughts, ideas, observations
2. **Research queries** — "Porcelain in China", "How does RAG work"
   - Agent should do a small search and create a paragraph of basic knowledge
3. **Article links** — parsed via Jina Reader
4. **YouTube links** — transcripts extracted via youtube-transcript-api (if transcripts exist — analyzed, if not — only link saved with note)
5. **Quotes and thought fragments** — any unstructured text

### Examples
- "Porcelain in China" → agent creates a note with brief topic overview
- https://example.com/article → agent reads article and creates summary
- https://youtube.com/watch?v=xxx → agent extracts transcript or title
- "Idea: make a bot for knowledge filtering" → saved as raw idea

---

## 📤 Output Format (Obsidian)

### Weekly Digest Structure
**File:** `2025-W03_digest.md`

```markdown
# Weekly Digest — 2025 Week 03

## 📊 Statistics
- Total messages: 42
- Notes created: 8
- Research queries: 3
- Links processed: 12

## 🗂️ Topics of the Week

### AI/ML
- [[mcp_protocol_deep_dive]] — Model Context Protocol breakdown
- [[rag_architectures_comparison]] — comparison of RAG approaches

### Mathematics
- [[probability_puzzle_monty_hall]] — Monty Hall paradox
- [[linear_algebra_practical_uses]] — practical applications of linear algebra

### Politics
- [[geopolitics_2025_trends]] — trends in international relations

### Finance
- [[investment_basics_summary]] — basic investment principles

### IT/Architecture
- [[aws_ci_cd_best_practices]] — CI/CD best practices in AWS

### Product/Leadership
- [[startup_team_building_notes]] — notes on team building

## ⚠️ Needs Attention
- [[uncertain_topic_xyz]] #needs_review — not sure about relevance
```

### Themed Note Structure
**File:** `mcp_protocol_deep_dive.md`

```markdown
# MCP Protocol Deep Dive

## 🎯 Quick Summary
Model Context Protocol (MCP) — open protocol for connecting AI assistants to external data sources. Developed by Anthropic.

## 💡 Key Ideas
- Allows Claude to work with files, DBs, APIs without hacks
- Architecture: client-server, JSON-RPC
- Can create custom MCP servers for custom sources

## 🔗 Links and Sources
- [Official MCP Documentation](https://modelcontextprotocol.io)
- [Video: MCP Tutorial](https://youtube.com/watch?v=xxx)
  - Transcript: "MCP simplifies context integration..."

## 🤔 Thoughts and Interpretations
- This solves the "AI can't see my files" problem
- Could potentially connect to Obsidian via MCP
- Worth trying to write a custom MCP server

## ❓ Questions and Doubts
- How safe is it to give AI access to files?
- Are there limits on context size transmitted?

## 🏷️ Tags
#ai #mcp #anthropic #tools #learn #actionable
```

---

## 🧠 Filter System (Agent Logic)

### 1️⃣ Usefulness Filter
**Question:** Does this carry knowledge or knowledge potential?

**Criteria:**
- Is there an idea, insight, fact, hypothesis?
- Related to my interests? (see user_profile.json)
- Could it be useful in the future?

**Decisions:**
- ✅ Keep
- ⚠️ Keep with `#needs_review` marker
- ❌ Delete (only if 100% garbage)

### 2️⃣ Novelty Filter
**Question:** Is this a duplicate or new information?

**Logic:**
- Compare only with current week's data (not with Obsidian history)
- If topic repeats → merge into one note
- If duplicate → mark as `#duplicate`

### 3️⃣ Thought Maturity Filter
**Classification:**
- 🌱 `#raw` — raw idea, thought fragment
- 🌿 `#developing` — developing concept
- 🌳 `#mature` — formed thought

### 4️⃣ Actionability Filter
**Question:** Can this be turned into action?

**Tags:**
- `#actionable` — can do/try
- `#theory` — abstract knowledge
- `#idea` — project/experiment idea
- `#reference` — reference information

### 5️⃣ Confidence Filter (anti-deletion)
**Rule:** If agent is uncertain → DON'T delete, mark it!

**Doubt markers:**
- `⚠️ #needs_review` — requires manual review
- `❓ #uncertain` — unsure about relevance
- `🟡 #low_confidence` — low confidence in categorization

---

## 🏷️ Tagging System

### Content Type
- `#thought` — thought, observation
- `#link` — processed link
- `#quote` — quote
- `#research` — research query
- `#video` — video (YouTube etc.)

### Topics (from user_profile.json)
- `#math` — mathematics
- `#ai` — AI/ML
- `#politics` — politics
- `#finance` — finance
- `#it` — IT, architecture, DevOps
- `#product` — product, startups
- `#leadership` — management, team lead
- `#systems` — systems thinking

### State
- `#raw` / `#developing` / `#mature`
- `#actionable` / `#theory` / `#idea` / `#reference`
- `#needs_review` / `#uncertain` / `#low_confidence`
- `#weekly_digest`

---

## 🎛️ User Profile (user_profile.json)

Interest and priority profile is created separately (not in this bot).

**File structure:**
```json
{
  "interests": {
    "math": {
      "weight": 0.9,
      "keywords": ["mathematics", "problems", "proofs", "algorithms"],
      "priority": "high"
    },
    "ai": {
      "weight": 1.0,
      "keywords": ["AI", "ML", "LLM", "RAG", "MCP", "agents", "Claude", "GPT"],
      "priority": "critical"
    },
    "politics": {
      "weight": 0.85,
      "keywords": ["politics", "geopolitics", "elections", "diplomacy"],
      "priority": "high"
    },
    "finance": {
      "weight": 0.7,
      "keywords": ["finance", "investments", "stocks", "budget"],
      "priority": "medium"
    },
    "it": {
      "weight": 0.95,
      "keywords": ["architecture", "AWS", "CI/CD", "Docker", "Kubernetes"],
      "priority": "high"
    },
    "product": {
      "weight": 0.9,
      "keywords": ["startup", "product", "team", "team lead", "management"],
      "priority": "high"
    }
  },
  "filters": {
    "min_relevance_score": 0.3,
    "auto_reject_below": 0.1,
    "prefer_practical": true,
    "prefer_depth": true
  },
  "blacklist": {
    "topics": ["celebrity gossip", "sports scores", "fashion trends"],
    "keywords": ["clickbait", "top-10", "shock"]
  }
}
```

**Profile creation:** use prompt (see above) in a separate AI chat.

---

## 🔄 Processing Workflow

### Step 1: Message Retrieval
- Connect to Telegram Bot API
- Retrieve all unread messages
- Save to local DB (SQLite)

### Step 2: Preprocessing
- Recognize message type (text, link, YouTube, research query)
- Parse web links via Jina Reader API
- Extract YouTube transcripts via youtube-transcript-api:
  - Attempt to get transcripts (Russian or English)
  - If no transcripts → save URL with `#needs_manual_review` marker
- For research queries: web search or generate basic knowledge via Claude

### Step 3: Processing through Claude
- Form prompt considering user_profile.json
- Send all messages in one batch (30-50 messages per 200k context)
- Apply filters (usefulness, novelty, maturity, actionability, confidence)
- Group by topics
- Create structured notes

### Step 4: Output Generation
- Create Weekly Digest file
- Create separate themed notes
- Save to `/Users/ionko/Documents/my_vault/life/weekly/`

### Step 5: Logging and Completion
- Save processing metrics to DB
- Optionally update user_profile.json (topic usage statistics)
- Complete container work

---

## 🧠 Agent Operating Principles

> **If uncertain — DON'T delete.**

> **If in doubt — mark with a marker.**

> **If you see potential — save and structure.**

Agent is an **editor and curator**, not an archiver:
- Removes noise
- Highlights meaning
- Connects ideas
- Makes cautious decisions
- Marks doubts instead of deleting

---

## 🔮 Future Improvements (v2.0+)

### Automatic Profile Learning
- Track which notes you read/edit
- Automatic adjustment of interest weights
- Hints: "You've been studying this topic for 3 weeks straight"

### Note Linking
- Automatic creation of [[wikilinks]] between related topics
- Maintain knowledge graph
- Detect recurring thought patterns

### Advanced Search
- Vector search across all Obsidian notes (embeddings)
- Novelty filter at knowledge base level, not just weekly

### External Source Integration
- Automatic import from Pocket, Instapaper, Readwise
- RSS feeds for automatic topic tracking
- Integration with research papers (arXiv, Google Scholar)

---

## 📦 Project Structure (Expected)

```
inbox_agent_bot/
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── src/
│   ├── main.py                 # Entry point
│   ├── telegram_client.py      # Telegram message retrieval
│   ├── preprocessor.py         # Link, YouTube, message type parsing
│   ├── llm_agent.py            # Claude work via LangChain
│   ├── obsidian_writer.py      # Markdown file creation
│   ├── database.py             # SQLite for history
│   └── config.py               # Load user_profile.json and env vars
├── config/
│   ├── user_profile.json       # Interest profile
│   └── .env                    # API keys (Telegram, Anthropic, Jina)
├── data/
│   └── history.db              # SQLite DB
├── prompts/
│   ├── system_prompt.txt       # System prompt for Claude
│   └── filters.txt             # Filter descriptions for agent
├── tests/
│   └── ...
├── requirements.txt
└── README.md
```

---

## 🚀 Development Roadmap

### Phase 1: MVP (Minimum Viable Product)
- [ ] Telegram bot receives messages
- [ ] Basic Claude Sonnet 4 integration
- [ ] Simple text processing (without links)
- [ ] Create Weekly Digest + one themed note
- [ ] Docker container for running

### Phase 2: Full Features
- [ ] Web link parsing (Jina Reader)
- [ ] YouTube support (transcripts)
- [ ] Research queries (web search or knowledge generation)
- [ ] Apply all 5 filters
- [ ] Load user_profile.json

### Phase 3: Polish
- [ ] Logging and statistics
- [ ] Error handling and fallback scenarios
- [ ] Tests (unit + integration)
- [ ] User documentation

### Phase 4: Future
- [ ] Automatic profile learning
- [ ] Vector search across Obsidian
- [ ] Web interface for configuration
- [ ] Voice message support

---

## 📚 Useful Resources

- [Anthropic API Docs](https://docs.anthropic.com/)
- [LangChain Documentation](https://python.langchain.com/)
- [python-telegram-bot](https://docs.python-telegram-bot.org/)
- [Jina Reader API](https://jina.ai/reader/)
- [youtube-transcript-api (GitHub)](https://github.com/jdepoix/youtube-transcript-api)
- [Obsidian Markdown Spec](https://help.obsidian.md/Editing+and+formatting/Basic+formatting+syntax)

---

## 🎯 Project Success = Learn New Technologies + Get Working Tool

This project is not just a filter, but a **second brain-editor** that:
- Reduces cognitive load
- Respects uncertainty
- Helps turn chaos into an internal knowledge system
- Allows me to learn LangChain, Claude API, agent systems in practice
