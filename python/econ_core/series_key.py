#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""series_key.py —— series_key 的规范化（别名解析 + 大小写变体）。

为什么需要它
------------
同一条序列在本项目里可能有多种键形：

    nbs|gdp|cny_100m                                      <- 声明式规范键
    NBS|000000000000|db8e5a86c08246e79b1b11251927e740    <- normalize 自动推导，知识库标了 alias_of
    nbs|000000000000|db8e5a86c08246e79b1b11251927e740    <- 落盘信封里出现的小写变体

不做归一化就会「同一条序列被算两遍」：export 的 CSV 多出行、数据字典的序列数虚高、
报告的总行数虚高。tools/export.py 与 tools/report.py 共用本模块，不各写一份。

解析规则（依次尝试）
--------------------
1. 知识库里能直接查到 -> 跟随 alias_of 解析到规范键；
2. 精确查不到、但忽略大小写能匹配上知识库某个键 -> 用那个键再解析一次；
3. 都不行 -> 原样返回（调用方自己决定是跳过还是保留）。

用法::

    from econ_core.series_key import canonical_key, is_alias_key
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import source_profiler  # type: ignore[no-redef]
else:
    from . import source_profiler

__all__ = ["canonical_key", "is_alias_key", "knowledge_keys"]


def knowledge_keys() -> dict[str, Any]:
    """知识库的 indicators 段（含 alias 条目）；读不到就返回空字典。"""
    try:
        return dict(source_profiler.load_knowledge().get("indicators") or {})
    except Exception:  # noqa: BLE001 - 调用方不该因为知识库缺失而崩
        return {}


def _match_key(key: str, kb: dict[str, Any]) -> Optional[str]:
    """在知识库里找这个键：先精确匹配，再忽略大小写匹配。"""
    if key in kb:
        return key
    low = key.lower()
    for cand in kb:
        if str(cand).lower() == low:
            return str(cand)
    return None


def canonical_key(key: str) -> str:
    """把任意键形解析到规范键；解析不了就原样返回。"""
    kb = knowledge_keys()
    if not kb:
        return key
    matched = _match_key(key, kb)
    if matched is None:
        return key
    try:
        return str(source_profiler.profile_series(matched)["series_key"])
    except Exception:  # noqa: BLE001
        return key


def is_alias_key(key: str) -> bool:
    """这个键本身是不是 alias 形式（知识库里带 alias_of 字段）。

    去重时用它决定「留哪一份」：留 is_alias_key 为 False 的那份。
    """
    kb = knowledge_keys()
    matched = _match_key(key, kb)
    if matched is None:
        return False
    item = kb.get(matched)
    return bool(isinstance(item, dict) and item.get("alias_of"))
