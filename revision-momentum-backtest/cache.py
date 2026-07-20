"""
簡易檔案快取 (cache.py)
=======================
FinMind 免費版有請求上限,且本專案要逐檔抓數百檔股票,所以用「檔案快取」:
每個 key 存成 cache/<key>.json = {fetched_at, fetched_date, data}。

關鍵用途:讓抓取「可中斷、可續跑」。若中途撞到額度上限,已抓的都在快取裡,
下次重跑直接沿用,不必從頭。過去的月營收/財報/股價都不會再變 → 永久快取。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent / "cache"


def _path(key: str) -> Path:
    safe = key.replace("/", "_").replace("\\", "_")
    return CACHE_DIR / f"{safe}.json"


def cache_get(key: str, ttl_seconds: float | None = None) -> dict | None:
    """讀快取。回傳 {fetched_at, fetched_date, data} 或 None(不存在/過期/損毀)。"""
    p = _path(key)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if ttl_seconds is not None:
        if time.time() - obj.get("fetched_at", 0) > ttl_seconds:
            return None
    return obj


def cache_set(key: str, data) -> dict:
    """寫快取,回傳寫入的物件。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    obj = {
        "fetched_at": time.time(),
        "fetched_date": time.strftime("%Y-%m-%d"),
        "data": data,
    }
    _path(key).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return obj


def cache_has(key: str) -> bool:
    return _path(key).exists()
