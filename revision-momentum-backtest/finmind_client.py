"""
FinMind 資料層 (finmind_client.py)
==================================
負責「逐檔」向 FinMind 抓三種資料,全部走檔案快取、可中斷可續跑:

  1) get_universe()        全體普通股 → 固定種子隨機抽樣(代表性樣本,對照組同池)
  2) fetch_month_revenue() 月營收(算 YoY 加速度 / 代理訊號一)
  3) fetch_eps()           單季 EPS(算 YoY / 代理訊號二)
  4) fetch_prices()        日收盤價(算 3/6/12 月未來報酬;基準 0050 亦用此)

免費版限制(實測,已誠實反映在報告):
  - 不能抓「全市場單一請求」→ 只能逐檔;故用隨機抽樣控制檔數。
  - 不能用 taiwan_stock_daily_adj(還原權值)→ 只能用未還原收盤價(股利拖累,已揭露)。
撞到額度/限流會丟 QuotaExceeded,由上層決定「停下來、用已快取的資料繼續分析」。
"""

from __future__ import annotations

import random
import re
import time

from cache import cache_get, cache_set

# 全市場普通股清單也快取(避免每次都打 taiwan_stock_info)
_UNIVERSE_KEY = "universe_common_stocks"


class FetchError(Exception):
    """抓取失敗的基底類別。"""


class QuotaExceeded(FetchError):
    """撞到 FinMind 免費額度 / 限流時丟出,讓抓取流程優雅停下(已抓的都在快取)。"""


class TransientError(FetchError):
    """暫時性錯誤(網路重置等),重試用盡仍失敗 → 不可快取,稍後重試同一檔。"""


_DL = None  # DataLoader 單例:只建一次、只登入一次(避免每次抓都多一次 login 請求)


def _loader(retries: int = 4):
    """回傳共用的 DataLoader 單例;建立(含登入)撞到網路錯誤會退避重試。

    若環境變數 FINMIND_TOKEN 有值,會用它登入(免費註冊帳號的額度遠高於匿名),
    大幅加快逐檔抓取;沒有就用匿名(額度很低,會很慢)。
    """
    global _DL
    if _DL is not None:
        return _DL
    import os
    from pathlib import Path

    from FinMind.data import DataLoader  # 延遲匯入
    # token 來源優先序:環境變數 FINMIND_TOKEN > 專案內 .finmind_token 檔(已 gitignore)
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        tok_file = Path(__file__).resolve().parent / ".finmind_token"
        if tok_file.exists():
            token = tok_file.read_text(encoding="utf-8").strip()
    last = None
    for attempt in range(retries):
        try:
            dl = DataLoader()
            if token:
                try:
                    dl.login_by_token(api_token=token)
                except Exception:  # noqa: BLE001 — token 失敗就退回匿名
                    pass
            _DL = dl
            return _DL
        except Exception as e:  # noqa: BLE001 — 登入時的網路錯誤,退避重試
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"建立 FinMind DataLoader 失敗(網路?):{last}")


def _is_quota_error(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in ("upper limit", "402", "request", "too many", "額度", "上限"))


def _safe_call(fn, *, retries: int = 4, sleep: float = 0.15):
    """呼叫 FinMind 並分類結果:

      - 正常 → 回傳 DataFrame
      - 該資料集免費版不開放(level is free)→ 回傳 None(可安全快取為空)
      - 撞到限流/額度 → raise QuotaExceeded
      - 其他暫時性錯誤(網路重置等)重試用盡 → raise TransientError(不可快取)
    """
    last = None
    for attempt in range(retries):
        try:
            df = fn()
            time.sleep(sleep)  # 禮貌節流
            return df
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            if _is_quota_error(msg):
                if attempt < retries - 1:
                    time.sleep(2.0 * (attempt + 1))  # 線性退避
                    continue
                raise QuotaExceeded(msg) from e
            if "level is free" in msg.lower():
                return None  # 該資料集免費版不開放 → 當作查無資料,可快取為空
            # 其他暫時性錯誤:短暫退避重試
            if attempt < retries - 1:
                time.sleep(0.6 * (attempt + 1))
                continue
    raise TransientError(str(last))  # 重試用盡且非額度類 → 不可快取


# ─────────────────────────────────────────────────────────────────────
# 1) 股票池:全體普通股隨機抽樣
# ─────────────────────────────────────────────────────────────────────
def _all_common_stocks() -> list[str]:
    cached = cache_get(_UNIVERSE_KEY)  # 永久快取
    if cached is not None:
        return cached["data"]
    dl = _loader()
    info = dl.taiwan_stock_info()
    info = info[info["type"].isin(["twse", "tpex"])]
    commons = sorted(
        {s for s in info["stock_id"] if re.fullmatch(r"[1-9][0-9]{3}", str(s))}
    )
    cache_set(_UNIVERSE_KEY, commons)
    return commons


def get_universe(seed: int, size: int) -> list[str]:
    """回傳「固定種子」隨機抽樣後的股票池(可重現)。size 大於母體則回全體。"""
    commons = _all_common_stocks()
    if size >= len(commons):
        return commons
    rng = random.Random(seed)
    return sorted(rng.sample(commons, size))


# ─────────────────────────────────────────────────────────────────────
# 2) 月營收
# ─────────────────────────────────────────────────────────────────────
def fetch_month_revenue(stock_id: str, start_date: str) -> list[dict] | None:
    """回傳 [{ry, rm, revenue, date}] 由舊到新;查無資料回 []。撞額度丟 QuotaExceeded。

    欄位:ry/rm = 營收所屬年/月;revenue = 當月營收(TWD);
         date = FinMind 給的「次月一日」字串(用來推算公開日 = 次月 10 日)。
    """
    key = f"rev_{stock_id}"
    cached = cache_get(key)
    if cached is not None:
        return cached["data"]
    dl = _loader()
    df = _safe_call(lambda: dl.taiwan_stock_month_revenue(stock_id=stock_id, start_date=start_date))
    if df is None or len(df) == 0:
        cache_set(key, [])
        return []
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append({
                "ry": int(r["revenue_year"]),
                "rm": int(r["revenue_month"]),
                "revenue": float(r["revenue"]),
                "date": str(r["date"]),
            })
        except (TypeError, ValueError, KeyError):
            continue
    rows.sort(key=lambda x: (x["ry"], x["rm"]))
    cache_set(key, rows)
    return rows


# ─────────────────────────────────────────────────────────────────────
# 3) 單季 EPS
# ─────────────────────────────────────────────────────────────────────
def fetch_eps(stock_id: str, start_date: str) -> list[dict] | None:
    """回傳 [{date, eps}](季末日期字串 + 該季 EPS)由舊到新;查無回 []。"""
    key = f"eps_{stock_id}"
    cached = cache_get(key)
    if cached is not None:
        return cached["data"]
    dl = _loader()
    df = _safe_call(lambda: dl.taiwan_stock_financial_statement(stock_id=stock_id, start_date=start_date))
    if df is None or len(df) == 0:
        cache_set(key, [])
        return []
    eps_df = df[df["type"] == "EPS"]
    rows = []
    for _, r in eps_df.iterrows():
        try:
            rows.append({"date": str(r["date"]), "eps": float(r["value"])})
        except (TypeError, ValueError, KeyError):
            continue
    rows.sort(key=lambda x: x["date"])
    cache_set(key, rows)
    return rows


# ─────────────────────────────────────────────────────────────────────
# 4) 日收盤價(未還原;免費版無 daily_adj)
# ─────────────────────────────────────────────────────────────────────
def fetch_prices(stock_id: str, start_date: str) -> list[dict] | None:
    """回傳 [{date, close}] 由舊到新;查無回 []。"""
    key = f"px_{stock_id}"
    cached = cache_get(key)
    if cached is not None:
        return cached["data"]
    dl = _loader()
    df = _safe_call(lambda: dl.taiwan_stock_daily(stock_id=stock_id, start_date=start_date))
    if df is None or len(df) == 0:
        cache_set(key, [])
        return []
    rows = []
    for _, r in df.iterrows():
        try:
            c = float(r["close"])
        except (TypeError, ValueError, KeyError):
            continue
        if c == c and c > 0:
            rows.append({"date": str(r["date"]), "close": round(c, 4)})
    rows.sort(key=lambda x: x["date"])
    cache_set(key, rows)
    return rows
