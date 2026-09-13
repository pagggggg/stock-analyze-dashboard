"""
輕量檔案快取(cache.py)
=======================
所有外部抓取(FinMind、investing.com)都經過這裡,存成 JSON 檔,可中斷可續跑。
key -> data/raw/<key>.json  ; 內容為 {"data": ...}
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_RAW = _ROOT / "data" / "raw"
_RAW.mkdir(parents=True, exist_ok=True)


def _path(key: str) -> Path:
    safe = key.replace("/", "_")
    return _RAW / f"{safe}.json"


def cache_get(key: str):
    p = _path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def cache_set(key: str, data) -> None:
    _path(key).write_text(json.dumps({"data": data}, ensure_ascii=False), encoding="utf-8")
