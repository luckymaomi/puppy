from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path = Path(".xhs-robot")

    @property
    def session_file(self) -> Path:
        return self.state_dir / "browser-session.json"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def browser_profiles_file(self) -> Path:
        return self.state_dir / "browser-profiles.json"

    @property
    def browser_data_dir(self) -> Path:
        return self.root / "profile"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def tasks_dir(self) -> Path:
        return self.state_dir / "tasks"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
