from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


RESOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{3,80}")


def observed_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class CommentObservation:
    author: str
    text: str
    comment_id: str | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    platform: str
    resource_type: str
    resource_id: str
    source_url: str
    metadata: dict[str, Any]
    content: str
    comments: tuple[CommentObservation, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    observed_at: str = field(default_factory=observed_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ObservationStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, observation: Observation) -> Path:
        if observation.platform not in {"xiaohongshu", "bilibili"}:
            raise ValueError("观察结果平台无效")
        if not RESOURCE_ID_PATTERN.fullmatch(observation.resource_id):
            raise ValueError("观察结果资源 ID 无效")
        platform_dir = self.root / observation.platform
        platform_dir.mkdir(parents=True, exist_ok=True)
        path = platform_dir / f"{observation.resource_type}-{observation.resource_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(observation.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("观察结果数量必须在 1 到 500 之间")
        paths = sorted(
            self.root.glob("*/*.json"), key=lambda path: path.stat().st_mtime, reverse=True
        )
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths[:limit]]
