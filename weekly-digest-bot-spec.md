# Weekly Digest Bot — Technical Specification

## Project Overview

Build a Telegram bot that acts as a personal content curator. Throughout the week, the user sends articles (URLs), questions, topics, and random thoughts. On Sunday night, the system processes everything and generates a **polished weekly magazine** in Markdown format — 15–20 minutes of reading, with each topic deeply researched and engagingly written.

The system must merge overlapping topics, enrich thin ones with additional context, and produce a final output that reads like a high-quality editorial publication, not a summary dump.

---

## Core Requirements

1. **Input collection** — accept URLs, plain text questions, topic seeds, and notes via Telegram throughout the week
2. **Smart clustering** — group related items, merge overlapping topics, deduplicate
3. **Deep enrichment** — for each cluster, research additional context so coverage is substantive, not shallow
4. **Editorial writing** — each topic is written as an engaging article with narrative structure
5. **Time-budgeted output** — total read time ~15–20 min, balanced across topics
6. **Status updates** — the user sees real-time progress of the generation pipeline in Telegram
7. **Logging** — structured logs for every pipeline stage for debugging and observability

---

## Architecture: Multi-Agent Pipeline

The system uses 5 specialized agents orchestrated by a deterministic Kotlin pipeline. Each "agent" is a function call to an LLM with a specific system prompt, not a separate service.

```
User (Telegram)
    │
    ▼
┌─────────────────┐
│ Collector Agent  │  ← runs on every incoming message
└────────┬────────┘
         │ saves to DB
         ▼
    ┌─────────┐
    │   DB    │  ← items accumulate during the week
    └────┬────┘
         │  Sunday night trigger (or manual /generate command)
         ▼
┌─────────────────┐
│  Orchestrator   │  ← deterministic Kotlin code, NOT an LLM
└────────┬────────┘
         │ sequential pipeline with status updates
         ▼
   ┌─────────────┐     ┌──────────────┐     ┌──────────────┐
   │  Clusterer   │ ──▶ │  Researcher  │ ──▶ │    Writer    │
   │   Agent      │     │    Agent     │     │    Agent     │
   └─────────────┘     └──────────────┘     └──────────────┘
                                                    │
                                                    ▼
                                            ┌──────────────┐
                                            │    Editor    │
                                            │    Agent     │
                                            └──────┬───────┘
                                                   │
                                                   ▼
                                            Markdown Journal
                                            (sent to user via Telegram)
```

---

## Agent Specifications

### Agent 1: Collector

**Trigger:** every incoming Telegram message during the week.

**Responsibility:** classify the input, extract content, and store a structured item.

**Input types to handle:**

| User sends | Action |
|---|---|
| URL/link | Fetch the page, extract article text (use Readability-like parser or LLM extraction), generate summary and tags |
| Plain text question | Store as `topic_seed` — a question or topic the user wants explored |
| Short note / thought | Store as `context_note` — supplementary context that may attach to a nearby topic |
| Multiple items in one message | Split and process each individually |

**System prompt guidance:**
- Extract 3–7 semantic tags per item for clustering
- Generate a 2–3 sentence summary
- Detect language of the content
- If URL fails to parse, store the raw URL and flag for manual review

**DB schema per item:**

```
Item {
    id: UUID
    created_at: Timestamp
    type: enum [ARTICLE, TOPIC_SEED, CONTEXT_NOTE]
    raw_content: String          // original user message
    source_url: String?          // if URL was provided
    extracted_text: String?      // parsed article body (can be large)
    summary: String              // LLM-generated 2-3 sentence summary
    tags: List<String>           // semantic tags for clustering
    language: String             // "en", "ru", etc.
    week_id: String              // e.g. "2026-W06"
    status: enum [COLLECTED, CLUSTERED, PUBLISHED]
}
```

**User feedback:** after processing, send a short confirmation:
```
✅ Saved: "Article about EU AI Act amendments"
Tags: #eu #ai-regulation #policy
```

---

### Agent 2: Clusterer

**Trigger:** first step of the Sunday generation pipeline.

**Input:** all items with `status = COLLECTED` for the current `week_id`.

**Responsibility:** group items into coherent topics. Merge overlapping ones. Assign priority.

**System prompt guidance:**
- Receive all items (summaries + tags) as context
- Group into 3–6 clusters (targeting 15–20 min total read time, ~3–5 min per cluster)
- Each cluster gets: a working title, list of item IDs, a brief editorial angle (what's interesting about this topic)
- If two items are about the same thing from different angles — merge into one richer cluster
- If an item doesn't fit any cluster and is too thin to stand alone — place it in a "Quick Bites" miscellaneous section
- Output: structured JSON

**Output format:**

```json
{
  "clusters": [
    {
      "id": "cluster-1",
      "title": "EU's New Approach to AI Regulation",
      "editorial_angle": "Three different sources this week painted contrasting pictures of how the EU AI Act will actually be enforced",
      "item_ids": ["uuid-1", "uuid-3", "uuid-7"],
      "estimated_read_minutes": 5,
      "priority": 1
    }
  ],
  "quick_bites_item_ids": ["uuid-5"]
}
```

---

### Agent 3: Researcher

**Trigger:** after Clusterer completes, runs once per cluster.

**Input:** a single cluster with all its items' full extracted content.

**Responsibility:** fill gaps. If the user sent 2 articles on a topic, the Researcher identifies what's missing for a complete picture and generates supplementary context using its own knowledge (or web search if available).

**System prompt guidance:**
- You are given N source materials on a topic
- Identify gaps: missing context, counterarguments, historical background, recent developments
- Produce a research brief (500–1000 words) that the Writer can use alongside the original sources
- Do NOT write the final article — just prepare the material
- Include specific facts, dates, names — the Writer needs concrete details, not vague framing

**Output:** a research brief as structured text, attached to the cluster.

---

### Agent 4: Writer

**Trigger:** after Researcher completes for a cluster.

**Input:** cluster metadata + original items' content + research brief.

**Responsibility:** write one engaging article per cluster.

**System prompt guidance:**
- Write in the style of a high-quality tech/policy magazine (think Ars Technica, Meduza long-reads, or The Verge features)
- Structure: hook → context → deep dive → implications → takeaway
- Use the user's original sources as primary material, enriched by the research brief
- Match the language to the dominant language of the source materials (if mostly Russian sources — write in Russian, if English — in English; if mixed — default to the user's preference or Russian)
- Target read time is provided per cluster — write accordingly (~250 words per minute of reading)
- Include inline references to original sources as Markdown links
- NO dry summaries. This should be interesting to read.

**Output:** a single Markdown article for the cluster.

---

### Agent 5: Editor

**Trigger:** after all Writer outputs are ready.

**Input:** all written articles + quick bites items + metadata.

**Responsibility:** assemble the final magazine. Ensure quality, consistency, and time budget.

**System prompt guidance:**
- Assemble articles into a single Markdown document
- Write a brief editorial intro (2–3 sentences about what's in this issue)
- Generate a table of contents with anchor links
- Add a "Quick Bites" section for unclustered items (1–2 sentences each)
- Calculate total estimated read time — if over 20 min, flag which article to trim; if under 15, flag which to expand
- Add a "Sources" appendix at the end with all original URLs
- Ensure consistent heading levels, formatting, tone
- Add issue metadata (week number, date range, number of items processed)

**Output format:**

```markdown
# 📖 Weekly Digest — Feb 2–8, 2026

> Issue #6 · 5 topics · ~18 min read
> Generated from 12 items collected this week

## In This Issue
1. [EU's New Approach to AI Regulation](#eu-ai-regulation)
2. [Why Kotlin Multiplatform Is Winning](#kotlin-multiplatform)
3. ...

---

## EU's New Approach to AI Regulation {#eu-ai-regulation}
*~5 min read · Sources: [link1], [link2], [link3]*

<article body>

---

## Quick Bites
- **Topic X**: one-liner summary. [Source](url)
- **Topic Y**: one-liner summary.

---

## All Sources
- [Original title](url) — collected Mon, Feb 3
- ...
```

---

## Orchestrator (Kotlin, NOT an LLM)

The orchestrator is deterministic code that runs the pipeline and manages state.

```kotlin
class DigestOrchestrator(
    private val db: DigestDatabase,
    private val agents: AgentRegistry,
    private val telegram: TelegramNotifier,
    private val logger: PipelineLogger
) {
    suspend fun generateWeeklyDigest(weekId: String) {
        val items = db.getItemsByWeek(weekId)
        if (items.isEmpty()) {
            telegram.notify("No items collected this week. Skipping digest generation.")
            return
        }

        telegram.notify("🚀 Starting digest generation for $weekId\n${items.size} items to process")
        logger.start(weekId)

        // Step 1: Cluster
        telegram.notify("📊 Step 1/4: Clustering ${items.size} items...")
        val clusters = agents.clusterer.run(items)
        logger.log("clusterer", "Formed ${clusters.size} clusters")
        telegram.notify("✅ Formed ${clusters.size} topic clusters")

        // Step 2: Research (parallel per cluster)
        telegram.notify("🔍 Step 2/4: Researching ${clusters.size} topics...")
        val researchBriefs = clusters.mapIndexed { i, cluster ->
            telegram.notify("  🔬 Researching (${i+1}/${clusters.size}): ${cluster.title}")
            val brief = agents.researcher.run(cluster, items)
            logger.log("researcher", "Completed research for: ${cluster.title}")
            cluster to brief
        }
        telegram.notify("✅ Research complete")

        // Step 3: Write (parallel per cluster)
        telegram.notify("✍️ Step 3/4: Writing articles...")
        val articles = researchBriefs.mapIndexed { i, (cluster, brief) ->
            telegram.notify("  📝 Writing (${i+1}/${clusters.size}): ${cluster.title}")
            val article = agents.writer.run(cluster, brief, items)
            logger.log("writer", "Wrote ${article.wordCount} words for: ${cluster.title}")
            article
        }
        telegram.notify("✅ All articles written")

        // Step 4: Edit & assemble
        telegram.notify("📰 Step 4/4: Assembling final magazine...")
        val magazine = agents.editor.run(articles, clusters, items, weekId)
        logger.log("editor", "Final magazine: ${magazine.estimatedReadMinutes} min read")

        // Deliver
        db.markItemsPublished(items.map { it.id })
        telegram.sendMarkdownDocument(magazine.content, "digest-$weekId.md")
        telegram.notify("✅ Your weekly digest is ready! ~${magazine.estimatedReadMinutes} min read")

        logger.finish(weekId)
    }
}
```

---

## Status Updates & Logging

### Telegram Status Updates

The user must see progress **in real time** as the pipeline runs. Use Telegram message editing to avoid spam:

**Approach: Single status message, continuously edited.**

```
📰 Generating Weekly Digest (Week 6)...

▰▰▰▰▱▱▱▱ Step 2/4: Researching

✅ Clustering — 4 topics formed
🔍 Researching (2/4): "Kotlin Multiplatform..."
⬜ Writing
⬜ Assembling
```

Implementation:
1. Send one status message at pipeline start — save the `message_id`
2. On each state change, call `editMessageText` with updated content
3. Use progress indicators: ✅ done, 🔄 in progress, ⬜ pending
4. On completion, send the final document as a separate file message

Fallback: if `editMessageText` fails (Telegram rate limit), send a new message instead of editing.

### Structured Logging

Every pipeline run must produce a structured log for debugging.

```kotlin
data class PipelineLog(
    val weekId: String,
    val startedAt: Instant,
    val finishedAt: Instant?,
    val status: PipelineStatus,  // RUNNING, COMPLETED, FAILED
    val steps: List<StepLog>
)

data class StepLog(
    val agent: String,           // "collector", "clusterer", etc.
    val startedAt: Instant,
    val finishedAt: Instant?,
    val status: StepStatus,
    val inputTokens: Int,
    val outputTokens: Int,
    val llmModel: String,
    val details: String,         // free-form details
    val error: String?           // null if success
)
```

Store logs in DB. Expose via Telegram command:

- `/status` — show current pipeline status (or last run summary)
- `/logs` — send last run's log as a text file
- `/cost` — show token usage and estimated API cost for last run

---

## Telegram Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message + usage instructions |
| `/generate` | Manually trigger digest generation (don't wait for Sunday) |
| `/items` | List items collected this week (title + type + tags) |
| `/delete <id>` | Remove an item from this week's collection |
| `/status` | Current pipeline status or last run summary |
| `/logs` | Get detailed log of last generation run |
| `/cost` | Token usage and estimated cost of last run |
| `/settings` | Configure: target read time, language preference, generation schedule |
| `/week` | Show current week ID and items count |

---

## Technical Stack

| Component | Technology | Notes |
|---|---|---|
| Language | **Kotlin** | Entire project |
| Telegram SDK | **tgbotapi** (by InsanusMokrassar) or **kotlin-telegram-bot** | Coroutine-native preferred |
| LLM API | **Anthropic Claude API** | Sonnet for Collector/Clusterer (fast, cheap). Opus for Writer/Editor (quality) |
| HTTP client | **Ktor Client** | For URL fetching, API calls |
| Article parsing | **Readability** (Mozilla) via Ktor + JSoup, or **Trafilatura** via subprocess | Extract article body from URLs |
| Database | **Exposed (JetBrains ORM) + PostgreSQL** or **SQLite** for simplicity | SQLite is fine for single-user bot |
| Scheduling | **kotlinx-coroutines** + simple cron-like check | Or Quartz if you want robustness |
| Deployment | **VPS** / **Railway** / **Docker on personal server** | Needs to run 24/7 for Telegram webhook |

---

## Agent Infrastructure

Each agent is a Kotlin function, not a separate service:

```kotlin
data class AgentConfig(
    val name: String,
    val systemPrompt: String,
    val model: String,          // "claude-sonnet-4-5-20250929" or "claude-opus-4-6"
    val temperature: Double,
    val maxTokens: Int
)

suspend fun runAgent(
    config: AgentConfig,
    userMessage: String,
    context: List<Message> = emptyList()
): String {
    // Call Claude API with config.systemPrompt as system, userMessage as user
    // Return the text response
    // Log tokens used
}
```

**Model selection per agent:**

| Agent | Model | Reasoning |
|---|---|---|
| Collector | Sonnet | Fast, runs on every message. Needs to be cheap |
| Clusterer | Sonnet | Analytical but straightforward |
| Researcher | Sonnet or Opus | Depends on depth needed. Start with Sonnet |
| Writer | Opus | Quality is critical — this is the product |
| Editor | Opus | Final quality gate |

---

## Article Parsing Strategy

When the user sends a URL:

1. **Fetch HTML** via Ktor HttpClient with reasonable User-Agent
2. **Extract article body** — priorities:
   - Try Mozilla Readability (via node subprocess or Kotlin port)
   - Fallback: JSoup with heuristic extraction (largest text block)
   - Fallback: send raw HTML to LLM and ask it to extract the article
3. **Handle edge cases:**
   - Paywalled content → save URL + whatever preview is available, flag as `partial`
   - Video/podcast links → save URL + title, mark as `media_link`
   - PDF links → extract text via Apache PDFBox
   - Twitter/X threads → extract thread text
4. **Limit extracted text** to ~5000 tokens to avoid blowing context windows in later stages

---

## Weekly Cycle

```
Monday–Saturday:
  User sends items → Collector Agent processes → DB stores

Sunday 23:00 (configurable):
  Orchestrator triggers → full pipeline runs → magazine delivered

Alternative:
  User sends /generate at any time → pipeline runs with current week's items
```

---

## Error Handling

- **LLM API failure:** retry 3 times with exponential backoff. If all fail, notify user and save partial state so pipeline can resume
- **URL fetch failure:** save the URL anyway, mark as `fetch_failed`, include in digest as "User shared a link to: <title if available>"
- **Pipeline crash mid-run:** save all completed steps. On retry, skip completed steps (idempotent design)
- **Empty week:** don't generate. Send a friendly message instead
- **Token budget exceeded:** if a single cluster is too large for context window, split it or summarize inputs before passing to Writer

---

## Future Enhancements (Out of Scope for v1)

- Rating/feedback on articles (👍👎 in Telegram) to improve future generation
- Persistent user preferences learned over time (topics of interest, preferred depth)
- Multi-user support
- Web UI for browsing past digests
- Audio version (TTS) of the digest
- Mid-week preview ("Here's what I've collected so far")
