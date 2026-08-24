from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import MarketSnapshot


def _without_source_retrieval_times(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_source_retrieval_times(item) for item in value]
    if not isinstance(value, dict):
        return value

    is_source_ref = {"source_id", "provider", "retrieved_at"}.issubset(value)
    return {
        key: _without_source_retrieval_times(item)
        for key, item in value.items()
        if not (is_source_ref and key == "retrieved_at")
    }


def snapshot_semantic_payload(snapshot: MarketSnapshot) -> dict[str, Any]:
    """Return stable business content, excluding only parse-event timestamps.

    The full first-written snapshot remains the stored audit record. This view is
    used only to decide whether a later parse represents the same immutable data.
    """
    payload = snapshot.model_dump(mode="json")
    payload.pop("created_at", None)
    return _without_source_retrieval_times(payload)


def snapshot_semantic_fingerprint(snapshot: MarketSnapshot) -> str:
    canonical = json.dumps(
        snapshot_semantic_payload(snapshot),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshots_semantically_equal(left: MarketSnapshot, right: MarketSnapshot) -> bool:
    return snapshot_semantic_payload(left) == snapshot_semantic_payload(right)
