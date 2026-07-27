"""
台股資料層 (sources_tw.py)
==========================
兩個來源,刻意分開,因為額度性質完全不同:

  1. TWSE「個股日本益比」BWIBBU —— 公開端點,**不吃 FinMind 額度**,可回溯到 2005 年。
     這是台股 PE 的權威口徑:trailing PE(近四季已公布 EPS),由交易所自己算。
     逐月抓(一次回傳一整月每日值),過去月份永久快取。

  2. FinMind 綜合損益表 —— 抓季 EPS 與毛利率(策略 B 的基本面訊號)。
     免費版有每小時上限,撞到會丟 QuotaExceeded,由上層決定「稍後再續跑」。

前視偏誤處理:
  - PE 是當日盤後公布 → 當日即可用。
  - 財報用「法定申報期限」當可用日(Q1→5/15…),保守且不會早於實際公布日。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import params as P
from cache import cache_get, cache_set


class QuotaExceeded(Exception):
    """撞到 FinMind 免費額度/限流 → 上層可以稍後續跑(已抓的都在快取)。"""


# ─────────────────────────────────────────────────────────────────────
# 1) TWSE 每日本益比(BWIBBU)—— 免額度
# ─────────────────────────────────────────────────────────────────────
_TWSE_BWIBBU = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU"

# {stock_id: {"failed_months": n}} —— 記錄「抓取失敗(非無資料)」的月數。
# 上層(fetch_data / run)必須檢查它:失敗月數 > 0 代表這檔的 PE 歷史是殘缺的,
# 不該直接拿去回測,否則會把「抓不到」誤當成「當時沒有有效 PE」。
LAST_FETCH_STATS: dict[str, dict] = {}


def _roc_to_iso(s: str) -> str | None:
    """民國日期 '104年06月01日' 或 '104/06/01' → '2015-06-01'。"""
    s = s.strip().replace("年", "/").replace("月", "/").replace("日", "")
    parts = [p for p in s.split("/") if p]
    if len(parts) != 3:
        return None
    try:
        y = int(parts[0]) + 1911
        return f"{y:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    except ValueError:
        return None


class PEFetchFailed(Exception):
    """抓取失敗(網路/限流/非預期回應)—— 與『確定無資料』必須分開處理。

    ★ 為什麼要有這個例外:原本的實作把任何失敗都當成 month_rows = [] 並**寫進永久快取**,
      結果 TWSE 一限流,整段歷史就被靜默記成「這些日子沒有本益比」,
      而且因為快取是永久的,之後永遠不會再重抓 —— 分析會建立在殘缺資料上而不自知。
      (實際踩到:一次抓 14 檔時被限流,1216/3661 整段 PE 變成 0 筆。)
    """


def _fetch_pe_month(stock_id: str, year: int, month: int) -> list[dict]:
    """抓某月的每日本益比 → [{date, pe}]。

    回傳 []  = **確定**該月無資料(尚未上市/停牌/TWSE 明確回覆查無)→ 可安全快取。
    拋例外   = 抓取失敗(限流、網路、格式非預期)→ **不可快取**,留待重試。

    重點:TWSE 欄位順序會隨年代改變,一定要用「欄名」定位,不能用固定索引。
    PE 為 '-' 或 <=0(EPS 為負或無資料)一律濾掉 → 這些日子「沒有有效 PE」。
    """
    import requests

    url = f"{_TWSE_BWIBBU}?date={year}{month:02d}01&stockNo={stock_id}&response=json"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        r.raise_for_status()
        j = r.json()
    except Exception as e:  # noqa: BLE001 — 網路/HTTP/JSON 問題一律視為「失敗」,不是「無資料」
        raise PEFetchFailed(f"{type(e).__name__}: {e}") from e

    stat = str(j.get("stat") or "")
    if stat != "OK":
        # TWSE 用 stat 訊息表達多種「查詢本身沒問題,但就是沒有資料」的情況。
        # 這些才可以安全地當成「該月無資料」並快取;其餘一律視為抓取失敗要重試。
        # (實測:BWIBBU 最早只到民國 94/09,更早的查詢會回「查詢日期小於…」)
        no_data_marks = ("沒有符合", "無資料", "查無", "查詢日期小於", "日期錯誤")
        if any(k in stat for k in no_data_marks):
            return []
        raise PEFetchFailed(f"TWSE stat={stat!r}")

    fields = j.get("fields") or []
    if "本益比" not in fields:
        # 早年(約 2010 前)部分月份不提供本益比欄位 —— 這是資料本身的限制,屬「無資料」
        return []
    idx = fields.index("本益比")
    out: list[dict] = []
    for row in j.get("data") or []:
        iso = _roc_to_iso(str(row[0]))
        if not iso:
            continue
        try:
            v = float(str(row[idx]).replace(",", "").strip())
        except (ValueError, IndexError):
            continue
        if v > 0:
            out.append({"date": iso, "pe": v})
    return out


def fetch_pe_daily_tw(stock_id: str, polite_sleep: float = 0.5,
                      retries: int = 3) -> list[dict]:
    """逐月抓 TWSE 每日本益比,合併成 [{date, pe}](由舊到新)。

    過去月份永久快取;當月 6 小時快取。
    ★ 只有「成功抓到」或「確定無資料」才寫入快取;抓取失敗**不快取**,
      下次執行會自動重試(可續跑)。失敗月數會記在 LAST_FETCH_STATS 供上層檢查。
    """
    today = time.localtime()
    cur_y, cur_m = today.tm_year, today.tm_mon
    rows: list[dict] = []
    n_fail = 0
    for y in range(P.PE_FETCH_START_YEAR, cur_y + 1):
        for m in range(1, 13):
            if y == cur_y and m > cur_m:
                break
            # TWSE BWIBBU 最早只到民國 94 年 9 月(2005-09);更早的月份不必浪費請求
            if (y, m) < (2005, 9):
                continue
            is_current = (y == cur_y and m == cur_m)
            key = f"pe_tw_{stock_id}_{y}{m:02d}"
            cached = cache_get(key, ttl_seconds=(6 * 3600 if is_current else None))
            if cached is not None:
                rows.extend(cached["data"])
                continue

            month_rows = None
            for attempt in range(retries):
                try:
                    month_rows = _fetch_pe_month(stock_id, y, m)
                    break
                except PEFetchFailed:
                    if attempt < retries - 1:
                        time.sleep(1.5 * (attempt + 1))   # 退避後重試(多半是限流)
            if month_rows is None:          # 重試用盡仍失敗 → 不快取,留待下次
                n_fail += 1
            else:
                cache_set(key, month_rows)
                rows.extend(month_rows)
            time.sleep(polite_sleep)        # 對 TWSE 禮貌節流

    LAST_FETCH_STATS[stock_id] = {"failed_months": n_fail}
    rows.sort(key=lambda x: x["date"])
    # 去重(同日期只留一筆)
    dedup: dict[str, float] = {}
    for r in rows:
        dedup[r["date"]] = r["pe"]
    return [{"date": d, "pe": dedup[d]} for d in sorted(dedup)]


# ─────────────────────────────────────────────────────────────────────
# 2) FinMind 綜合損益表 —— 季 EPS + 毛利率
# ─────────────────────────────────────────────────────────────────────
_DL = None


def _loader():
    """FinMind DataLoader 單例(只登入一次)。token 讀自環境或上層專案 .env。"""
    global _DL
    if _DL is not None:
        return _DL
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        env = Path(__file__).resolve().parent.parent / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("FINMIND_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    from FinMind.data import DataLoader

    dl = DataLoader()
    if token:
        try:
            dl.login_by_token(api_token=token)
        except Exception:  # noqa: BLE001 — token 失效就退回匿名(額度較低)
            pass
    _DL = dl
    return _DL


def _is_quota(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in ("upper limit", "402", "too many", "reach the upper", "額度"))


def tw_filing_available_date(quarter_end: str) -> str:
    """台股財報法定申報期限 = 保守的「可交易日」。

    Q1(3/31)→5/15;Q2(6/30)→8/14;Q3(9/30)→11/14;Q4(12/31)→隔年3/31。
    用法定期限(而非實際公布日)是保守做法:真實公布只會更早,不會更晚,
    因此不可能發生「用了還沒公開的資訊」。
    """
    y, m = int(quarter_end[:4]), int(quarter_end[5:7])
    md = P.TW_FILING_DEADLINE[m]
    return f"{y + 1}-{md}" if m == 12 else f"{y}-{md}"


def fetch_financials_tw(stock_id: str, use_parent_cache: bool = True) -> dict:
    """回傳 {季末日期: {Revenue, GrossProfit, EPS}}。撞額度丟 QuotaExceeded。

    取得順序:
      1. 本專案快取(永久;財報過去值不會變)
      2. 直接向 FinMind 抓 TW_FS_START 起的完整歷史
      3. 抓失敗(額度用盡)才退回上層 Stock_analyze/cache 的 finmind_fs_long_<id>.json

    ⚠ 為什麼把上層快取放到最後?
       上層專案的抓取起點由它自己的設定決定(實測只有近 8 年),
       直接沿用會讓回測期間平白少掉好幾年。所以優先自己抓完整區間,
       只有在額度用盡時才退而求其次(並在報告標明實際起訖)。
    """
    key = f"fs_tw_{stock_id}"
    cached = cache_get(key)
    if cached is not None:
        return cached["data"]

    quota_err: Exception | None = None
    try:
        dl = _loader()
        df = dl.taiwan_stock_financial_statement(stock_id=stock_id, start_date=P.TW_FS_START)
        if df is not None and len(df) > 0:
            piv: dict[str, dict[str, float]] = {}
            for _, r in df.iterrows():
                try:
                    piv.setdefault(str(r["date"]), {})[str(r["type"])] = float(r["value"])
                except (TypeError, ValueError):
                    continue
            if piv:
                cache_set(key, piv)
                return piv
    except Exception as e:  # noqa: BLE001
        if _is_quota(str(e)):
            quota_err = QuotaExceeded(str(e))
        else:
            quota_err = e

    # 退路:沿用上層專案已抓好的同源資料(範圍可能較短,報告會標明實際起訖)
    if use_parent_cache:
        parent = Path(__file__).resolve().parent.parent / "cache" / f"finmind_fs_long_{stock_id}.json"
        if parent.exists():
            try:
                import json

                piv = json.loads(parent.read_text(encoding="utf-8"))["data"]
                if isinstance(piv, dict) and piv:
                    cache_set(key, piv)
                    return piv
            except (json.JSONDecodeError, OSError, KeyError):
                pass

    if quota_err is not None:
        raise quota_err
    raise RuntimeError(f"FinMind 未回傳 {stock_id} 財報")


def quarterly_fundamentals_tw(stock_id: str) -> list[dict]:
    """把 FinMind 損益表整理成回測用的季序列(由舊到新)。

    每季輸出:{quarter_end, available_date, eps, gross_margin}
      eps          = 單季 EPS(FinMind 'EPS',已是單季數)
      gross_margin = GrossProfit / Revenue × 100(單季毛利率)
      available_date = 法定申報期限(保守可用日)
    """
    piv = fetch_financials_tw(stock_id)
    rows: list[dict] = []
    for qend in sorted(piv):
        t = piv[qend]
        rev = t.get("Revenue")
        gp = t.get("GrossProfit")
        eps = t.get("EPS")
        if eps is None or not rev:
            continue
        rows.append({
            "quarter_end": qend,
            "available_date": tw_filing_available_date(qend),
            "eps": float(eps),
            "gross_margin": round(gp / rev * 100.0, 3) if gp is not None else None,
            "eps_source": "actual",  # 台股免費源無分析師共識 → 只有實際值
        })
    return rows
