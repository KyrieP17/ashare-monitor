from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from .models import ThesisRevision


class FieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before: Any
    after: Any


class RevisionDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_version: int
    to_version: int
    changes: dict[str, FieldChange]


_CONTENT_FIELDS = (
    "market_expectation",
    "assessment",
    "support_evidence",
    "counter_evidence",
    "price_in_risks",
    "invalidation_conditions",
    "invalidation_response",
    "proposed_lifecycle_status",
)


def diff_revisions(before: ThesisRevision, after: ThesisRevision) -> RevisionDiff:
    if before.thesis_id != after.thesis_id:
        raise ValueError("cannot diff revisions from different theses")
    changes: dict[str, FieldChange] = {}
    before_data = before.model_dump(mode="json")
    after_data = after.model_dump(mode="json")
    for field in _CONTENT_FIELDS:
        if before_data[field] != after_data[field]:
            changes[field] = FieldChange(before=before_data[field], after=after_data[field])
    return RevisionDiff(from_version=before.version, to_version=after.version, changes=changes)
