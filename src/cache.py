"""
簡易檔案快取 (cache.py)
=======================
FinMind 免費版有請求上限,TWSE 也需要禮貌性節流,所以用「檔案快取」避免重複抓:

  - 每個 key 存成 cache/<key>.json,內容 = {fetched_at, fetched_date, data}
  - 讀取可指定 TTL(秒);None = 永不過期
  - 用法上的關鍵:
      * 過去月份的資料「不會再變」→ 永久快取(ttl=None)
      * 當月/最新資料會變 → 給短 TTL(例如 6 小時)

這樣第一次跑會實際打 API,之後重跑幾乎不再連網,既省額度又快。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# cache/ 放在專案根目錄底下(本檔在 src/,故往上一層)
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


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
        age = time.time() - obj.get("fetched_at", 0)
        if age > ttl_seconds:
            return None  # 過期,視為 miss
    return obj


def cache_set(key: str, data) -> dict:
    """寫快取,回傳寫入的物件(含 fetched_date 供標註來源用)。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    obj = {
        "fetched_at": time.time(),
        "fetched_date": time.strftime("%Y-%m-%d"),
        "data": data,
    }
    _path(key).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return obj
