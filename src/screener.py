"""
兩層選股篩選器 — 核心 (screener.py)
====================================
讀「本地全市場資料」(data/universe/<代號>.json,由 fetch_universe.py 抓好),
套用 config/screener.yaml 的門檻,做兩層篩選:

  第一層 資格篩選(6 條全過才進池,排地雷):
    1 上市滿5年  2 近5年≥4年EPS為正  3 近4季OCF為正
    4 負債比<60%(金融股排除此條)  5 近60日日均成交金額>門檻  6 有最新財報
  第二層 品質篩選(通過第一層者中標記,不淘汰):
    7 近3年營收CAGR>門檻  8 近3年毛利率斜率≥0  9 近3年ROE均>門檻  10 盈餘修正動能

誠實原則:任一條件因資料缺失無法判斷 → 回 "na"(資料不足),一律不當通過。
所有門檻皆來自 config,不寫死。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- FinMind 科目名稱(和 data_layer 一致)----
_REVENUE = "Revenue"
_GROSS = "GrossProfit"
_EPS = "EPS"
_PARENT_NI = "EquityAttributableToOwnersOfParent"   # 損益表:母公司業主淨利
_LIAB = "Liabilities"
_ASSETS = "TotalAssets"
_NCI = "NoncontrollingInterests"
_OCF = "CashFlowsFromOperatingActivities"           # 現金流:營運活動(YTD 累計)


# ======================================================================
# A. 從 FinMind pivot 萃取「選股需要的精簡指標」(存本地用)
# ======================================================================
def _annual_sum(income_pivot: dict, year: int, key: str) -> float | None:
    """某年 4 季損益科目加總成全年(單季值);不足 4 季回 None。"""
    tot, n = 0.0, 0
    for d, t in income_pivot.items():
        if d.startswith(str(year)) and key in t:
            tot += t[key]
            n += 1
    return tot if n == 4 else None


def _year_end(pivot: dict, year: int, key: str) -> float | None:
    t = pivot.get(f"{year}-12-31")
    return t.get(key) if t else None


def _single_quarter_ocf(cashflow_pivot: dict) -> list[list]:
    """現金流量表為 YTD 累計,轉成每季『單季』OCF。回傳 [[date, single_q_ocf], ...] 已排序。"""
    items = sorted((d, t[_OCF]) for d, t in cashflow_pivot.items() if _OCF in t)
    out: list[list] = []
    prev_year = None
    prev_cum = 0.0
    for d, cum in items:
        y = d[:4]
        q = d[5:7]
        if q == "03" or y != prev_year:      # 每年 Q1(或跨年)重新起算
            single = cum
        else:
            single = cum - prev_cum
        out.append([d, round(single, 2)])
        prev_year, prev_cum = y, cum
    return out


def extract_metrics(income: dict, balance: dict, cashflow: dict) -> dict:
    """把三張表 pivot 萃取成精簡指標 dict(存進 data/universe/<id>.json 的主體)。"""
    inc_years = sorted({int(d[:4]) for d in income})
    annual: dict[str, dict] = {}
    for y in inc_years:
        rev = _annual_sum(income, y, _REVENUE)
        gp = _annual_sum(income, y, _GROSS)
        eps = _annual_sum(income, y, _EPS)          # 全年 EPS = 4 季加總
        pni = _annual_sum(income, y, _PARENT_NI)
        if rev is None and eps is None:
            continue
        annual[str(y)] = {
            "revenue": rev, "gross_profit": gp, "eps": eps, "parent_ni": pni,
        }

    annual_bs: dict[str, dict] = {}
    for y in sorted({int(d[:4]) for d in balance}):
        la = _year_end(balance, y, _LIAB)
        ta = _year_end(balance, y, _ASSETS)
        if la is None or ta is None:
            continue
        annual_bs[str(y)] = {
            "liabilities": la, "total_assets": ta,
            "nci": _year_end(balance, y, _NCI) or 0.0,
        }

    # 最新一季資產負債(負債比用最新,不是年底)
    latest_bs = None
    if balance:
        d = max(balance)
        t = balance[d]
        if _LIAB in t and _ASSETS in t:
            latest_bs = {"date": d, "liabilities": t[_LIAB], "total_assets": t[_ASSETS]}

    inc_dates = sorted(income)
    return {
        "first_report": inc_dates[0] if inc_dates else None,
        "latest_report": inc_dates[-1] if inc_dates else None,
        "annual": annual,
        "annual_bs": annual_bs,
        "latest_bs": latest_bs,
        "ocf_q": _single_quarter_ocf(cashflow)[-8:],
    }


# ======================================================================
# B. 條件評估:每條回 (status, detail);status ∈ pass / fail / na(資料不足)
# ======================================================================
@dataclass
class Cond:
    status: str      # "pass" / "fail" / "na"
    detail: str = ""

    @property
    def mark(self) -> str:
        return {"pass": "✅", "fail": "❌", "na": "⚠️資料不足"}[self.status]


def _complete_years(annual: dict, need_key: str) -> list[int]:
    """有該欄位、且值不為 None 的年份(升冪)。"""
    ys = []
    for y, a in annual.items():
        if a.get(need_key) is not None:
            ys.append(int(y))
    return sorted(ys)


def _slope(ys: list[float]) -> float:
    """對 y 序列(x=0,1,2,...)做最小平方斜率。"""
    n = len(ys)
    if n < 2:
        return 0.0
    xm = (n - 1) / 2.0
    ym = sum(ys) / n
    num = sum((i - xm) * (v - ym) for i, v in enumerate(ys))
    den = sum((i - xm) ** 2 for i in range(n))
    return num / den if den else 0.0


# ---- 第一層 ----------------------------------------------------------
def c1_listed_years(rec: dict, cfg: dict) -> Cond:
    first = rec.get("first_report")
    if not first:
        return Cond("na", "無財報起始日")
    yrs = (date.today() - date.fromisoformat(first)).days / 365.25
    need = cfg["layer1"]["listed_years"]["min"]
    return Cond("pass" if yrs >= need else "fail", f"約 {yrs:.1f} 年(門檻 {need}）")


def c2_eps_positive(rec: dict, cfg: dict) -> Cond:
    conf = cfg["layer1"]["eps_positive"]
    yrs = _complete_years(rec["annual"], "eps")
    window = yrs[-conf["years"]:]
    if len(window) < conf["years"]:
        return Cond("na", f"僅 {len(window)} 個完整年度 EPS(需 {conf['years']}）")
    pos = sum(1 for y in window if (rec["annual"][str(y)]["eps"] or 0) > 0)
    ok = pos >= conf["min_positive_years"]
    return Cond("pass" if ok else "fail",
                f"近{conf['years']}年 {pos} 年為正(需 ≥{conf['min_positive_years']}）")


def c3_ocf_positive(rec: dict, cfg: dict) -> Cond:
    conf = cfg["layer1"]["ocf_positive"]
    q = rec.get("ocf_q") or []
    if len(q) < conf["quarters"]:
        return Cond("na", f"僅 {len(q)} 季 OCF(需 {conf['quarters']}）")
    last = q[-conf["quarters"]:]
    vals = [v for _, v in last]
    if conf["mode"] == "each_positive":
        ok = all(v > 0 for v in vals)
        return Cond("pass" if ok else "fail", f"近{conf['quarters']}季各季OCF {'皆正' if ok else '有負值'}")
    ttm = sum(vals)
    return Cond("pass" if ttm > 0 else "fail", f"近{conf['quarters']}季OCF加總 {ttm/1e8:,.1f}億")


def c4_debt_ratio(rec: dict, cfg: dict, stock_id: str) -> Cond:
    conf = cfg["layer1"]["debt_ratio"]
    sid = int(stock_id) if stock_id.isdigit() else -1
    if conf["exclude_financial"] and conf["financial_id_min"] <= sid <= conf["financial_id_max"]:
        return Cond("pass", "金融股,依設定排除此條")
    bs = rec.get("latest_bs")
    if not bs or not bs.get("total_assets"):
        return Cond("na", "無最新資產負債資料")
    ratio = bs["liabilities"] / bs["total_assets"] * 100
    return Cond("pass" if ratio < conf["max_pct"] else "fail",
                f"負債比 {ratio:.1f}%(門檻 <{conf['max_pct']}%)")


def c5_liquidity(rec: dict, cfg: dict) -> Cond:
    conf = cfg["layer1"]["liquidity"]
    avg = rec.get("liq_avg_value")
    days = rec.get("liq_days") or 0
    if avg is None or days < conf["days"]:
        return Cond("na", f"成交資料不足({days} 日)")
    ok = avg > conf["min_avg_value"]
    return Cond("pass" if ok else "fail",
                f"近{conf['days']}日均額 {avg/1e8:,.2f}億(門檻 {conf['min_avg_value']/1e8:.2f}億)")


def c6_latest_report(rec: dict, cfg: dict) -> Cond:
    conf = cfg["layer1"]["latest_report"]
    latest = rec.get("latest_report")
    if not latest:
        return Cond("na", "無財報")
    age = (date.today() - date.fromisoformat(latest)).days
    return Cond("pass" if age <= conf["max_age_days"] else "fail",
                f"最新財報 {latest}(距今 {age} 天,門檻 ≤{conf['max_age_days']}）")


# ---- 第二層 ----------------------------------------------------------
def q7_revenue_cagr(rec: dict, cfg: dict) -> Cond:
    conf = cfg["layer2"]["revenue_cagr"]
    n = conf["years"]
    yrs = _complete_years(rec["annual"], "revenue")
    if len(yrs) < n + 1:
        return Cond("na", f"營收年數不足(有 {len(yrs)},需 {n+1}）")
    y0, y1 = yrs[-1 - n], yrs[-1]
    r0 = rec["annual"][str(y0)]["revenue"]
    r1 = rec["annual"][str(y1)]["revenue"]
    if not r0 or r0 <= 0 or not r1 or r1 <= 0:
        return Cond("na", "營收含非正值,無法算CAGR")
    cagr = ((r1 / r0) ** (1 / n) - 1) * 100
    return Cond("pass" if cagr > conf["min_pct"] else "fail", f"{cagr:.1f}%")


def q8_gross_margin_trend(rec: dict, cfg: dict) -> Cond:
    conf = cfg["layer2"]["gross_margin_trend"]
    n = conf["years"]
    yrs = [y for y in _complete_years(rec["annual"], "gross_profit")
           if rec["annual"][str(y)].get("revenue")]
    window = yrs[-n:]
    if len(window) < n:
        return Cond("na", f"毛利年數不足(有 {len(window)},需 {n}）")
    gm = [rec["annual"][str(y)]["gross_profit"] / rec["annual"][str(y)]["revenue"] * 100
          for y in window]
    s = _slope(gm)
    return Cond("pass" if s >= conf["min_slope"] else "fail",
                f"斜率 {s:+.2f}pp/年({gm[0]:.1f}%→{gm[-1]:.1f}%)")


def q9_roe(rec: dict, cfg: dict) -> Cond:
    conf = cfg["layer2"]["roe"]
    n = conf["years"]
    roes: list[float] = []
    yrs = sorted(int(y) for y in rec["annual_bs"])
    for y in yrs:
        a = rec["annual"].get(str(y))
        b = rec["annual_bs"].get(str(y))
        if not a or not b or a.get("parent_ni") is None:
            continue
        eq = b["total_assets"] - b["liabilities"]
        if conf["equity_basis"] == "parent":
            eq -= b.get("nci", 0.0)
        if eq and eq > 0:
            roes.append(a["parent_ni"] / eq * 100)
    window = roes[-n:]
    if len(window) < n:
        return Cond("na", f"ROE 年數不足(有 {len(window)},需 {n}）")
    avg = sum(window) / len(window)
    return Cond("pass" if avg > conf["min_avg_pct"] else "fail", f"近{n}年均 {avg:.1f}%")


def q10_momentum(rec: dict, cfg: dict, stock_id: str) -> Cond:
    conf = cfg["layer2"]["momentum"]
    if conf["source"] == "off":
        return Cond("na", "未啟用共識來源")
    from .data_layer import load_consensus_history
    from .scan_state import revision_momentum
    hist = load_consensus_history(ROOT / f"data/consensus/{stock_id}.csv")
    d, pct = revision_momentum(hist)
    if d == "na":
        return Cond("na", "無足夠共識歷史")
    if d == "up":
        return Cond("pass", f"共識上修 {pct:+.1f}%")
    if d == "down":
        return Cond("fail", f"共識下修 {pct:+.1f}%")
    return Cond("fail", "共識持平")


# ======================================================================
# C. 單檔評估 + 全體篩選
# ======================================================================
@dataclass
class ScreenResult:
    stock_id: str
    name: str
    industry: str
    layer1: dict = field(default_factory=dict)   # key -> Cond
    layer2: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)  # 顯示用:rev_cagr / gm_slope / roe / momentum
    gate_keys: tuple = ("q7", "q8", "q9")         # 列入「兩層全過」判定的第二層條件

    @property
    def layer1_pass(self) -> bool:
        return all(c.status == "pass" for c in self.layer1.values())

    @property
    def layer2_pass(self) -> bool:
        # 只用「列入判定」的品質條件(⑩修正動能預設僅標記、不列入)
        return all(self.layer2[k].status == "pass" for k in self.gate_keys)

    @property
    def both_pass(self) -> bool:
        return self.layer1_pass and self.layer2_pass


L1_LABELS = {
    "c1": "① 上市滿5年", "c2": "② 近5年≥4年EPS正", "c3": "③ 近4季OCF正",
    "c4": "④ 負債比<60%", "c5": "⑤ 流動性達標", "c6": "⑥ 有最新財報",
}
L2_LABELS = {
    "q7": "⑦ 營收CAGR", "q8": "⑧ 毛利率趨勢", "q9": "⑨ ROE", "q10": "⑩ 修正動能",
}


def evaluate(rec: dict, cfg: dict) -> ScreenResult:
    sid = rec["stock_id"]
    r = ScreenResult(stock_id=sid, name=rec.get("name", sid), industry=rec.get("industry", ""))
    r.layer1 = {
        "c1": c1_listed_years(rec, cfg),
        "c2": c2_eps_positive(rec, cfg),
        "c3": c3_ocf_positive(rec, cfg),
        "c4": c4_debt_ratio(rec, cfg, sid),
        "c5": c5_liquidity(rec, cfg),
        "c6": c6_latest_report(rec, cfg),
    }
    # 第二層只在通過第一層時才有意義,但仍全部算出來(標記用)
    r.layer2 = {
        "q7": q7_revenue_cagr(rec, cfg),
        "q8": q8_gross_margin_trend(rec, cfg),
        "q9": q9_roe(rec, cfg),
        "q10": q10_momentum(rec, cfg, sid),
    }
    r.metrics = {
        "rev_cagr": r.layer2["q7"].detail,
        "gm_slope": r.layer2["q8"].detail,
        "roe": r.layer2["q9"].detail,
        "momentum": r.layer2["q10"].detail,
    }
    # ⑩ 修正動能是否列入「兩層全過」判定(預設否;由 config 控制)
    gate = ["q7", "q8", "q9"]
    if cfg["layer2"]["momentum"].get("gating", False):
        gate.append("q10")
    r.gate_keys = tuple(gate)
    return r


def screen_all(records: list[dict], cfg: dict) -> tuple[list[ScreenResult], dict]:
    """回傳 (每檔結果, 漏斗統計)。"""
    results = [evaluate(rec, cfg) for rec in records]

    # 漏斗統計:每條 Layer1 的 pass/fail/na 家數(獨立計算)
    funnel: dict[str, dict] = {}
    for key in L1_LABELS:
        c = {"pass": 0, "fail": 0, "na": 0}
        for r in results:
            c[r.layer1[key].status] += 1
        funnel[key] = c
    funnel["layer1_pass"] = sum(1 for r in results if r.layer1_pass)
    funnel["both_pass"] = sum(1 for r in results if r.layer1_pass and r.layer2_pass)
    funnel["total"] = len(results)
    return results, funnel


# ---- 本地資料存取 ----------------------------------------------------
def load_records(universe_dir: str | Path) -> list[dict]:
    d = Path(universe_dir)
    out: list[dict] = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def load_config(path: str | Path) -> dict:
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
