"""DryRunLLMProvider — deterministic mock for pipeline testing without real API calls."""

import re

from .provider import LLMResponse


class DryRunLLMProvider:
    """Deterministic mock LLM provider for dry-run pipeline testing.

    Parses item IDs from user messages and returns valid structured outputs
    without making any API calls. Token counts are always 0.

    Supports tool names used by FilterAgent and ClustererAgent.
    All free-text calls (researcher, writer, editor, translator) return
    a placeholder string.
    """

    async def generate(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        return LLMResponse(
            content="[DRY RUN] Placeholder content — no LLM call was made.",
            input_tokens=0,
            output_tokens=0,
            model=model,
        )

    async def generate_structured(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        tool_name: str,
        tool_description: str,
        output_schema: dict,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        item_ids = re.findall(r"- ID: (\S+)", user_message)

        if tool_name == "filter_items":
            structured = {
                "kept_items": [
                    {"id": iid, "relevance_score": 1.0, "reason": "dry run — all kept"}
                    for iid in item_ids
                ],
                "filtered_items": [],
            }

        elif tool_name == "create_clusters":
            structured = self._make_clusters(item_ids)

        elif tool_name == "collect_item":
            structured = {
                "type": "article",
                "summary": "[DRY RUN] Placeholder item summary",
                "tags": ["dry-run"],
                "language": "en",
            }

        else:
            structured = {}

        return LLMResponse(
            content="{}",
            input_tokens=0,
            output_tokens=0,
            model=model,
            structured_output=structured,
        )

    @staticmethod
    def _make_clusters(item_ids: list[str]) -> dict:
        """Split item IDs into up to 3 equally-sized dry-run clusters."""
        if not item_ids:
            return {"clusters": [], "quick_bites_item_ids": []}

        n = len(item_ids)
        num_groups = min(3, n)
        chunk = max(1, n // num_groups)

        groups: list[list[str]] = []
        for i in range(0, n, chunk):
            groups.append(item_ids[i : i + chunk])

        # Merge any surplus tail group into the last full group
        while len(groups) > num_groups:
            groups[-2].extend(groups.pop())

        clusters = [
            {
                "id": f"cluster-{i + 1}",
                "title": f"[DRY RUN] Topic Group {i + 1}",
                "editorial_angle": f"Dry-run placeholder cluster {i + 1} of {len(groups)}",
                "item_ids": group,
                "estimated_read_minutes": 3,
                "priority": i + 1,
            }
            for i, group in enumerate(groups)
            if group
        ]
        return {"clusters": clusters, "quick_bites_item_ids": []}
