from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path = Path(".puppy")

    @property
    def session_file(self) -> Path:
        return self.state_dir / "anonymous-browser.json"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def browser_runtime_dir(self) -> Path:
        return self.root / "browser"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def tasks_dir(self) -> Path:
        return self.state_dir / "wanders"

    @property
    def observations_dir(self) -> Path:
        return self.root / "observations"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.browser_runtime_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.observations_dir.mkdir(parents=True, exist_ok=True)
