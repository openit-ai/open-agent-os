"""Pure behavioral feature aggregation; persistence is handled by the worker."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp
from typing import Iterable

from .features import BehavioralFeature

@dataclass(frozen=True)
class FeatureAggregate:
    feature_name: str
    count: int
    mean: float
    variance: float
    freshness: float
    sessions: int


def aggregate_features(features: Iterable[BehavioralFeature], now: datetime | None = None) -> dict[str, FeatureAggregate]:
    now = now or datetime.now(timezone.utc)
    values: dict[str, list[float]] = defaultdict(list)
    sessions: dict[str, set[str]] = defaultdict(set)
    dates: dict[str, list[datetime]] = defaultdict(list)
    for feature in features:
        values[feature.name].append(feature.value * max(0.0, min(1.0, feature.confidence)))
        session_key = feature.observed_at[:10]
        sessions[feature.name].add(session_key)
        try:
            dt = datetime.fromisoformat(feature.observed_at.replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            dates[feature.name].append(dt)
        except ValueError:
            pass
    result: dict[str, FeatureAggregate] = {}
    for name, vals in values.items():
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        age_days = max(0.0, (now - max(dates[name], default=now)).total_seconds() / 86400)
        result[name] = FeatureAggregate(name, len(vals), mean, variance, exp(-age_days / 30), len(sessions[name]))
    return result
