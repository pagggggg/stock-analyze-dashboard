"""
本益比河流圖資料 (river.py)
============================
把「長區間日股價」+「長區間每季 EPS」+「逐月 rolling N 年本益比分位」
組成『本益比河流圖』要用的月頻序列:

    河道三條線 = TTM EPS(當月) × 當月當時可得的 rolling {P10,P50,P90}
    股價線                             = 每月收盤
    現價標記                           = 最新一筆收盤

判讀:股價線落在 P10 附近=相對便宜、貼近或超過 P90=相對貴。
因為河道用『當時的 TTM EPS』抬升,所以看的是「相對歷史估值位階」,不是絕對股價。

口徑:河道與現價PE 都採 **TTM(過去4季實際 EPS)**；
      FinMind 無公告日欄位，財報生效日使用法定申報期限 fallback；
      歷史每月只用當時以前資料，避免把今天知道的分位套回過去。
      這和報告第五節的『前瞻PE(含本季試算)』略有差別,屬正常(前瞻通常較低)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .models import PEBand


def supports_tw_filing_fallback(name: str) -> bool:
    """The fixed domestic deadlines are not asserted for foreign/KY issuers."""
    normalized = (name or "").upper()
    return "-KY" not in normalized and not normalized.endswith("KY")


@dataclass
class RiverSeries:
    """河流圖用的月頻序列 + 現價標記。"""

    dates: list[str]          # 已完成月份的月末交易日
    price: list[float]        # 對應月收盤價
    band_low: list[float]     # 低本益比河道 = TTM EPS × pe_low
    band_mid: list[float]     # 中本益比河道 = TTM EPS × pe_mid
    band_high: list[float]    # 高本益比河道 = TTM EPS × pe_high
    pe_low: float
    pe_mid: float
    pe_high: float
    current_date: str         # 現價日期
    current_price: float      # 現價
    current_pe: float | None  # 現價 ÷ 最新 TTM EPS(trailing PE)
    source: str = ""
    currency: str = "TWD"


def _filing_available_date(qend: date) -> date:
    """台灣本國、曆年制發行人的保守可用日 fallback。

    FinMind 財報沒有實際公告日欄位，故採法定申報期限：Q1 5/15、Q2 8/14、
    Q3 11/14、Q4 次年 3/31。KY/外國發行人由呼叫端排除，不套用此假設。
    """
    if qend.month == 3:
        deadline = date(qend.year, 5, 15)
    elif qend.month == 6:
        deadline = date(qend.year, 8, 14)
    elif qend.month == 9:
        deadline = date(qend.year, 11, 14)
    elif qend.month == 12:
        deadline = date(qend.year + 1, 3, 31)
    else:
        raise ValueError(f"非標準季末:{qend.isoformat()}")
    # 若名目期限落在週末，先順延至下一工作日；再從下一工作日視為可用。
    # 這仍不是交易所假日日曆，因此在頁面與 schema 明示為 fallback。
    while deadline.weekday() >= 5:
        deadline += timedelta(days=1)
    available = deadline + timedelta(days=1)
    while available.weekday() >= 5:
        available += timedelta(days=1)
    return available


def _quarter_index(d: str) -> int | None:
    qend = date.fromisoformat(d)
    if qend.month not in (3, 6, 9, 12):
        return None
    return qend.year * 4 + qend.month // 3 - 1


def _next_quarter_end(qend: date) -> date:
    if qend.month == 3:
        return date(qend.year, 6, 30)
    if qend.month == 6:
        return date(qend.year, 9, 30)
    if qend.month == 9:
        return date(qend.year, 12, 31)
    if qend.month == 12:
        return date(qend.year + 1, 3, 31)
    raise ValueError(f"非標準季末:{qend.isoformat()}")


def _ttm_series(income_pivot: dict,
                filing_fallback_supported: bool = True) -> list[tuple[date, date, float]]:
    """由每季 EPS 累計 TTM；生效日使用法定申報期限 fallback。"""
    if not filing_fallback_supported:
        return []
    items: list[tuple[str, float]] = []
    for d, t in income_pivot.items():
        eps = t.get("EPS")
        if eps is not None:
            try:
                items.append((d, float(eps)))
            except (TypeError, ValueError):
                continue
    items.sort(key=lambda x: x[0])

    out: list[tuple[date, date, float]] = []
    for i in range(3, len(items)):                 # 要湊滿 4 季才有 TTM
        window = items[i - 3:i + 1]
        qidx = [_quarter_index(d) for d, _ in window]
        if any(x is None for x in qidx) or any(qidx[j] != qidx[0] + j for j in range(4)):
            continue                                # 缺季不能把跨五季的四筆資料冒充 TTM
        ttm = sum(e for _, e in window)
        qend = date.fromisoformat(items[i][0])
        out.append((_filing_available_date(qend),
                    _filing_available_date(_next_quarter_end(qend)), round(ttm, 4)))
    return out


def _monthly_price(price_rows: list[dict], exclude_open_month: bool = False) -> list[tuple[str, float]]:
    """日收盤 → 月頻。可排除仍在進行中的最新月份。"""
    by_month: dict[str, dict] = {}
    for r in sorted(price_rows, key=lambda x: x["date"]):
        by_month[r["date"][:7]] = r               # 同月後者覆蓋 → 留最後一筆
    months = sorted(by_month)
    if exclude_open_month and months and months[-1] == date.today().strftime("%Y-%m"):
        # 當月尚在進行中時只由紅點呈現；已跨月的最後一筆則是完整月末，不刪除。
        months.pop()
    return [(by_month[k]["date"], by_month[k]["close"]) for k in months]


def _ttm_asof(ttm_series: list[tuple[date, date, float]], d: date) -> float | None:
    """取當日有效的 TTM EPS；下一季應申報日到達後不可再沿用舊值。"""
    val = None
    for eff, expires, ttm in ttm_series:           # 已按生效日排序
        if eff <= d < expires:
            val = ttm
        elif eff > d:
            break
    return val


def build_pe_river(
    price_rows: list[dict],
    income_pivot: dict,
    current_price: float | None = None,
    current_date: str | None = None,
    years: int = 5,
    filing_fallback_supported: bool = True,
) -> RiverSeries:
    """組出河流圖月頻序列。缺 EPS 或股價會 raise,由上層決定略過此圖。"""
    if not filing_fallback_supported:
        raise ValueError("河流圖不對 KY/外國發行人套用本國法定申報期限")
    ttm = _ttm_series(income_pivot)
    if not ttm or not price_rows:
        raise ValueError("河流圖資料不足(缺 EPS 或股價序列)")

    # 黑色股價線只畫完整月末；未完成月份僅用紅色最新點顯示。
    monthly = _monthly_price(price_rows, exclude_open_month=True)
    daily_pe = daily_pe_series(price_rows, income_pivot)
    first_pe_date = date.fromisoformat(daily_pe[0][0]) if daily_pe else None
    pts: list[tuple[str, float, float, float, float, float]] = []
    for dstr, close in monthly:
        e = _ttm_asof(ttm, date.fromisoformat(dstr))
        d = date.fromisoformat(dstr)
        cutoff = _shift_years(d, -years)
        vals = sorted(pe for pd, pe in daily_pe if cutoff < date.fromisoformat(pd) <= d)
        # 逐月 rolling N 年必須真的有完整 N 年歷史；不足時不拿較短視窗冒充。
        min_samples = max(252, int(years * 252 * 0.60))
        if (e is None or e <= 0 or len(vals) < min_samples
                or first_pe_date is None or first_pe_date > cutoff + timedelta(days=7)):
            continue
        pts.append((dstr, close, e, _percentile(vals, .10),
                    _percentile(vals, .50), _percentile(vals, .90)))

    if not pts:
        raise ValueError("河流圖:股價與 EPS 沒有重疊區間")

    latest = max(price_rows, key=lambda x: x["date"])
    cp = float(current_price) if current_price is not None else float(latest["close"])
    cd = current_date or latest["date"]
    current_ttm = _ttm_asof(ttm, date.fromisoformat(cd))
    cpe = cp / current_ttm if current_ttm and current_ttm > 0 else None

    # 圖例與區間判斷直接由目前日期重算，不沿用呼叫端可能是其他口徑的 PEBand。
    current_cutoff = _shift_years(date.fromisoformat(cd), -years)
    current_vals = sorted(pe for pd, pe in daily_pe
                          if current_cutoff < date.fromisoformat(pd) <= date.fromisoformat(cd))
    if cpe is not None and not any(pd == cd for pd, _ in daily_pe):
        current_vals.append(cpe)
        current_vals.sort()
    min_samples = max(252, int(years * 252 * 0.60))
    if (first_pe_date is None or first_pe_date > current_cutoff + timedelta(days=7)
            or len(current_vals) < min_samples):
        raise ValueError(f"河流圖:有效 PE 歷史不足完整 rolling {years} 年")
    pe_lo = _percentile(current_vals, .10)
    pe_mid = _percentile(current_vals, .50)
    pe_hi = _percentile(current_vals, .90)
    source = (f"FinMind 收盤÷近4季 basic EPS；截至 {cd} rolling {years}年 "
              "P10/P50/P90；本國發行人法定申報期限 fallback；latest-restated")

    # 第二遍:用原始歷史分位畫河道三線
    dates: list[str] = []
    price: list[float] = []
    lo: list[float] = []
    mid: list[float] = []
    hi: list[float] = []
    for dstr, close, e, rolling_lo, rolling_mid, rolling_hi in pts:
        dates.append(dstr)
        price.append(round(close, 1))
        lo.append(round(e * rolling_lo, 1))
        mid.append(round(e * rolling_mid, 1))
        hi.append(round(e * rolling_hi, 1))

    return RiverSeries(
        dates=dates, price=price, band_low=lo, band_mid=mid, band_high=hi,
        pe_low=round(pe_lo, 1), pe_mid=round(pe_mid, 1), pe_high=round(pe_hi, 1),
        current_date=cd, current_price=round(cp, 1),
        current_pe=round(cpe, 1) if cpe else None, source=source,
    )


def build_pe_river_us(hist, earnings_dates=None, years: int = 5,
                      eps_events: list[tuple[date, float]] | None = None,
                      fx_series: list[tuple[date, float]] | None = None,
                      source_note: str = "") -> RiverSeries:
    """Yahoo 美股河道：拆股調整 Close ÷ 實際公告日可得的四季 Reported EPS。"""
    from .valuation_flag import pe_series_us

    if hist is None or not len(hist):
        raise ValueError("美股河流圖缺股價序列")
    daily_pe = pe_series_us(hist, earnings_dates, years=years, eps_events=eps_events,
                            fx_series=fx_series, release_time_aware=True)
    if not daily_pe:
        raise ValueError("美股河流圖缺可用 Reported EPS")
    pe_by_date = {d: pe for d, pe in daily_pe}
    price_rows = []
    for ts, row in hist.sort_index().iterrows():
        try:
            close = float(row["Close"])
        except (KeyError, TypeError, ValueError):
            continue
        if close > 0:
            price_rows.append({"date": ts.date().isoformat(), "close": close})
    monthly = _monthly_price(price_rows, exclude_open_month=True)
    first_pe_date = date.fromisoformat(daily_pe[0][0])
    pe_dates = [date.fromisoformat(d) for d, _ in daily_pe]
    pe_values = [pe for _, pe in daily_pe]
    pts = []
    for dstr, close in monthly:
        d = date.fromisoformat(dstr)
        current_pe = pe_by_date.get(dstr)
        if current_pe is None:
            continue
        ttm = close / current_pe
        cutoff = _shift_years(d, -years)
        vals = sorted(pe for pd, pe in zip(pe_dates, pe_values) if cutoff < pd <= d)
        min_samples = max(252, int(years * 252 * 0.60))
        if (ttm <= 0 or len(vals) < min_samples
                or first_pe_date > cutoff + timedelta(days=7)):
            continue
        pts.append((dstr, close, ttm, _percentile(vals, .10),
                    _percentile(vals, .50), _percentile(vals, .90)))
    if not pts:
        raise ValueError("美股河流圖:不足完整 rolling 歷史")
    # 與台股頁一致只呈現最近 rolling 視窗；更早資料僅供每月分位暖機。
    display_start = _shift_years(date.fromisoformat(price_rows[-1]["date"]), -years)
    pts = [x for x in pts if date.fromisoformat(x[0]) >= display_start]
    if not pts:
        raise ValueError("美股河流圖:最近五年月頻資料不足")

    latest = price_rows[-1]
    cd, cp = latest["date"], latest["close"]
    cpe = pe_by_date.get(cd)
    cutoff = _shift_years(date.fromisoformat(cd), -years)
    current_vals = sorted(pe for pd, pe in zip(pe_dates, pe_values)
                          if cutoff < pd <= date.fromisoformat(cd))
    min_samples = max(252, int(years * 252 * 0.60))
    if (cpe is None or first_pe_date > cutoff + timedelta(days=7)
            or len(current_vals) < min_samples):
        raise ValueError(f"美股河流圖:有效 PE 歷史不足完整 rolling {years} 年")
    pe_lo = _percentile(current_vals, .10)
    pe_mid = _percentile(current_vals, .50)
    pe_hi = _percentile(current_vals, .90)
    source = (f"Yahoo Close（拆股調整、不含股息）÷首個可交易收盤日起可得的四季 Reported EPS；"
              f"截至 {cd} rolling {years}年 P10/P50/P90")
    if source_note:
        source += f"；{source_note}"
    return RiverSeries(
        dates=[x[0] for x in pts],
        price=[round(x[1], 2) for x in pts],
        band_low=[round(x[2] * x[3], 2) for x in pts],
        band_mid=[round(x[2] * x[4], 2) for x in pts],
        band_high=[round(x[2] * x[5], 2) for x in pts],
        pe_low=round(pe_lo, 1), pe_mid=round(pe_mid, 1), pe_high=round(pe_hi, 1),
        current_date=cd, current_price=round(cp, 2), current_pe=round(cpe, 1),
        source=source, currency="USD",
    )


# ======================================================================
# 由 FinMind 自算本益比區間(多股掃描用,免逐月打 TWSE)
# ----------------------------------------------------------------------
# 每日本益比 = 當日收盤 ÷ 當時近四季(TTM)EPS,再取近 N 年的百分位:
#     低 = P10、中 = P50(中位數)、高 = P90
# 用百分位而非 min/max,避免財報空窗期 EPS 偏低造成的單日爆量把區間拉歪;
# 這和單股報告用 TWSE 官方本益比(min/mean/max)略有口徑差異,但可跨股一致比較。
# ======================================================================
def daily_pe_series(price_rows: list[dict], income_pivot: dict,
                    filing_fallback_supported: bool = True) -> list[tuple[str, float]]:
    """回傳 [(date, pe)] 每日本益比(收盤 ÷ TTM EPS)；非正值才排除。"""
    ttm = _ttm_series(income_pivot, filing_fallback_supported)
    if not ttm:
        return []
    out: list[tuple[str, float]] = []
    for r in sorted(price_rows, key=lambda x: x["date"]):
        e = _ttm_asof(ttm, date.fromisoformat(r["date"]))
        if e and e > 0:
            pe = r["close"] / e
            if 0 < pe < float("inf"):             # P10/P50/P90 自身可抵抗少數極端值
                out.append((r["date"], round(pe, 3)))
    return out


def current_trailing_pe(price_rows: list[dict], income_pivot: dict,
                        filing_fallback_supported: bool = True,
                        current_price: float | None = None,
                        current_date: str | None = None) -> tuple[float | None, str | None]:
    """最新價格對當時可得 TTM EPS；不沿用歷史最後一個有效 PE。"""
    if not price_rows:
        return None, None
    latest = max(price_rows, key=lambda x: x["date"])
    dstr = current_date or latest["date"]
    close = current_price if current_price is not None else latest.get("close")
    eps = _ttm_asof(_ttm_series(income_pivot, filing_fallback_supported),
                    date.fromisoformat(dstr))
    if eps is None or eps <= 0 or close is None or close <= 0:
        return None, dstr
    return close / eps, dstr


def _percentile(sorted_vals: list[float], p: float) -> float:
    """線性內插百分位(p 為 0~1)。"""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _shift_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def compute_pe_band_finmind(
    price_rows: list[dict], income_pivot: dict, years: int = 10, fetched_date: str = "",
    filing_fallback_supported: bool = True,
) -> PEBand:
    """由 FinMind 股價 + EPS 自算近 N 年本益比區間(P10/P50/P90)。缺料會 raise。"""
    if not filing_fallback_supported:
        raise ValueError("不對 KY/外國發行人套用本國法定申報期限")
    series = daily_pe_series(price_rows, income_pivot)
    if not series:
        raise ValueError("無法由 FinMind 計算本益比(缺 EPS 或股價序列)")
    if not price_rows:
        raise ValueError("缺股價序列")
    as_of = date.fromisoformat(max(r["date"] for r in price_rows))
    cutoff = _shift_years(as_of, -years)
    recent = [(d, pe) for d, pe in series if cutoff < date.fromisoformat(d) <= as_of]
    min_samples = max(252, int(years * 252 * 0.60))
    if (not recent or date.fromisoformat(series[0][0]) > cutoff + timedelta(days=7)
            or len(recent) < min_samples):
        raise ValueError(f"有效 PE 歷史不足完整 rolling {years} 年")
    vals = sorted(pe for _, pe in recent)
    lo, mid, hi = _percentile(vals, 0.10), _percentile(vals, 0.50), _percentile(vals, 0.90)
    src = (f"FinMind 每日本益比(收盤÷近4季 basic EPS,截至{as_of.isoformat()}"
           f" rolling {years}年 P10/P50/P90；本國發行人法定期限 fallback；latest-restated)")
    if fetched_date:
        src += f" 抓取 {fetched_date}"
    return PEBand(
        pe_low=round(lo, 1), pe_mid=round(mid, 1), pe_high=round(hi, 1),
        years_covered=f"{recent[0][0]}–{recent[-1][0]},rolling {years} 年",
        source=src,
    )
