"""
檔案快取 (cache.py)
===================
每個 key 存成 cache/<key>.json = {fetched_at, fetched_date, data}。

用途:
  1. FinMind 免費版有每小時請求上限 → 抓到的要留著,可中斷可續跑。
  2. TWSE 逐月抓本益比(一檔約 220 個月)→ 過去月份不會變,永久快取。
  3. 讓回測「可重現」:同一份快取重跑,結果完全一樣。
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
    """讀快取,回傳 {fetched_at, fetched_date, data};不存在/過期/損毀回 None。"""
    p = _path(key)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if ttl_seconds is not None and time.time() - obj.get("fetched_at", 0) > ttl_seconds:
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
