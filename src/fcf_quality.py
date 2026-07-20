"""
FCF 品質檢查 (fcf_quality.py) — 新功能
======================================
「賺到的錢是不是真現金?擴產有沒有變成營收?」用 4 個角度檢查獲利含金量:

  A. 雙線對照(圖):資本支出年增率 vs 營收年增率,且把資本支出「領先 2 年」對齊。
       邏輯:台積電今天的資本支出(蓋廠/買機台),約 2 年後才變成產能與營收。
       所以把「資本支出年增率」往後推 2 年,和「營收年增率」疊圖,
       看『前兩年的猛擴產』有沒有兌現成『現在的營收成長』。

  B. 三燈號(綠/黃/紅),抓獲利含金量的warning:
       1) 存貨天數 DIO:上升太快=庫存堆積(需求轉弱/砍單風險)
       2) 應收天數 DSO:上升太快=收款變慢(塞貨/呆帳疑慮)
       3) 營運現金流 OCF 年增率:衰退=帳面獲利沒轉成真現金

資料來源(皆 FinMind,長區間一次抓):
  - 綜合損益表:營收 Revenue、毛利 GrossProfit(→ 營業成本 COGS = 營收 − 毛利)
  - 資產負債表:存貨 Inventories、應收帳款淨額 AccountsReceivableNet(取『年底』)
  - 現金流量表:營運現金流、資本支出(YTD 累計,取『12-31 全年』值)
"""

from __future__ import annotations

from dataclasses import dataclass

# FinMind 各表科目名稱
_REVENUE = "Revenue"
_GROSS = "GrossProfit"
_INVENTORY = "Inventories"
_RECEIVABLE = "AccountsReceivableNet"
_OCF = "CashFlowsFromOperatingActivities"          # 營運現金流(YTD 累計)
_CAPEX = "PropertyAndPlantAndEquipment"            # 資本支出(現金流表,通常為負)

# ── 領先年數:資本支出→營收的遞延(台積電擴產約 2 年開花)────────────
_CAPEX_LEAD_YEARS = 2

# ── 三燈號門檻(經驗法則,可調)────────────────────────────────────────
# 存貨天數 DIO = 365 × 期末存貨 / 全年營業成本;看的是「年增變化」不是絕對值:
#     綠:YoY 變化 ≤ +5%     (庫存去化健康 / 與營收同步)
#     黃:+5% < YoY ≤ +20%   (略有累積,留意)
#     紅:YoY > +20%         (庫存明顯堆積,可能需求轉弱或砍單風險)
_DIO_GREEN, _DIO_RED = 5.0, 20.0
# 應收天數 DSO = 365 × 期末應收帳款 / 全年營收;同樣看年增變化:
#     綠:YoY ≤ +5%          (收款效率穩定)
#     黃:+5% < YoY ≤ +20%   (收款趨緩)
#     紅:YoY > +20%         (收款明顯變慢,潛在塞貨 / 呆帳)
_DSO_GREEN, _DSO_RED = 5.0, 20.0
# 營運現金流 OCF 年增率(本身就是成長率,直接看數值):
#     綠:≥ +10%             (營運現金成長,獲利含金量佳)
#     黃:−10% ~ +10%        (持平)
#     紅:< −10%             (營運現金衰退,帳面獲利沒轉成真現金)
_OCF_GREEN, _OCF_RED = 10.0, -10.0


@dataclass
class Signal:
    """單一燈號。"""

    name: str          # 存貨天數 / 應收天數 / 營運現金流年增率
    light: str         # green / yellow / red / gray(資料不足)
    value_text: str    # 顯示文字,如 "88 天(YoY +3.2%)"
    note: str          # 白話一行
    kind: str = ""     # 穩定鍵:dio / dso / ocf(給狀態比對用,不受年份影響)


@dataclass
class FcfQualityResult:
    """FCF 品質檢查結果(圖 + 燈號)。"""

    years: list[int]                    # 有完整年度資料的年份(升冪)
    rev_growth: list[float | None]      # 營收年增率 %(對齊 years)
    capex_growth: list[float | None]    # 資本支出年增率 %(對齊 years)
    dio: list[float | None]             # 存貨天數(對齊 years)
    dso: list[float | None]             # 應收天數(對齊 years)
    ocf_bn: list[float | None]          # 營運現金流(十億台幣,對齊 years)
    ocf_growth: list[float | None]      # OCF 年增率 %
    signals: list[Signal]               # 三燈號(取最新完整年度)
    capex_lead_years: int               # 資本支出領先年數(=2)
    latest_year: int | None
    source: str = ""


def _year_end(pivot: dict, year: int, key: str) -> float | None:
    """取某年『12-31』那筆的科目值(現金流/資產負債取年底)。"""
    t = pivot.get(f"{year}-12-31")
    if not t:
        return None
    v = t.get(key)
    return float(v) if v is not None else None


def _annual_sum(income_pivot: dict, year: int, key: str) -> float | None:
    """把某年 4 季的損益科目加總成全年(綜合損益表為單季值)。缺季回 None。"""
    total = 0.0
    n = 0
    for d, t in income_pivot.items():
        if d.startswith(str(year)) and key in t:
            total += t[key]
            n += 1
    return total if n == 4 else None            # 一定要滿 4 季,否則不完整


def _growth(cur: float | None, prev: float | None) -> float | None:
    """年增率 %。任一缺或分母 0 回 None。"""
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100.0


def _light_by_change(change_pct: float | None, green_le: float, red_gt: float) -> str:
    """依『年增變化%』給燈:≤green 綠、>red 紅、之間黃、缺值灰。"""
    if change_pct is None:
        return "gray"
    if change_pct <= green_le:
        return "green"
    if change_pct > red_gt:
        return "red"
    return "yellow"


def build_fcf_quality(
    income_pivot: dict,
    balance_pivot: dict,
    cashflow_pivot: dict,
    source: str = "FinMind 財報/資產負債/現金流",
) -> FcfQualityResult:
    """組出 FCF 品質檢查(雙線圖序列 + 三燈號)。資料不足會 raise。

    設計:各序列「各自」用有資料的年份,不互相拖累——
      - 核心年份 years = 有『4 季損益 + 年底現金流』的年(營收/資本支出/OCF 連續);
      - 存貨/應收(DIO/DSO)另外看『年底資產負債表』,某些年缺(如 FinMind 2016/2017
        缺年底資產負債)就在該年填 None,不影響雙線圖與 OCF。
      - 年增率一律對『日曆前一年』計算,前一年缺就給 None(避免跨年誤算)。
    """
    # 各科目 by year(有才放,分開蒐集)
    revenue: dict[int, float] = {}
    cogs: dict[int, float] = {}
    ocf: dict[int, float] = {}
    capex: dict[int, float] = {}
    inventory: dict[int, float] = {}
    receivable: dict[int, float] = {}

    for y in sorted({int(d[:4]) for d in income_pivot}):
        rev = _annual_sum(income_pivot, y, _REVENUE)
        gross = _annual_sum(income_pivot, y, _GROSS)
        o = _year_end(cashflow_pivot, y, _OCF)
        cx = _year_end(cashflow_pivot, y, _CAPEX)
        # 核心年份:要有完整 4 季損益 + 年底現金流(營收/COGS/OCF/Capex)
        if None not in (rev, gross, o, cx):
            revenue[y] = rev
            cogs[y] = rev - gross                 # 營業成本 = 營收 − 毛利
            ocf[y] = o
            capex[y] = abs(cx)                     # 現金流表資本支出為負,取絕對值
        # 存貨/應收:各自看年底資產負債表(某些年可能缺)
        inv = _year_end(balance_pivot, y, _INVENTORY)
        rec = _year_end(balance_pivot, y, _RECEIVABLE)
        if inv is not None and y in revenue:       # DIO 需要 COGS(在 revenue 同組)
            inventory[y] = inv
        if rec is not None and y in revenue:       # DSO 需要營收
            receivable[y] = rec

    years = sorted(revenue)
    if len(years) < 2:
        raise ValueError("FCF 品質:完整年度不足 2 年,無法比較")

    # 對齊 years 的各序列(年增率一律對『日曆前一年』y-1)
    rev_growth: list[float | None] = []
    capex_growth: list[float | None] = []
    dio: list[float | None] = []
    dso: list[float | None] = []
    ocf_bn: list[float | None] = []
    ocf_growth: list[float | None] = []
    for y in years:
        p = y - 1
        rev_growth.append(_growth(revenue.get(y), revenue.get(p)))
        capex_growth.append(_growth(capex.get(y), capex.get(p)))
        ocf_bn.append(round(ocf[y] / 1e9, 1))
        ocf_growth.append(_growth(ocf.get(y), ocf.get(p)))
        dio.append(round(365 * inventory[y] / cogs[y], 1) if y in inventory else None)
        dso.append(round(365 * receivable[y] / revenue[y], 1) if y in receivable else None)

    # ── 三燈號:用「最新年度」對比『日曆前一年』─────────────────────
    ly = years[-1]
    py = ly - 1
    dio_chg = _growth(_pick(inventory, cogs, ly, "dio"), _pick(inventory, cogs, py, "dio"))
    dso_chg = _growth(_pick(receivable, revenue, ly, "dso"), _pick(receivable, revenue, py, "dso"))
    ocf_g = _growth(ocf.get(ly), ocf.get(py))
    dio_now = dio[-1]
    dso_now = dso[-1]

    signals = [
        Signal(
            name=f"存貨天數 DIO({ly})",
            kind="dio",
            light=_light_by_change(dio_chg, _DIO_GREEN, _DIO_RED),
            value_text=((f"{dio_now:.0f} 天" if dio_now is not None else "—")
                        + (f"(YoY {dio_chg:+.1f}%)" if dio_chg is not None else "")),
            note="庫存要幾天賣完;快速攀升代表庫存堆積、需求可能轉弱(綠≤+5%｜黃≤+20%｜紅>+20%)。",
        ),
        Signal(
            name=f"應收天數 DSO({ly})",
            kind="dso",
            light=_light_by_change(dso_chg, _DSO_GREEN, _DSO_RED),
            value_text=((f"{dso_now:.0f} 天" if dso_now is not None else "—")
                        + (f"(YoY {dso_chg:+.1f}%)" if dso_chg is not None else "")),
            note="賣出後多久收到錢;拉長代表收款變慢、恐塞貨或呆帳(綠≤+5%｜黃≤+20%｜紅>+20%)。",
        ),
        Signal(
            name=f"營運現金流年增率({ly})",
            kind="ocf",
            light=("gray" if ocf_g is None
                   else "green" if ocf_g >= _OCF_GREEN
                   else "red" if ocf_g < _OCF_RED
                   else "yellow"),
            value_text=(f"{ocf_g:+.1f}%" if ocf_g is not None else "—")
                       + f"(OCF {ocf_bn[-1]:,.0f} 十億)",
            note="帳面獲利有沒有變成真現金;衰退是警訊(綠≥+10%｜黃±10%｜紅<−10%)。",
        ),
    ]

    return FcfQualityResult(
        years=years,
        rev_growth=rev_growth,
        capex_growth=capex_growth,
        dio=dio,
        dso=dso,
        ocf_bn=ocf_bn,
        ocf_growth=ocf_growth,
        signals=signals,
        capex_lead_years=_CAPEX_LEAD_YEARS,
        latest_year=ly,
        source=source,
    )


def _pick(numer: dict[int, float], denom: dict[int, float], year: int, kind: str) -> float | None:
    """算某年的 DIO/DSO 供燈號比較(年底存貨或應收 ÷ 當年 COGS 或營收)。"""
    if year not in numer or year not in denom or not denom[year]:
        return None
    return 365 * numer[year] / denom[year]
