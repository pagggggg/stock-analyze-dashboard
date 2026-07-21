"""
本益比河流圖資料 (river.py)
============================
把「長區間日股價」+「長區間每季 EPS」+「近10年本益比區間(低/中/高)」
組成『本益比河流圖』要用的月頻序列:

    河道三條線(隨 TTM EPS 成長而抬升) = TTM EPS(當月) × {pe_low, pe_mid, pe_high}
    股價線                             = 每月收盤
    現價標記                           = 最新一筆收盤

判讀:股價線落在「低本益比河道」附近=相對便宜、貼「高本益比河道」=相對貴。
因為河道用『當時的 TTM EPS』抬升,所以看的是「相對歷史估值位階」,不是絕對股價。

口徑:河道與現價PE 都採 **TTM(過去4季實際 EPS)**,和 TWSE 歷史本益比同口徑;
      這和報告第五節的『前瞻PE(含本季試算)』略有差別,屬正常(前瞻通常較低)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .models import PEBand

# 財報約在季末後才公布 → 河道用的 TTM EPS「生效日」順延這麼多天,
# 避免把「還沒公布的 EPS」畫到過去的股價上(前視偏誤 look-ahead bias)。
_REPORT_LAG_DAYS = 45


@dataclass
class RiverSeries:
    """河流圖用的月頻序列 + 現價標記。"""

    dates: list[str]          # 月頻 x 軸(每月最後交易日 'YYYY-MM-DD')
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


def _ttm_series(income_pivot: dict) -> list[tuple[date, float]]:
    """由每季 EPS 累計出 TTM(滾動4季)EPS,回傳 [(生效日, ttm_eps)] 已排序。"""
    items: list[tuple[str, float]] = []
    for d, t in income_pivot.items():
        eps = t.get("EPS")
        if eps is not None:
            try:
                items.append((d, float(eps)))
            except (TypeError, ValueError):
                continue
    items.sort(key=lambda x: x[0])

    out: list[tuple[date, float]] = []
    for i in range(3, len(items)):                 # 要湊滿 4 季才有 TTM
        ttm = sum(e for _, e in items[i - 3:i + 1])
        qend = date.fromisoformat(items[i][0])
        out.append((qend + timedelta(days=_REPORT_LAG_DAYS), round(ttm, 4)))
    return out


def _monthly_price(price_rows: list[dict]) -> list[tuple[str, float]]:
    """日收盤 → 月頻(取每月最後一個交易日)。回傳 [(date_str, close)]。"""
    by_month: dict[str, dict] = {}
    for r in sorted(price_rows, key=lambda x: x["date"]):
        by_month[r["date"][:7]] = r               # 同月後者覆蓋 → 留最後一筆
    return [(by_month[k]["date"], by_month[k]["close"]) for k in sorted(by_month)]


def _ttm_asof(ttm_series: list[tuple[date, float]], d: date) -> float | None:
    """取『生效日 <= d』的最後一個 TTM EPS(step 函數,前向填補)。"""
    val = None
    for eff, ttm in ttm_series:                    # 已按生效日排序
        if eff <= d:
            val = ttm
        else:
            break
    return val


def build_pe_river(
    price_rows: list[dict],
    income_pivot: dict,
    pe_band: PEBand,
    current_price: float | None = None,
    current_date: str | None = None,
) -> RiverSeries:
    """組出河流圖月頻序列。缺 EPS 或股價會 raise,由上層決定略過此圖。"""
    ttm = _ttm_series(income_pivot)
    if not ttm or not price_rows:
        raise ValueError("河流圖資料不足(缺 EPS 或股價序列)")

    # 第一遍:算每月 TTM EPS 與「當月隱含本益比」= 月收盤 / TTM EPS
    monthly = _monthly_price(price_rows)
    pts: list[tuple[str, float, float]] = []   # (date, close, ttm_eps)
    implied: list[float] = []
    for dstr, close in monthly:
        e = _ttm_asof(ttm, date.fromisoformat(dstr))
        if e is None or e <= 0:                    # 早於第一份 TTM 的月份跳過
            continue
        pts.append((dstr, close, e))
        implied.append(close / e)

    if not pts:
        raise ValueError("河流圖:股價與 EPS 沒有重疊區間")

    last_ttm = ttm[-1][1]
    cp = float(current_price) if current_price else pts[-1][1]
    cd = current_date or pts[-1][0]
    cpe = cp / last_ttm if last_ttm else None

    # 河道邊界:以「近N年本益比區間(pe_band,如 P10/P50/P90)」為底,
    # 但必要時往外擴張,確保『所有畫出來的股價點 + 現價』都落在河道內——
    # 這樣股價線永遠貼著河道跑,不會像先前那樣衝出上緣(現價 PE 超過 P90 時)。
    pool = implied + ([cpe] if cpe else [])
    pe_lo = min(pe_band.pe_low, min(pool))
    pe_hi = max(pe_band.pe_high, max(pool))
    pe_mid = min(max(pe_band.pe_mid, pe_lo), pe_hi)   # 中線夾在上下緣之間

    # 第二遍:用(可能擴張後的)本益比畫河道三線
    dates: list[str] = []
    price: list[float] = []
    lo: list[float] = []
    mid: list[float] = []
    hi: list[float] = []
    for dstr, close, e in pts:
        dates.append(dstr)
        price.append(round(close, 1))
        lo.append(round(e * pe_lo, 1))
        mid.append(round(e * pe_mid, 1))
        hi.append(round(e * pe_hi, 1))

    return RiverSeries(
        dates=dates, price=price, band_low=lo, band_mid=mid, band_high=hi,
        pe_low=round(pe_lo, 1), pe_mid=round(pe_mid, 1), pe_high=round(pe_hi, 1),
        current_date=cd, current_price=round(cp, 1),
        current_pe=round(cpe, 1) if cpe else None, source=pe_band.source,
    )


# ======================================================================
# 由 FinMind 自算本益比區間(多股掃描用,免逐月打 TWSE)
# ----------------------------------------------------------------------
# 每日本益比 = 當日收盤 ÷ 當時近四季(TTM)EPS,再取近 N 年的百分位:
#     低 = P10、中 = P50(中位數)、高 = P90
# 用百分位而非 min/max,避免財報空窗期 EPS 偏低造成的單日爆量把區間拉歪;
# 這和單股報告用 TWSE 官方本益比(min/mean/max)略有口徑差異,但可跨股一致比較。
# ======================================================================
def daily_pe_series(price_rows: list[dict], income_pivot: dict) -> list[tuple[str, float]]:
    """回傳 [(date, pe)] 每日本益比(收盤 ÷ TTM EPS),已濾極端值。"""
    ttm = _ttm_series(income_pivot)
    if not ttm:
        return []
    out: list[tuple[str, float]] = []
    for r in sorted(price_rows, key=lambda x: x["date"]):
        e = _ttm_asof(ttm, date.fromisoformat(r["date"]))
        if e and e > 0:
            pe = r["close"] / e
            if 0 < pe < 200:                       # 濾掉 EPS 極小造成的離群本益比
                out.append((r["date"], round(pe, 3)))
    return out


def _percentile(sorted_vals: list[float], p: float) -> float:
    """線性內插百分位(p 為 0~1)。"""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def compute_pe_band_finmind(
    price_rows: list[dict], income_pivot: dict, years: int = 10, fetched_date: str = ""
) -> PEBand:
    """由 FinMind 股價 + EPS 自算近 N 年本益比區間(P10/P50/P90)。缺料會 raise。"""
    series = daily_pe_series(price_rows, income_pivot)
    if not series:
        raise ValueError("無法由 FinMind 計算本益比(缺 EPS 或股價序列)")
    cutoff = date.today().year - years + 1
    recent = [(d, pe) for d, pe in series if int(d[:4]) >= cutoff] or series
    vals = sorted(pe for _, pe in recent)
    lo, mid, hi = _percentile(vals, 0.10), _percentile(vals, 0.50), _percentile(vals, 0.90)
    y0 = min(int(d[:4]) for d, _ in recent)
    y1 = max(int(d[:4]) for d, _ in recent)
    src = "FinMind 每日本益比(收盤÷近4季EPS,P10/P50/P90)"
    if fetched_date:
        src += f" 抓取 {fetched_date}"
    return PEBand(
        pe_low=round(lo, 1), pe_mid=round(mid, 1), pe_high=round(hi, 1),
        years_covered=f"{y0}–{y1},共 {y1 - y0 + 1} 年",
        source=src,
    )
