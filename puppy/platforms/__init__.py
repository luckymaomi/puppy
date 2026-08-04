from .base import (
    AdvanceResult,
    HumanInterventionRequired,
    PageInteractionError,
    ResourceLink,
)
from .bilibili import BilibiliAdapter
from .xhs import XiaohongshuAdapter


def create_adapter(platform, page, evidence, *, comments_limit):
    if platform == "xiaohongshu":
        return XiaohongshuAdapter(page, evidence, comments_limit=comments_limit)
    if platform == "bilibili":
        return BilibiliAdapter(page, evidence, comments_limit=comments_limit)
    raise ValueError(f"未知平台: {platform}")


__all__ = [
    "AdvanceResult",
    "BilibiliAdapter",
    "HumanInterventionRequired",
    "PageInteractionError",
    "ResourceLink",
    "XiaohongshuAdapter",
    "create_adapter",
]
