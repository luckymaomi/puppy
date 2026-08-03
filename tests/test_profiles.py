import json

import pytest

from xhs_robot.paths import AppPaths
from xhs_robot.profiles import BrowserProfileStore


def test_browser_profiles_keep_default_login_and_create_isolated_profiles(tmp_path) -> None:
    paths = AppPaths(tmp_path / "runtime")
    store = BrowserProfileStore(paths)

    default = store.load("default")
    created = store.create("第二个账号")
    used = store.mark_used(created.id)

    assert default.chromium_directory == "Default"
    assert created.chromium_directory != default.chromium_directory
    assert used.last_used_at is not None
    assert {item.name for item in store.list()} == {"默认浏览器", "第二个账号"}


def test_browser_profile_index_always_restores_the_default_profile(tmp_path) -> None:
    paths = AppPaths(tmp_path / "runtime")
    store = BrowserProfileStore(paths)
    created = store.create("独立账号")
    paths.browser_profiles_file.write_text(
        json.dumps({"profiles": [created.to_dict()]}, ensure_ascii=False),
        encoding="utf-8",
    )

    restored = BrowserProfileStore(paths)

    assert restored.load("default").chromium_directory == "Default"
    assert restored.load(created.id) == created


def test_browser_profile_index_rejects_unsafe_chromium_directory(tmp_path) -> None:
    paths = AppPaths(tmp_path / "runtime")
    store = BrowserProfileStore(paths)
    default = store.load("default").to_dict()
    default["chromium_directory"] = ".."
    paths.browser_profiles_file.write_text(
        json.dumps({"profiles": [default]}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Chromium Profile 目录无效"):
        BrowserProfileStore(paths)
