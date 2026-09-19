#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filter.py —— TG2Gotify v2 的匹配逻辑（纯函数，无副作用，便于单测）
==================================================================

设计（PLAN.md §三）：
  每个监听源 4 个参数：
    enabled           0=不监听，1=监听（兼容 true/false/1/0/"true"/"false"）
    use_pool          true=走关键词过滤（共享池+频道extra），false=全量转发
    extra_keywords    频道私有额外关键词，与共享池是「或」的关系
    exclude_keywords  排除黑名单，两种模式下都生效

  判定顺序：
    1) enabled=0 → 跳过
    2) use_pool=true → 命中 =（池子词 OR 频道extra词 任一出现在消息文本中，不区分大小写）
       use_pool=false → 无条件命中
    3) 排除词兜底：消息中出现任一排除词 → 丢弃（全量转发模式下同样生效）
"""
from __future__ import annotations

from typing import Any, Mapping


def normalize_enabled(v: Any, default: bool = True) -> bool:
    """enabled 字段兼容 true/false、1/0、"true"/"false"、字符串数字；缺省视为启用。"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y", "是")
    return bool(v)


def _as_keyword_list(v: Any) -> list[str]:
    """把关键词字段规整为去掉空白、去掉空项的字符串列表（保留原大小写，匹配时再 lower）。"""
    if v is None:
        return []
    if isinstance(v, str):
        # 支持逗号/换行/中文逗号分隔的写法
        raw = v.replace("，", ",").replace("\r", "\n").split("\n")
        parts: list[str] = []
        for chunk in raw:
            parts.extend(chunk.split(","))
    elif isinstance(v, (list, tuple)):
        parts = list(v)
    else:
        return []
    return [str(p).strip() for p in parts if str(p).strip()]


def normalize_source(src: Mapping[str, Any]) -> dict:
    """把一个来源配置规整为标准四字段结构。

    兼容旧版 v1：
    - 老配置的 ``keywords``（数组）自动迁移为 ``extra_keywords``；
    - v1 里 ``keywords`` 缺失或为空数组 = 全量转发 → 映射为 use_pool=False（保语义）；
    - 老配置显式写了 use_pool 时以它为准（v1 不认识这个字段，冲突时一律按 v2 解释）。

    防御：src 不是 dict（手编辑配置把某个来源写坏成字符串等）时不抛异常，
    返回一个「已禁用」的占位源 —— 否则坏配置会让热重载/消息处理任务整体崩掉。
    """
    if not isinstance(src, Mapping):
        return {"label": str(src)[:50], "enabled": False, "use_pool": False,
                "extra_keywords": [], "exclude_keywords": []}
    has_old_kw = "keywords" in src
    raw_use_pool = src.get("use_pool")
    out = {
        "label": str(src.get("label") or ""),
        "enabled": normalize_enabled(src.get("enabled")),
        "use_pool": None if raw_use_pool is None else normalize_enabled(raw_use_pool, False),
        "extra_keywords": _as_keyword_list(src.get("extra_keywords")),
        "exclude_keywords": _as_keyword_list(src.get("exclude_keywords")),
    }
    # 旧字段迁移：keywords → extra_keywords（合并去重，不丢配置）
    old = _as_keyword_list(src.get("keywords"))
    if old:
        seen = set(out["extra_keywords"])
        for kw in old:
            if kw not in seen:
                out["extra_keywords"].append(kw)
                seen.add(kw)
    if out["use_pool"] is None:
        # v1 语义：有关键词才过滤；没写 keywords 或 keywords 为空 = 全量转发
        if has_old_kw and not old:
            out["use_pool"] = False
        elif not has_old_kw and not out["extra_keywords"]:
            out["use_pool"] = False
        else:
            out["use_pool"] = True
    return out


def _contains_any(text_lower: str, kws: list[str]) -> bool:
    """不区分大小写地判断任一关键词是否出现在文本中。"""
    return any(kw.lower() in text_lower for kw in kws)


def match_message(text: str, src: Mapping[str, Any],
                  pool: list[str] | None = None) -> tuple[bool, str]:
    """核心匹配函数。

    参数:
        text  消息文本（原始，含大小写）
        src   单个来源配置（未规整也行，内部先 normalize）
        pool  共享关键词池（KEYWORD_POOL）

    返回:
        (是否推送, 原因) —— 原因用于日志：'hit_pool' / 'hit_extra' /
        'pass_all' / 'excluded' / 'no_hit' / 'disabled'
    """
    s = normalize_source(src)
    pool_kws = _as_keyword_list(pool or [])

    # 1) 总开关
    if not s["enabled"]:
        return False, "disabled"

    text_lower = (text or "").lower()

    # 3) 排除词兜底（先查排除，语义与计划书一致且省一次匹配）
    if s["exclude_keywords"] and _contains_any(text_lower, s["exclude_keywords"]):
        return False, "excluded"

    # 2) 命中判定
    if not s["use_pool"]:
        return True, "pass_all"  # 全量转发（排除词已在上面兜底）
    if _contains_any(text_lower, pool_kws):
        return True, "hit_pool"
    if _contains_any(text_lower, s["extra_keywords"]):
        return True, "hit_extra"
    return False, "no_hit"


def migrate_pool(cfg: dict) -> None:
    """v1 配置自动迁移：config 里没写 KEYWORD_POOL 时，把各来源老 keywords
    的**交集**提升为共享池（v1 各频道词表相同时即全部共享），各来源保留
    交集之外的私词作 extra_keywords —— 迁移后过滤语义与 v1 完全一致。
    （纯配置逻辑放这里，便于无依赖单测；tg2gotify.load_config 调用。）"""
    old_lists: list[set[str]] = []
    for src in cfg.get("SOURCES", {}).values():
        if not isinstance(src, dict):
            continue
        s = normalize_source(src)
        if s["use_pool"] and s["extra_keywords"]:
            old_lists.append(set(s["extra_keywords"]))
    if not old_lists:
        cfg["KEYWORD_POOL"] = []
        return
    pool_set = set.intersection(*old_lists)
    # 按原配置出现顺序排列（sorted 会打乱用户习惯的词序）；无公共词时池子为空
    pool_ordered: list[str] = []
    if pool_set:
        for src in cfg["SOURCES"].values():
            if not isinstance(src, dict):
                continue
            s = normalize_source(src)
            if not (s["use_pool"] and s["extra_keywords"]):
                continue
            for w in s["extra_keywords"]:
                if w in pool_set and w not in pool_ordered:
                    pool_ordered.append(w)
    cfg["KEYWORD_POOL"] = pool_ordered
    # 统一把各来源的老 keywords 字段清理为 extra_keywords（池内词去掉、独有词保留；
    # 无公共词时 pool_set 空 = 独有词全部保留，语义不变）
    for src in cfg["SOURCES"].values():
        if not isinstance(src, dict) or "keywords" not in src:
            continue
        s = normalize_source(src)
        if s["use_pool"] and s["extra_keywords"]:
            src["extra_keywords"] = [w for w in s["extra_keywords"] if w not in pool_set]
            src["use_pool"] = True  # 迁移后显式落盘，避免被「无关键词=全量」的缺省规则误判
        src.pop("keywords", None)  # 已迁移完成，清掉旧字段避免歧义
