"""Seedable demo content for local bot testing without API spend."""

from datetime import datetime
from uuid import uuid4

from .db.models import Item, ItemStatus, ItemType


def build_demo_items(week_id: str) -> list[Item]:
    """Build a small deterministic set of demo items for the current week."""
    now = datetime.now()

    demo_specs = [
        {
            "type": ItemType.ARTICLE,
            "summary": "Agentic coding tools are moving from autocomplete to end-to-end workflow execution.",
            "tags": ["ai", "llm", "developer-tools", "automation"],
            "source_url": "https://demo.local/agentic-coding",
            "raw_content": (
                "[DEMO] Article: agentic coding tools now handle repo exploration, "
                "editing, test runs, and iterative fixes instead of only code completion."
            ),
            "extracted_text": (
                "A new wave of coding assistants is focused on task completion rather "
                "than single-line suggestions. The practical shift is from autocomplete "
                "toward workflow automation: reading a codebase, planning a change, "
                "editing files, running tests, and revising the patch until it passes."
            ),
        },
        {
            "type": ItemType.ARTICLE,
            "summary": "Selective CI and smaller test scopes are becoming a major engineering productivity lever.",
            "tags": ["engineering", "ci", "system-design", "productivity"],
            "source_url": "https://demo.local/selective-ci",
            "raw_content": (
                "[DEMO] Article: teams are replacing full-repo CI runs with change-aware "
                "pipelines that execute only affected tests and build targets."
            ),
            "extracted_text": (
                "Engineering teams are attacking CI latency with more granular build graphs, "
                "selective test execution, and tighter ownership boundaries. The payoff is "
                "not just faster merges, but better developer focus because feedback arrives "
                "before context is lost."
            ),
        },
        {
            "type": ItemType.ARTICLE,
            "summary": "Passkeys are improving login conversion, but rollout quality still depends on fallback design.",
            "tags": ["security", "authentication", "product", "ux"],
            "source_url": "https://demo.local/passkeys",
            "raw_content": (
                "[DEMO] Article: passkey adoption is rising, but weak fallback flows can "
                "erase the gains by confusing users during device switching and recovery."
            ),
            "extracted_text": (
                "Passkeys reduce phishing risk and can materially improve sign-in completion, "
                "but product teams still need careful recovery and cross-device flows. "
                "Bad fallback design turns a security upgrade into a support burden."
            ),
        },
        {
            "type": ItemType.TOPIC_SEED,
            "summary": "How should a weekly digest balance depth, novelty, and practical next steps?",
            "tags": ["knowledge-management", "writing", "productivity"],
            "source_url": None,
            "raw_content": (
                "[DEMO] Topic seed: what makes a weekly digest genuinely useful instead "
                "of just a better-organized reading backlog?"
            ),
            "extracted_text": None,
        },
        {
            "type": ItemType.CONTEXT_NOTE,
            "summary": "Prefer concise takeaways, explicit trade-offs, and action items over trend summaries.",
            "tags": ["preferences", "editing", "analysis"],
            "source_url": None,
            "raw_content": (
                "[DEMO] Context note: prioritize practical implications, trade-offs, "
                "and what to do next. Avoid generic hype or broad future-of-X framing."
            ),
            "extracted_text": None,
        },
    ]

    items: list[Item] = []
    for spec in demo_specs:
        items.append(
            Item(
                id=str(uuid4()),
                created_at=now,
                type=spec["type"],
                raw_content=spec["raw_content"],
                source_url=spec["source_url"],
                extracted_text=spec["extracted_text"],
                summary=spec["summary"],
                tags=spec["tags"],
                language="en",
                week_id=week_id,
                status=ItemStatus.COLLECTED,
            )
        )

    return items
