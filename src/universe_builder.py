"""
可分析母體建構 (universe_builder.py)
====================================
產出所有後續篩選的「基礎池」:只保留『有分析師覆蓋、有法說會、資訊揭露充分』的中大型股,
認知圈外的標的一律排除。母體會存成 config/universe.yaml,供篩選器讀取。

台股 4 條(門檻見 config screener.yaml → universe_builder.tw):
  ① 市值 > 300 億台幣            (yfinance marketCap)
  ② 有分析師共識,至少 3 家覆蓋  (yfinance earnings_estimate numberOfAnalysts)
  ③ 近一年有召開法人說明會       (公開資訊觀測站 MOPS 法說會一覽表)
  ④ 近60日日均成交額 > 1 億      (yfinance 收盤×成交量)

美股 4 條(universe_builder.us):
  ① 市值 > 20 億美元  ② 分析師 ≥ 5 家  ③ 有季度法說(以有季度共識為代理)  ④ 日均額 > 2000萬美元

誠實原則:任一條件資料缺失無法判斷 → 標 na(資料不足),一律不算通過。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .cache import cache_get, cache_set
from .data_layer import fetch_coverage_snapshot
from .screener import Cond   # 沿用 pass/fail/na 的小資料類

_MOPS_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1"


# ======================================================================
# A. 法說會(MOPS 法人說明會一覽表)——「有沒有開法說」的判定來源
# ======================================================================
def _roc_to_iso(s: str) -> str | None:
    """民國 'YYY/MM/DD' → 西元 'YYYY-MM-DD'。"""
    try:
        p = s.split("/")
        return f"{int(p[0]) + 1911:04d}-{int(p[1]):02d}-{int(p[2]):02d}"
    except (ValueError, IndexError):
        return None


def _fetch_mops_year(roc_year: int) -> dict[str, str]:
    """抓某民國年的法說會一覽表 → {stock_id: 最近一次法說西元日期}。"""
    import requests

    out: dict[str, str] = {}
    r = requests.post(
        _MOPS_URL,
        data={"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1,
              "TYPEK": "sii", "year": str(roc_year)},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=40,
    )
    r.raise_for_status()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 3 and re.fullmatch(r"[1-9]\d{3}", cells[0] or ""):
            iso = _roc_to_iso(cells[2])
            if iso and (cells[0] not in out or iso > out[cells[0]]):
                out[cells[0]] = iso
    return out


def fetch_meeting_ids_tw(lookback_days: int = 365) -> set[str]:
    """回傳『近 lookback_days 天有開法說會』的上市股票代號集合。MOPS 資料快取 24h。"""
    key = "mops_meetings_sii"
    cached = cache_get(key, ttl_seconds=24 * 3600)
    if cached is not None:
        meetings = cached["data"]
    else:
        cur_roc = date.today().year - 1911
        meetings: dict[str, str] = {}
        for yr in (cur_roc, cur_roc - 1):          # 當年 + 去年 → 涵蓋滾動一年
            try:
                for sid, iso in _fetch_mops_year(yr).items():
                    if sid not in meetings or iso > meetings[sid]:
                        meetings[sid] = iso
            except Exception:  # noqa: BLE001 — 單年失敗不影響
                continue
        if meetings:
            cache_set(key, meetings)
    cut = (date.today() - timedelta(days=lookback_days)).isoformat()
    return {sid for sid, iso in meetings.items() if iso >= cut}


# ======================================================================
# B. 單檔評估
# ======================================================================
_U_LABELS = {
    "u1": "① 市值門檻", "u2": "② 分析師覆蓋", "u3": "③ 法說會/季度法說", "u4": "④ 流動性",
}


@dataclass
class UniverseResult:
    stock_id: str
    name: str
    industry: str
    market: str                       # twse / us
    conds: dict = field(default_factory=dict)   # u1..u4 -> Cond
    market_cap: float | None = None
    n_analysts: int | None = None
    liq_avg: float | None = None
    ok: bool = True                   # 快照有抓到(能評估)

    @property
    def passed(self) -> bool:
        return all(c.status == "pass" for c in self.conds.values())

    @property
    def n_pass(self) -> int:
        return sum(1 for c in self.conds.values() if c.status == "pass")

    @property
    def missing(self) -> list[str]:
        """沒通過(fail/na)的條件標籤,給邊緣案例用。"""
        return [_U_LABELS[k] for k, c in self.conds.items() if c.status != "pass"]


def _money(v: float | None, market: str) -> str:
    if v is None:
        return "—"
    if market == "us":
        return f"{v / 1e9:,.1f}十億美元" if v >= 1e9 else f"{v / 1e6:,.0f}百萬美元"
    return f"{v / 1e8:,.0f}億" if v >= 1e8 else f"{v / 1e4:,.0f}萬"


def evaluate(stock: dict, snap: dict | None, meeting_ids: set[str], cfg: dict) -> UniverseResult:
    market = stock.get("market", "twse")
    conf = cfg["universe_builder"]["us" if market == "us" else "tw"]
    r = UniverseResult(stock_id=stock["stock_id"], name=stock.get("name", stock["stock_id"]),
                       industry=stock.get("industry", ""), market=market)
    if not snap:
        r.ok = False
        for k in ("u1", "u2", "u3", "u4"):
            r.conds[k] = Cond("na", "無 yfinance 快照")
        return r

    mc = snap.get("market_cap")
    n = snap.get("n_y0") or snap.get("n_q0") or snap.get("n_q1")
    liq = snap.get("liq_avg")
    r.market_cap, r.n_analysts, r.liq_avg = mc, n, liq

    # ① 市值
    if mc is None:
        r.conds["u1"] = Cond("na", "無市值資料")
    else:
        r.conds["u1"] = Cond("pass" if mc > conf["min_market_cap"] else "fail",
                             f"市值 {_money(mc, market)}(門檻 {_money(conf['min_market_cap'], market)})")
    # ② 分析師覆蓋
    need = conf["min_analyst_coverage"]
    if snap.get("n_y0") is None and snap.get("n_q0") is None and snap.get("n_q1") is None:
        r.conds["u2"] = Cond("fail", f"無分析師覆蓋資料(需 ≥{need} 家)")
    else:
        cov = n or 0
        r.conds["u2"] = Cond("pass" if cov >= need else "fail", f"{cov} 家(需 ≥{need}）")
    # ③ 法說會 / 季度法說
    if market == "us":
        has_call = bool(snap.get("n_q0") or snap.get("n_q1"))
        r.conds["u3"] = Cond("pass" if (has_call or not conf.get("require_earnings_call", True)) else "fail",
                             "有季度共識(法說代理)" if has_call else "無季度共識(視為無季度法說)")
    else:
        if not conf.get("require_meeting", True):
            r.conds["u3"] = Cond("pass", "未要求法說會")
        else:
            in_set = r.stock_id in meeting_ids
            r.conds["u3"] = Cond("pass" if in_set else "fail",
                                 "近一年有法說會" if in_set else "近一年查無法說會")
    # ④ 流動性
    if liq is None:
        r.conds["u4"] = Cond("na", "無成交資料")
    else:
        r.conds["u4"] = Cond("pass" if liq > conf["min_avg_value"] else "fail",
                             f"日均額 {_money(liq, market)}(門檻 {_money(conf['min_avg_value'], market)})")
    return r


# ======================================================================
# C. 批次建構
# ======================================================================
def build(candidates: list[dict], market: str, cfg: dict, meeting_ids: set[str] | None = None,
          progress=None) -> tuple[list[UniverseResult], dict]:
    """對一批候選股建構母體。回傳 (每檔結果, 統計)。progress(i,n,result) 可選。"""
    meeting_ids = meeting_ids if meeting_ids is not None else set()
    results: list[UniverseResult] = []
    for i, s in enumerate(candidates, 1):
        s = dict(s)
        s["market"] = market
        ticker = f"{s['stock_id']}.TW" if market == "twse" else s["stock_id"]
        try:
            snap, _ = fetch_coverage_snapshot(ticker, liq_days=cfg["universe_builder"]["liquidity_days"])
        except Exception:  # noqa: BLE001
            snap = None
        r = evaluate(s, snap, meeting_ids, cfg)
        results.append(r)
        if progress:
            progress(i, len(candidates), r)

    # 統計:每條刷掉幾檔(獨立計)+ 通過家數 + 邊緣案例(僅差一條)
    stats = {"total": len(results), "passed": sum(1 for r in results if r.passed)}
    for k, label in _U_LABELS.items():
        stats[k] = {"fail": sum(1 for r in results if r.conds[k].status == "fail"),
                    "na": sum(1 for r in results if r.conds[k].status == "na"),
                    "label": label}
    stats["edge"] = [r for r in results if r.n_pass == 3]   # 4 條只差 1 條
    return results, stats
