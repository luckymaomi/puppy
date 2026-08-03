from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .paths import AppPaths


PROFILE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
PROFILE_DIRECTORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    id: str
    name: str
    chromium_directory: str
    created_at: str
    last_used_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BrowserProfileStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.paths.ensure()
        self.ensure_default()

    def ensure_default(self) -> BrowserProfile:
        profiles = self._read()
        for profile in profiles:
            if profile.id == "default":
                return profile
        if any(profile.name.casefold() == "默认浏览器" for profile in profiles):
            raise ValueError("浏览器资料索引占用了默认浏览器名称")
        profile = BrowserProfile(
            id="default",
            name="默认浏览器",
            chromium_directory="Default",
            created_at=now_iso(),
        )
        self._write([profile, *profiles])
        return profile

    def list(self) -> list[BrowserProfile]:
        return sorted(
            self._read(),
            key=lambda item: item.last_used_at or item.created_at,
            reverse=True,
        )

    def load(self, profile_id: str) -> BrowserProfile:
        self._validate_id(profile_id)
        for profile in self._read():
            if profile.id == profile_id:
                return profile
        raise ValueError(f"浏览器资料不存在: {profile_id}")

    def create(self, name: str) -> BrowserProfile:
        normalized = " ".join(name.split())
        if not normalized or len(normalized) > 40:
            raise ValueError("浏览器名称长度必须在 1 到 40 个字符之间")
        profiles = self._read()
        if any(item.name.casefold() == normalized.casefold() for item in profiles):
            raise ValueError("浏览器名称已存在")
        profile_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
        profile = BrowserProfile(
            id=profile_id,
            name=normalized,
            chromium_directory=f"XhsProfile-{profile_id}",
            created_at=now_iso(),
        )
        self._write([*profiles, profile])
        return profile

    def mark_used(self, profile_id: str) -> BrowserProfile:
        selected = self.load(profile_id)
        updated = BrowserProfile(
            id=selected.id,
            name=selected.name,
            chromium_directory=selected.chromium_directory,
            created_at=selected.created_at,
            last_used_at=now_iso(),
        )
        profiles = [updated if item.id == profile_id else item for item in self._read()]
        self._write(profiles)
        return updated

    def _read(self) -> list[BrowserProfile]:
        path = self.paths.browser_profiles_file
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("浏览器资料索引无法读取") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), list):
            raise ValueError("浏览器资料索引格式无效")
        try:
            profiles = [BrowserProfile(**item) for item in raw["profiles"]]
        except (TypeError, KeyError) as exc:
            raise ValueError("浏览器资料索引格式无效") from exc
        ids: set[str] = set()
        names: set[str] = set()
        directories: set[str] = set()
        for profile in profiles:
            self._validate_id(profile.id)
            if (
                not isinstance(profile.name, str)
                or not profile.name.strip()
                or profile.name != " ".join(profile.name.split())
                or len(profile.name) > 40
            ):
                raise ValueError("浏览器资料名称无效")
            if (
                not isinstance(profile.chromium_directory, str)
                or not PROFILE_DIRECTORY_PATTERN.fullmatch(
                    profile.chromium_directory
                )
                or profile.chromium_directory
                != (
                    "Default"
                    if profile.id == "default"
                    else f"XhsProfile-{profile.id}"
                )
            ):
                raise ValueError("Chromium Profile 目录无效")
            if not isinstance(profile.created_at, str) or not profile.created_at:
                raise ValueError("浏览器资料创建时间无效")
            if profile.last_used_at is not None and not isinstance(
                profile.last_used_at, str
            ):
                raise ValueError("浏览器资料最近使用时间无效")
            normalized_name = profile.name.casefold()
            if (
                profile.id in ids
                or normalized_name in names
                or profile.chromium_directory in directories
            ):
                raise ValueError("浏览器资料索引存在重复项")
            ids.add(profile.id)
            names.add(normalized_name)
            directories.add(profile.chromium_directory)
        return profiles

    def _write(self, profiles: list[BrowserProfile]) -> None:
        path = self.paths.browser_profiles_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"profiles": [item.to_dict() for item in profiles]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _validate_id(profile_id: str) -> None:
        if not isinstance(profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(
            profile_id
        ):
            raise ValueError("浏览器资料 ID 格式无效")
