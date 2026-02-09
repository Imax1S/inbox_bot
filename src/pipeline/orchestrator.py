"""Digest pipeline orchestrator — deterministic code that runs the multi-agent pipeline."""

import logging
from datetime import datetime
from uuid import uuid4

from ..agents.clusterer import ClustererAgent
from ..agents.editor import EditorAgent
from ..agents.researcher import ResearcherAgent
from ..agents.writer import WriterAgent
from ..db.database import Database
from ..db.models import ItemStatus, PipelineRun, PipelineStatus
from ..llm.provider import estimate_cost
from ..obsidian_writer import ObsidianWriter
from .status_updater import StatusUpdater

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        db: Database,
        clusterer: ClustererAgent,
        researcher: ResearcherAgent,
        writer: WriterAgent,
        editor: EditorAgent,
        obsidian_writer: ObsidianWriter,
    ):
        self.db = db
        self.clusterer = clusterer
        self.researcher = researcher
        self.writer = writer
        self.editor = editor
        self.obsidian_writer = obsidian_writer

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

        if status_updater:
            await status_updater.start(week_id, len(items))

        total_input = 0
        total_output = 0

        # Read user's language preference (default: Russian)
        digest_language = await self.db.get_setting("digest_language", "ru")
        lang_name = "Russian" if digest_language == "ru" else "English"
        logger.info("Digest language: %s (%s)", digest_language, lang_name)

        try:
            # ── Step 1: Cluster ──
            if status_updater:
                await status_updater.update(0, f"Clustering {len(items)} items...")
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
                        1,
                        f"Researching ({i + 1}/{len(cluster_result.clusters)}): "
                        f"{cluster.title}",
                    )
                logger.info("Researching: %s", cluster.title)

                cluster_items = [
                    item for item in items if item.id in cluster.item_ids
                ]
                briefs[cluster.id] = await self.researcher.process(
                    cluster, cluster_items, run_id=run_id,
                    language=lang_name,
                )

            # ── Step 3: Write ──
            articles: dict[str, str] = {}
            for i, cluster in enumerate(cluster_result.clusters):
                if status_updater:
                    await status_updater.update(
                        2,
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
                    language=lang_name,
                )

            # ── Step 4: Edit & Assemble ──
            if status_updater:
                await status_updater.update(3, "Assembling final magazine...")
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
                language=lang_name,
            )

            # ── Save & Finalize ──
            file_path = self.obsidian_writer.save_digest(magazine)

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
