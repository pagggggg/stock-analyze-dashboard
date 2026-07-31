"""
進場規則回測 (entry_rule.py)
============================
測一條「只管進場、不管出場」的規則:

    前瞻PE < 20x  且  歷史PE百分位 < 50%   → 進場,持有 1 / 3 / 5 年不賣

★★ 必須先講清楚的口徑問題(這會實質改變規則,不是小註腳)★★
    題目寫的是「前瞻PE」,但**歷史回測做不出前瞻PE**:
    前瞻PE = 股價 ÷ 分析師對「未來一年」的共識EPS,
    要回測就需要「歷史上每一天的共識EPS」——免費資料源沒有這種東西;
    拿今天的共識去回推過去,就是標準的前視偏誤(look-ahead bias)。
    因此本回測一律用 **trailing PE**(股價 ÷ 近四季『實際』EPS)。

    影響方向(報告會用數據呈現):對獲利成長中的公司,
    未來EPS > 過去EPS ⇒ 前瞻PE < trailing PE。
    所以「trailing PE < 20」比「前瞻PE < 20」**更嚴格**,
    會少抓到一些「前瞻PE 已低於 20 但 trailing PE 還在 20 以上」的成長股買點。
    → 本回測的觸發次數是**偏保守**的下界。

三條規則併陳(回答「兩條件同時成立是不是真的比較好」):
    R1  只用 trailing PE < 20
    R2  只用 歷史PE百分位 < 50%(即 PE 低於自身歷史中位數)
    R3  兩者同時成立

無前視設計:
    - 百分位一律用**擴張視窗**(只用當日以前的 PE 算),暖身 504 個交易日
    - 訊號 T 日成立 → **T+1 日收盤**進場
    - 報酬用還原股利價;PE 用未還原價(與交易所口徑一致)

「觸發」的定義:
    採**邊緣觸發** —— 條件由「不成立」變成「成立」的那一天才算一次。
    否則條件連續成立 300 天會被算成 300 次進場,把次數灌得毫無意義。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import params as P
import sources_tw as TW
from cache import cache_get, cache_set
from prices import PriceSeries, fetch_prices

ROOT = Path(__file__).resolve().parent

# 題目給定,不做最佳化
PE_ABS_MAX = 20.0        # 「PE < 20x」的絕對門檻
PCTL_MAX = 50.0          # 「歷史PE百分位 < 50%」
HOLD_YEARS = (1, 3, 5)   # 持有期
WARMUP = P.WARMUP_TRADING_DAYS   # 沿用主回測:504 個交易日
COOLDOWN_DAYS = 180      # 觸發後的冷卻期(見 find_triggers 說明);併陳 0 天版本作對照

UNIVERSE = [
    {"code": "2330", "name": "台積電", "yf": "2330.TW"},
    {"code": "2308", "name": "台達電", "yf": "2308.TW"},
    {"code": "2454", "name": "聯發科", "yf": "2454.TW"},
    {"code": "2317", "name": "鴻海", "yf": "2317.TW"},
]


# ─────────────────────────────────────────────────────────────────────
# 大盤(報酬指數)—— 環境標記用
# ─────────────────────────────────────────────────────────────────────
def fetch_taiex() -> list[dict]:
    """加權股價『報酬』指數(含息),用來標記進場當時的大盤環境。快取 24h。"""
    cached = cache_get("taiex_total_return", ttl_seconds=24 * 3600)
    if cached is not None:
        return cached["data"]
    import os
    for line in (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from FinMind.data import DataLoader
    dl = DataLoader()
    dl.login_by_token(api_token=os.environ["FINMIND_TOKEN"])
    df = dl.taiwan_stock_total_return_index(index_id="TAIEX", start_date="2005-01-01")
    rows = [{"date": str(r["date"]), "price": float(r["price"])} for _, r in df.iterrows()]
    rows.sort(key=lambda x: x["date"])
    cache_set("taiex_total_return", rows)
    return rows


# ─────────────────────────────────────────────────────────────────────
# 時間軸:每日 PE + 擴張視窗百分位 + 三條規則的成立與否
# ─────────────────────────────────────────────────────────────────────
def _percentile_rank(sorted_vals: list[float], x: float) -> float:
    """x 在歷史樣本中的百分位(有多少比例的歷史值低於 x)。"""
    if not sorted_vals:
        return float("nan")
    import bisect
    return bisect.bisect_left(sorted_vals, x) / len(sorted_vals) * 100.0


def build_timeline(code: str, yf_ticker: str) -> list[dict]:
    """每日:{date, close_raw, close_adj, pe, pctl, r1, r2, r3, tradable}。"""
    px_rows = fetch_prices(yf_ticker)
    px = PriceSeries(px_rows)
    pe_daily = {r["date"]: r["pe"] for r in TW.fetch_pe_daily_tw(code)}

    import bisect
    seen: list[float] = []          # 已排序的歷史 PE(擴張視窗)
    out: list[dict] = []
    for i, d in enumerate(px.dates):
        pe = pe_daily.get(d)
        pctl = None
        tradable = False
        if pe is not None and len(seen) >= WARMUP:
            pctl = _percentile_rank(seen, pe)
            tradable = True
        r1 = bool(tradable and pe is not None and pe < PE_ABS_MAX)
        r2 = bool(tradable and pctl is not None and pctl < PCTL_MAX)
        out.append({
            "date": d, "close_raw": px.raw[i], "close_adj": px.adj[i],
            "pe": pe, "pctl": pctl, "tradable": tradable,
            "r1": r1, "r2": r2, "r3": bool(r1 and r2),
        })
        # 當日結束後才把今天的 PE 併入歷史(明天才看得到 → 杜絕前視)
        if pe is not None:
            bisect.insort(seen, pe)
    return out


# ─────────────────────────────────────────────────────────────────────
# 邊緣觸發 + 固定持有期報酬
# ─────────────────────────────────────────────────────────────────────
def _fwd_return(rows: list[dict], i: int, years: int) -> tuple[float | None, str | None, bool]:
    """從第 i 日(進場日)持有 N 年的報酬。回傳 (報酬, 出場日, 是否已到期)。"""
    d0 = date.fromisoformat(rows[i]["date"])
    try:
        target = d0.replace(year=d0.year + years)
    except ValueError:                       # 2/29
        target = d0.replace(year=d0.year + years, day=28)
    tgt = target.isoformat()
    for j in range(i + 1, len(rows)):
        if rows[j]["date"] >= tgt:
            return rows[j]["close_adj"] / rows[i]["close_adj"] - 1.0, rows[j]["date"], True
    # 還沒到期:用最後一天算「目前為止」的報酬,但標記未到期
    if len(rows) - 1 > i:
        return rows[-1]["close_adj"] / rows[i]["close_adj"] - 1.0, rows[-1]["date"], False
    return None, None, False


def find_triggers(rows: list[dict], key: str, lag: int = 1,
                  cooldown_days: int = 0) -> list[dict]:
    """邊緣觸發:條件由 False→True 當天算一次,T+lag 日收盤進場。

    cooldown_days:觸發後這段期間內不再觸發(0 = 不設冷卻)。

    ★ 為什麼需要冷卻期(實測發現的問題):
      單純的邊緣觸發會把「同一段低估期」重複計算 —— PE 在門檻附近上下震盪,
      每穿越一次就算一次新買點。實測台積電:條件只在 30% 的交易日成立,
      卻產生 52 次「觸發」,顯然不是 52 個獨立的投資機會。
      這會讓後面的「平均報酬/勝率」看起來樣本很多,實際上高度重複、彼此不獨立,
      統計意義被嚴重高估。
      加上冷卻期後,一段低估期只算一次進場,才接近真實可執行的決策次數。
      180 天(約半年、跨兩次季報)是結構性選擇,不是為了讓績效好看而調的參數;
      報告會把「無冷卻」與「180天冷卻」兩種數字併陳,讓你自己看差異。
    """
    trig: list[dict] = []
    prev = False
    last_entry: date | None = None
    for i, r in enumerate(rows):
        now = r[key]
        if now and not prev and i + lag < len(rows):
            d = date.fromisoformat(rows[i + lag]["date"])
            if last_entry is not None and cooldown_days > 0 \
                    and (d - last_entry).days < cooldown_days:
                prev = now
                continue
            e = i + lag
            item = {
                "signal_date": r["date"],
                "entry_date": rows[e]["date"],
                "entry_price": rows[e]["close_adj"],
                "entry_price_raw": rows[e]["close_raw"],
                "pe": r["pe"], "pctl": r["pctl"], "idx": e,
            }
            for y in HOLD_YEARS:
                ret, exit_d, matured = _fwd_return(rows, e, y)
                item[f"ret_{y}y"] = ret
                item[f"exit_{y}y"] = exit_d
                item[f"matured_{y}y"] = matured
            trig.append(item)
            last_entry = d
        prev = now
    return trig


def condition_days(rows: list[dict], key: str) -> dict:
    """條件成立的天數佔比、最長「等待期」(條件不成立的連續天數)。"""
    period = [r for r in rows if r["tradable"]]
    if not period:
        return {"n_days": 0}
    on = sum(1 for r in period if r[key])
    longest, cur, start, longest_span = 0, 0, None, (None, None)
    for r in period:
        if not r[key]:
            if cur == 0:
                start = r["date"]
            cur += 1
            if cur > longest:
                longest, longest_span = cur, (start, r["date"])
        else:
            cur = 0
    return {
        "n_days": len(period),
        "on_days": on,
        "on_pct": on / len(period) * 100,
        "off_pct": (1 - on / len(period)) * 100,
        "longest_wait_days": longest,
        "longest_wait_span": longest_span,
        "first_tradable": period[0]["date"],
        "last": period[-1]["date"],
    }
