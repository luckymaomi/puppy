from __future__ import annotations

from dataclasses import dataclass


class HumanInterventionRequired(RuntimeError):
    pass


class PageInteractionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceLink:
    resource_id: str
    resource_type: str
    path: str
    title: str


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    moved: bool
    source_exhausted: bool = False
    page_changed: bool = False
