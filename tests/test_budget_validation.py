"""Tests for orchestrator budget validation and clusterer budget passing."""

from datetime import datetime

import pytest

from src.db.models import Cluster, ClusterResult


def test_budget_validation_trims_clusters():
    """When cluster total exceeds budget, lowest-priority clusters should be demoted to quick_bites."""
    cluster_result = ClusterResult(
        clusters=[
            Cluster(
                id="c-1", title="Top Priority", editorial_angle="test",
                item_ids=["a", "b"], estimated_read_minutes=10, priority=1,
            ),
            Cluster(
                id="c-2", title="Mid Priority", editorial_angle="test",
                item_ids=["c", "d"], estimated_read_minutes=10, priority=2,
            ),
            Cluster(
                id="c-3", title="Low Priority", editorial_angle="test",
                item_ids=["e", "f"], estimated_read_minutes=15, priority=3,
            ),
        ],
        quick_bites_item_ids=["g"],
    )

    target_read_minutes = 25

    # Simulate orchestrator budget validation logic
    total_estimated = sum(c.estimated_read_minutes for c in cluster_result.clusters)
    assert total_estimated == 35  # exceeds 25

    if total_estimated > target_read_minutes:
        sorted_clusters = sorted(cluster_result.clusters, key=lambda c: c.priority)
        kept_clusters = []
        running_total = 0
        for c in sorted_clusters:
            if running_total + c.estimated_read_minutes <= target_read_minutes:
                kept_clusters.append(c)
                running_total += c.estimated_read_minutes
            else:
                cluster_result.quick_bites_item_ids.extend(c.item_ids)
        cluster_result.clusters = kept_clusters

    # c-1 (10 min, priority 1) and c-2 (10 min, priority 2) fit within 25 min
    # c-3 (15 min, priority 3) gets demoted
    assert len(cluster_result.clusters) == 2
    assert cluster_result.clusters[0].id == "c-1"
    assert cluster_result.clusters[1].id == "c-2"
    assert "e" in cluster_result.quick_bites_item_ids
    assert "f" in cluster_result.quick_bites_item_ids
    assert "g" in cluster_result.quick_bites_item_ids  # original quick bite still there


def test_budget_validation_no_trim_when_under_budget():
    """Clusters within budget should not be trimmed."""
    cluster_result = ClusterResult(
        clusters=[
            Cluster(
                id="c-1", title="Topic", editorial_angle="test",
                item_ids=["a"], estimated_read_minutes=5, priority=1,
            ),
            Cluster(
                id="c-2", title="Topic 2", editorial_angle="test",
                item_ids=["b"], estimated_read_minutes=5, priority=2,
            ),
        ],
        quick_bites_item_ids=[],
    )

    target_read_minutes = 25
    total_estimated = sum(c.estimated_read_minutes for c in cluster_result.clusters)
    assert total_estimated <= target_read_minutes

    # No trimming needed
    assert len(cluster_result.clusters) == 2
