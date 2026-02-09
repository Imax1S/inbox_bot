"""Data models for the digest pipeline."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ItemType(str, Enum):
    ARTICLE = "ARTICLE"
    TOPIC_SEED = "TOPIC_SEED"
    CONTEXT_NOTE = "CONTEXT_NOTE"


class ItemStatus(str, Enum):
    COLLECTED = "COLLECTED"
    CLUSTERED = "CLUSTERED"
    PUBLISHED = "PUBLISHED"


class PipelineStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class Item:
    id: str
    created_at: datetime
    type: ItemType
    raw_content: str
    summary: str
    tags: list[str]
    language: str
    week_id: str
    status: ItemStatus
    source_url: str | None = None
    extracted_text: str | None = None

    def short_id(self) -> str:
        return self.id[:8]

    def tags_str(self) -> str:
        return " ".join(f"#{t.replace('-', '_')}" for t in self.tags)


@dataclass
class Cluster:
    id: str
    title: str
    editorial_angle: str
    item_ids: list[str]
    estimated_read_minutes: int
    priority: int


@dataclass
class ClusterResult:
    clusters: list[Cluster]
    quick_bites_item_ids: list[str]

    @classmethod
    def from_json(cls, data: dict) -> "ClusterResult":
        clusters = []
        for c in data.get("clusters", []):
            clusters.append(Cluster(
                id=c["id"],
                title=c["title"],
                editorial_angle=c.get("editorial_angle", ""),
                item_ids=c.get("item_ids", []),
                estimated_read_minutes=c.get("estimated_read_minutes", 3),
                priority=c.get("priority", 1),
            ))
        return cls(
            clusters=clusters,
            quick_bites_item_ids=data.get("quick_bites_item_ids", []),
        )


@dataclass
class StepLog:
    id: str
    run_id: str
    agent: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    input_tokens: int
    output_tokens: int
    llm_model: str
    details: str
    error: str | None = None


@dataclass
class PipelineRun:
    id: str
    week_id: str
    started_at: datetime
    finished_at: datetime | None
    status: PipelineStatus
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    steps: list[StepLog] = field(default_factory=list)
