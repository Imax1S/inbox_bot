"""Digest pipeline orchestrator — deterministic code that runs the multi-agent pipeline."""

import logging
from datetime import datetime
from uuid import uuid4

from ..agents.clusterer import ClustererAgent
from ..agents.editor import EditorAgent
from ..agents.filter import FilterAgent
from ..agents.researcher import ResearcherAgent
from ..agents.translator import TranslatorAgent
from ..agents.writer import WriterAgent
from ..db.database import Database
from ..db.models import ItemStatus, PipelineRun, PipelineStatus
from ..llm.provider import estimate_cost
from ..obsidian_writer import ObsidianWriter
from .status_updater import StatusUpdater

logger = logging.getLogger(__name__)

LANGUAGE_NAMES = {
    "ru": "Russian",
    "en": "English",
}


class Orchestrator:
    def __init__(
        self,
        db: Database,
        clusterer: ClustererAgent,
        researcher: ResearcherAgent,
        writer: WriterAgent,
        editor: EditorAgent,
        translator: TranslatorAgent,
        obsidian_writer: ObsidianWriter,
        filter_agent: FilterAgent | None = None,
        dry_run: bool = False,
    ):
        self.db = db
        self.clusterer = clusterer
        self.researcher = researcher
        self.writer = writer
        self.editor = editor
        self.translator = translator
        self.obsidian_writer = obsidian_writer
        self.filter_agent = filter_agent
        self.dry_run = dry_run

    async def run(
        self,
        week_id: str,
        status_updater: StatusUpdater | None = None,
    ) -> str | None:
        """Run the full digest pipeline.

        Returns the path to the saved file, or None if no items.
        """
        items = await self.db.get_items_by_week(week_id, status=ItemStatus.COLLECTED)
        if not items:
            logger.info("No items for %s — skipping", week_id)
            return None

        run_id = str(uuid4())
        run = PipelineRun(
            id=run_id,
            week_id=week_id,
            started_at=datetime.now(),
            finished_at=None,
            status=PipelineStatus.RUNNING,
        )
        await self.db.save_pipeline_run(run)

        total_input = 0
        total_output = 0

        # Read user's language preference (default: English)
        digest_language = await self.db.get_setting("digest_language", "en")
        needs_translation = digest_language != "en"
        lang_name = LANGUAGE_NAMES.get(digest_language, digest_language)
        logger.info("Digest language: %s (translation needed: %s)", lang_name, needs_translation)

        if status_updater:
            await status_updater.start(week_id, len(items), needs_translation=needs_translation)

        # Track filtered items for user notification
        filter_report: list[dict] = []

        try:
            # ── Step 0: Filter (if filter agent is configured) ──
            if self.filter_agent:
                if status_updater:
                    await status_updater.update(0, f"Filtering {len(items)} items...")
                logger.info("Filtering %d items for %s", len(items), week_id)

                filter_result = await self.filter_agent.process(items, run_id=run_id)

                if filter_result.filtered_items:
                    # Build report for user notification
                    for fi in filter_result.filtered_items:
                        filtered_item = next(
                            (item for item in items if item.id == fi.id), None
                        )
                        filter_report.append({
                            "summary": filtered_item.summary[:80] if filtered_item else fi.id[:8],
                            "reason": fi.reason,
                            "type": fi.filter_type,
                            "score": fi.relevance_score,
                        })

                    # Keep only items that passed the filter
                    kept_ids = set(filter_result.kept_item_ids)
                    items = [item for item in items if item.id in kept_ids]

                    logger.info(
                        "Filter: kept %d items, filtered %d items",
                        len(items),
                        len(filter_result.filtered_items),
                    )

                if not items:
                    logger.info("All items filtered out for %s", week_id)
                    if status_updater:
                        await status_updater.fail(
                            "All items were filtered as irrelevant. "
                            "Try adjusting your profile (/setup) or adding more content."
                        )
                    await self.db.update_pipeline_run(
                        run_id, PipelineStatus.COMPLETED,
                    )
                    return None

            # ── Step 1: Cluster ──
            if status_updater:
                await status_updater.update(1, f"Clustering {len(items)} items...")
            logger.info("Clustering %d items for %s", len(items), week_id)

            cluster_result = await self.clusterer.process(items, run_id=run_id)
            logger.info(
                "Formed %d clusters + %d quick bites",
                len(cluster_result.clusters),
                len(cluster_result.quick_bites_item_ids),
            )

            # ── Step 2: Research ──
            briefs: dict[str, str] = {}
            for i, cluster in enumerate(cluster_result.clusters):
                if status_updater:
                    await status_updater.update(
                        2,
                        f"Researching ({i + 1}/{len(cluster_result.clusters)}): "
                        f"{cluster.title}",
                    )
                logger.info("Researching: %s", cluster.title)

                cluster_items = [
                    item for item in items if item.id in cluster.item_ids
                ]
                briefs[cluster.id] = await self.researcher.process(
                    cluster, cluster_items, run_id=run_id
                )

            # ── Step 3: Write ──
            articles: dict[str, str] = {}
            for i, cluster in enumerate(cluster_result.clusters):
                if status_updater:
                    await status_updater.update(
                        3,
                        f"Writing ({i + 1}/{len(cluster_result.clusters)}): "
                        f"{cluster.title}",
                    )
                logger.info("Writing: %s", cluster.title)

                cluster_items = [
                    item for item in items if item.id in cluster.item_ids
                ]
                articles[cluster.id] = await self.writer.process(
                    cluster,
                    cluster_items,
                    briefs[cluster.id],
                    run_id=run_id,
                )

            # ── Step 4: Edit & Assemble ──
            if status_updater:
                await status_updater.update(4, "Assembling final magazine...")
            logger.info("Assembling magazine for %s", week_id)

            quick_bites_items = [
                item
                for item in items
                if item.id in cluster_result.quick_bites_item_ids
            ]

            magazine = await self.editor.process(
                articles=articles,
                cluster_result=cluster_result,
                quick_bites_items=quick_bites_items,
                all_items=items,
                week_id=week_id,
                run_id=run_id,
            )

            # ── Step 5: Translate (if needed) ──
            if needs_translation:
                if status_updater:
                    await status_updater.update(5, f"Translating to {lang_name}...")
                logger.info("Translating magazine to %s", lang_name)

                magazine = await self.translator.process(
                    magazine=magazine,
                    target_language=lang_name,
                    run_id=run_id,
                )

            # Resolve drop_below once for both file section and Telegram report
            drop_below = 0.25
            if self.filter_agent:
                from ..profile_defaults import get_scoring_thresholds
                strictness = self.filter_agent.user_profile.get("strictness",
                              self.filter_agent.user_profile.get("filtering_strictness", "moderate"))
                drop_below = get_scoring_thresholds(strictness)["drop_below"]

            # ── Append filtered-items section (if any) ──
            if filter_report:
                type_icon = {
                    "irrelevant": "🚫",
                    "duplicate": "🔄",
                    "noise": "🗑",
                    "shallow": "📉",
                }
                rows = []
                for entry in filter_report:
                    score = entry.get("score", 0.0)
                    gap = drop_below - score
                    gap_str = f"−{gap:.2f}" if gap > 0 else f"+{abs(gap):.2f}"
                    icon = type_icon.get(entry["type"], "❌")
                    rows.append(
                        f"| {entry['summary']} "
                        f"| {score:.2f} "
                        f"| {gap_str} "
                        f"| {icon} {entry['type']} "
                        f"| {entry['reason']} |"
                    )
                filtered_section = (
                    "\n\n---\n\n"
                    f"## 🚫 Отфильтровано ({len(filter_report)})\n\n"
                    f"*drop\\_below = {drop_below:.2f}*\n\n"
                    "| Материал | Скор | До порога | Тип | Причина |\n"
                    "|---|---|---|---|---|\n"
                    + "\n".join(rows)
                )
                magazine += filtered_section

            # ── Save & Finalize ──
            file_path = self.obsidian_writer.save_digest(magazine)

            if not self.dry_run:
                await self.db.update_items_status(
                    [item.id for item in items], ItemStatus.PUBLISHED
                )

            # Aggregate token usage from step logs
            last_run = await self.db.get_last_run(week_id)
            if last_run and last_run.steps:
                total_input = sum(s.input_tokens for s in last_run.steps)
                total_output = sum(s.output_tokens for s in last_run.steps)

            # Estimate total cost
            cost = 0.0
            if last_run and last_run.steps:
                for step in last_run.steps:
                    cost += estimate_cost(
                        step.llm_model, step.input_tokens, step.output_tokens
                    )

            await self.db.update_pipeline_run(
                run_id,
                PipelineStatus.COMPLETED,
                total_input_tokens=total_input,
                total_output_tokens=total_output,
                estimated_cost_usd=cost,
            )

            # Send filter report if items were filtered
            if filter_report and status_updater:
                report_lines = [
                    f"🗑 Filtered {len(filter_report)} item(s) "
                    f"(drop_below={drop_below:.2f}):\n"
                ]
                type_icon = {
                    "irrelevant": "🚫",
                    "duplicate": "🔄",
                    "noise": "🗑",
                    "shallow": "📉",
                }
                for entry in filter_report:
                    icon = type_icon.get(entry["type"], "❌")
                    score = entry.get("score", 0.0)
                    gap = drop_below - score
                    score_str = f"score={score:.2f}, -{gap:.2f} to threshold"
                    report_lines.append(
                        f"{icon} {entry['summary']}\n"
                        f"   [{score_str}] → {entry['reason']}"
                    )
                await status_updater.send_message("\n".join(report_lines))

            if status_updater:
                await status_updater.finish(str(file_path))

            logger.info(
                "Pipeline complete for %s: %d tokens in, %d tokens out, $%.4f",
                week_id,
                total_input,
                total_output,
                cost,
            )

            return str(file_path)

        except Exception as e:
            logger.exception("Pipeline failed for %s: %s", week_id, e)
            await self.db.update_pipeline_run(
                run_id,
                PipelineStatus.FAILED,
                total_input_tokens=total_input,
                total_output_tokens=total_output,
            )
            if status_updater:
                await status_updater.fail(str(e))
            raise
