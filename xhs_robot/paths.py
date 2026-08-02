from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path = Path(".xhs-robot")

    @property
    def session_file(self) -> Path:
        return self.root / "session.json"

    @property
    def profile_dir(self) -> Path:
        return self.root / "profile"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
