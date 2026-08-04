from puppy.platforms.bilibili import ARTICLE_PATTERN, VIDEO_PATTERN
from puppy.platforms.xhs import NOTE_PATTERN


def test_platform_resource_ids_only_match_known_public_paths() -> None:
    xhs = NOTE_PATTERN.search("/explore/64d73b70c2133c0001abcd12")
    video = VIDEO_PATTERN.search("/video/BV12C411W7CM")
    article = ARTICLE_PATTERN.search("/read/cv3543806")

    assert xhs and xhs.group(1) == "64d73b70c2133c0001abcd12"
    assert video and video.group(1) == "BV12C411W7CM"
    assert article and article.group(1) == "cv3543806"
    assert NOTE_PATTERN.search("/user/profile/64d73b70c2133c0001abcd12") is None
