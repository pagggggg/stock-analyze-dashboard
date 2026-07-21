"""
資料層 (data_layer.py)
======================
負責「取得數據」。分兩條路(先手動、再自動,自動失敗一律退回手動):

  1. 手動 (manual)  → 讀 data/*.csv                       ← 保證跑得動的 fallback
  2. 自動 (auto)    → 免費 API:
       - 近8季財務數據  用 FinMind(TaiwanStockFinancialStatements)
       - 近10年本益比    用 TWSE 個股日本益比(BWIBBU)聚合成年度高/低/平均

自動抓取會:
  - 用 cache.py 檔案快取避免重複打 API(FinMind 有額度、TWSE 要禮貌節流)
  - 每筆資料標註「來源 + 抓取日期」
  - 提供 validate_against_csv():把 API 數字和你原本手動 CSV 對照,差異 > 2% 列警告

CSV 欄位說明見 data/ 底下的範例檔與 README。
"""

from __future__ import annotations

import csv
import os
import statistics
import time
from datetime import date
from pathlib import Path

from .cache import cache_get, cache_set
from .models import PEBand, QuarterFinancials


def _finmind_loader():
    """建立 FinMind DataLoader,支援用環境變數 FINMIND_TOKEN 登入(CI/higher 額度用)。

    本機沒設 token 就走匿名(有較低額度);GitHub Actions 可用 secret 注入
    FINMIND_TOKEN 取得較高額度,避免每日掃描多檔時被限流。
    """
    from FinMind.data import DataLoader  # 延遲匯入:手動模式不需要 FinMind

    return DataLoader(token=os.getenv("FINMIND_TOKEN", ""))


# ======================================================================
# A. 手動:近 8 季財務數據 CSV
# ======================================================================
def load_financials_csv(path: str | Path) -> list[QuarterFinancials]:
    """讀取近 8 季 (或更多) 財務數據 CSV → list[QuarterFinancials]。

    期望欄位 (第一列為表頭):
        quarter, revenue_twd_bn, gross_margin_pct, opex_ratio_pct,
        tax_rate_pct, shares_bn, non_op_ratio_pct, reported_eps, source
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到財務數據 CSV:{path}")

    rows: list[QuarterFinancials] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:  # utf-8-sig 相容 Excel 存檔
        reader = csv.DictReader(f)
        for i, r in enumerate(reader, start=2):  # start=2:第2行才是資料 (第1行表頭)
            # 跳過整列空白 / 註解列 (以 # 開頭的 quarter)
            q = (r.get("quarter") or "").strip()
            if not q or q.startswith("#"):
                continue
            try:
                rows.append(
                    QuarterFinancials(
                        quarter=q,
                        revenue_twd_bn=float(r["revenue_twd_bn"]),
                        gross_margin_pct=float(r["gross_margin_pct"]),
                        opex_ratio_pct=float(r["opex_ratio_pct"]),
                        tax_rate_pct=float(r["tax_rate_pct"]),
                        shares_bn=float(r["shares_bn"]),
                        non_op_ratio_pct=float(r.get("non_op_ratio_pct", 0) or 0),
                        reported_eps=float(r.get("reported_eps", 0) or 0),
                        source=(r.get("source") or "").strip(),
                    )
                )
            except (KeyError, ValueError) as e:
                raise ValueError(f"財務 CSV 第 {i} 行解析失敗:{e}。請檢查欄位與數值格式。")

    if not rows:
        raise ValueError(f"財務數據 CSV 沒有任何有效資料列:{path}")

    # 依季度排序 (字串排序對 '2024Q3' 這種格式剛好正確:先比年再比季)
    rows.sort(key=lambda x: x.quarter)
    return rows


def compute_historical_averages(quarters: list[QuarterFinancials], last_n: int = 8) -> dict:
    """算近 N 季的營業費用率 / 稅率 / 業外比 平均,供假設檔參考與報告標註。

    回傳 dict:{"opex_ratio": ..., "tax_rate": ..., "non_op_ratio": ...,
               "quarters_used": [...]}
    """
    recent = quarters[-last_n:] if len(quarters) >= last_n else quarters
    return {
        "opex_ratio": statistics.mean(q.opex_ratio_pct for q in recent),
        "tax_rate": statistics.mean(q.tax_rate_pct for q in recent),
        "non_op_ratio": statistics.mean(q.non_op_ratio_pct for q in recent),
        "gross_margin": statistics.mean(q.gross_margin_pct for q in recent),
        "quarters_used": [q.quarter for q in recent],
    }


def trailing_eps(
    quarters: list[QuarterFinancials],
    n: int = 3,
    before_quarter: str | None = None,
) -> tuple[float, list[str]]:
    """取試算季度「之前」最近 n 季的『實際公布 EPS』加總,給估值年化 (TTM) 用。

    為什麼要 before_quarter?
      自動抓取後資料可能比試算季度還新(例如 API 已有到 2026Q1,但你要估 2025Q1)。
      若直接取「資料裡最後 3 季」會抓到比目標還新的季度,年化就錯位了。
      傳入 before_quarter(= 試算季度標籤)後,只取「嚴格早於它」的 3 季,才正確。
      季度字串 '2024Q4' < '2025Q1' 的字典序剛好等於時間序,可直接比較。

    回傳:(EPS 加總, 使用的季度標籤清單)
    """
    seq = quarters
    if before_quarter:
        earlier = [q for q in quarters if q.quarter < before_quarter]
        if earlier:  # 有早於目標的季度才用;否則退回「資料裡最後 n 季」
            seq = earlier
    recent = seq[-n:]
    total = sum(q.reported_eps for q in recent)
    return total, [q.quarter for q in recent]


def merge_supplement(
    base: list[QuarterFinancials],
    supplement_path: str | Path,
) -> tuple[list[QuarterFinancials], set[str]]:
    """把「補充 CSV」合併進 base，用來補上 API 尚未收錄、但已公布的季度。

    典型情境:法說會剛開完、當季實際數已公布,但 FinMind 還沒收錄該季財報。
    這時把該季手動填進 data/financials_supplement.csv,就能讓年化(TTM)用到最新一季。

    規則:同季度以 supplement 覆蓋 base、新季度則附加,最後重新排序。
    回傳:(合併後清單, 被補充/覆蓋的季度標籤集合)
    """
    path = Path(supplement_path)
    if not path.exists():
        return base, set()
    try:
        extra = load_financials_csv(path)
    except (FileNotFoundError, ValueError):
        return base, set()

    by_q = {q.quarter: q for q in base}
    labels: set[str] = set()
    for q in extra:
        by_q[q.quarter] = q
        labels.add(q.quarter)
    merged = sorted(by_q.values(), key=lambda x: x.quarter)
    return merged, labels


# ======================================================================
# B. 手動:近 10 年本益比高低區間 CSV
# ======================================================================
def load_pe_history_csv(path: str | Path) -> PEBand:
    """讀取近 10 年 (或更多) 本益比高低 CSV,統計出 low / mid / high。

    期望欄位:year, pe_high, pe_low, source
    統計方式:
        pe_low  = 期間所有『年度低點』裡的最小值 (最保守)
        pe_high = 期間所有『年度高點』裡的最大值 (最樂觀)
        pe_mid  = 所有年度 (高+低)/2 的平均 (常態中樞)
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到本益比歷史 CSV:{path}")

    highs: list[float] = []
    lows: list[float] = []
    years: list[int] = []
    source = ""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            y = (r.get("year") or "").strip()
            if not y or y.startswith("#"):
                continue
            highs.append(float(r["pe_high"]))
            lows.append(float(r["pe_low"]))
            years.append(int(float(y)))
            if not source:
                source = (r.get("source") or "").strip()

    if not highs:
        raise ValueError(f"本益比 CSV 沒有有效資料列:{path}")

    pe_low = min(lows)
    pe_high = max(highs)
    pe_mid = statistics.mean((h + l) / 2 for h, l in zip(highs, lows))
    years_covered = f"{min(years)}–{max(years)},共 {len(years)} 年"

    return PEBand(
        pe_low=round(pe_low, 1),
        pe_mid=round(pe_mid, 1),
        pe_high=round(pe_high, 1),
        years_covered=years_covered,
        source=source or "(未標註來源)",
    )


# ======================================================================
# C. 自動抓取(一)近 8 季財務數據 — FinMind
# ======================================================================
# FinMind 綜合損益表(TaiwanStockFinancialStatements)實測回傳的 type 名稱:
#   Revenue                            營業收入
#   GrossProfit                        營業毛利
#   OperatingExpenses                  營業費用
#   OperatingIncome                    營業利益
#   TotalNonoperatingIncomeAndExpense  營業外收入及支出  ← 業外收支(直接給,免估算)
#   PreTaxIncome                       稅前淨利
#   TAX                                所得稅費用
#   EquityAttributableToOwnersOfParent 母公司業主淨利   ← EPS 就是用這個算的
#   EPS                                基本每股盈餘
# 因為每個欄位都拿得到,回測誤差可壓到 <0.3%(僅少數點來自少數股權)。

# FinMind 綜合損益表科目 → 我們要的欄位,對照用
_FS_KEYS = {
    "revenue": "Revenue",
    "gross_profit": "GrossProfit",
    "opex": "OperatingExpenses",
    "op_income": "OperatingIncome",
    "non_op": "TotalNonoperatingIncomeAndExpense",
    "pretax": "PreTaxIncome",
    "tax": "TAX",
    "parent_ni": "EquityAttributableToOwnersOfParent",
    "eps": "EPS",
}


def _quarter_label_from_date(date_str: str) -> str:
    """把財報季末日期 'YYYY-MM-DD' 轉成 '2024Q3'。"""
    y = int(date_str[:4])
    m = int(date_str[5:7])
    return f"{y}Q{(m - 1) // 3 + 1}"


def fetch_financials_finmind(
    stock_id: str = "2330",
    last_n: int = 8,
    start_date: str = "2023-01-01",
) -> list[QuarterFinancials]:
    """用 FinMind 抓近 N 季綜合損益表 → list[QuarterFinancials]。

    衍生邏輯(全部來自同一張損益表,無估算):
        revenue_twd_bn   = Revenue / 1e9
        gross_margin_pct = GrossProfit / Revenue × 100
        opex_ratio_pct   = OperatingExpenses / Revenue × 100
        non_op_ratio_pct = TotalNonoperatingIncomeAndExpense / Revenue × 100
        tax_rate_pct     = TAX / PreTaxIncome × 100
        shares_bn        = 母公司業主淨利 / EPS / 1e9(用 EPS 反推最精準)
        reported_eps     = EPS

    失敗(網路/額度/欄位缺)會 raise,由 main.py 決定退回手動 CSV。
    """
    # --- 先看快取(12 小時內不重抓;財報季更新,12h 綽綽有餘) ---
    key = f"finmind_fs_{stock_id}"
    cached = cache_get(key, ttl_seconds=12 * 3600)
    if cached:
        piv = cached["data"]
        fetched_date = cached["fetched_date"]
    else:
        dl = _finmind_loader()
        df = dl.taiwan_stock_financial_statement(stock_id=stock_id, start_date=start_date)
        if df is None or len(df) == 0:
            raise RuntimeError("FinMind 未回傳財報資料(可能離線或超出額度)")
        # 轉成 {季末日期: {科目: 數值}} 存快取(純 dict,JSON 友善)
        piv: dict[str, dict[str, float]] = {}
        for _, r in df.iterrows():
            piv.setdefault(str(r["date"]), {})[str(r["type"])] = float(r["value"])
        obj = cache_set(key, piv)
        fetched_date = obj["fetched_date"]

    rows = quarters_from_income_pivot(piv, last_n=last_n, fetched_date=fetched_date)
    if not rows:
        raise RuntimeError("FinMind 有回應但解析不到完整季度,建議改用手動 CSV")
    return rows


def quarters_from_income_pivot(
    piv: dict, last_n: int = 8, fetched_date: str = "", source: str | None = None
) -> list[QuarterFinancials]:
    """把 FinMind 綜合損益表 pivot({季末日期:{科目:值}})→ 近 N 季 QuarterFinancials。

    衍生邏輯全部來自同一張損益表(無估算):
        revenue_twd_bn   = Revenue / 1e9
        gross_margin_pct = GrossProfit / Revenue × 100
        opex_ratio_pct   = OperatingExpenses / Revenue × 100
        non_op_ratio_pct = TotalNonoperatingIncomeAndExpense / Revenue × 100
        tax_rate_pct     = TAX / PreTaxIncome × 100
        shares_bn        = 母公司業主淨利 / EPS / 1e9(用 EPS 反推最精準)
        reported_eps     = EPS
    抽成獨立函式,讓「近8季報表」與「長區間河流圖/FCF」共用同一份 pivot,不重複打 API。
    """
    src = source or (f"FinMind 財報 (抓取 {fetched_date})" if fetched_date else "FinMind 財報")
    dates = sorted(piv.keys())[-last_n:]  # 由舊到新,取最後 N 季
    rows: list[QuarterFinancials] = []
    for d in dates:
        t = piv[d]
        rev = t.get(_FS_KEYS["revenue"])
        gp = t.get(_FS_KEYS["gross_profit"])
        ox = t.get(_FS_KEYS["opex"])
        non_op = t.get(_FS_KEYS["non_op"])
        pre = t.get(_FS_KEYS["pretax"])
        tax = t.get(_FS_KEYS["tax"])
        parent_ni = t.get(_FS_KEYS["parent_ni"])
        eps = t.get(_FS_KEYS["eps"])

        # 缺營收或 EPS 就無法計算,跳過該季
        if not rev or not eps:
            continue

        shares_bn = (parent_ni / eps) / 1e9 if parent_ni else 0.0
        gross_margin_pct = gp / rev * 100.0 if gp else 0.0
        opex_ratio_pct = ox / rev * 100.0 if ox else 0.0
        non_op_ratio_pct = non_op / rev * 100.0 if non_op is not None else 0.0
        tax_rate_pct = tax / pre * 100.0 if (tax is not None and pre) else 0.0

        rows.append(
            QuarterFinancials(
                quarter=_quarter_label_from_date(d),
                revenue_twd_bn=round(rev / 1e9, 4),
                gross_margin_pct=round(gross_margin_pct, 2),
                opex_ratio_pct=round(opex_ratio_pct, 2),
                tax_rate_pct=round(tax_rate_pct, 2),
                shares_bn=round(shares_bn, 4),
                non_op_ratio_pct=round(non_op_ratio_pct, 2),
                reported_eps=round(eps, 2),
                source=src,
            )
        )
    return rows


# 對外仍用 fetch_financials_auto 這個名字(main.py 呼叫),內部改走 FinMind
def fetch_financials_auto(ticker: str = "2330", last_n: int = 8) -> list[QuarterFinancials]:
    """自動抓財務數據(FinMind)。ticker 可帶 '.TW',會自動去掉。"""
    stock_id = ticker.replace(".TWO", "").replace(".TW", "")
    return fetch_financials_finmind(stock_id=stock_id, last_n=last_n)


# ======================================================================
# D. 自動抓取(二)近 10 年本益比 — TWSE 個股日本益比(BWIBBU)
# ======================================================================
_TWSE_BWIBBU = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU"


def _twse_fetch_month(stock_id: str, year: int, month: int) -> list[float]:
    """抓某一個月的每日本益比清單(浮點數)。

    重點:TWSE 的欄位順序會隨年代不同!(2016 年 '本益比' 在第 1 欄,
          2026 年在第 3 欄)所以一定要用「欄名」定位,不能用固定索引。
    """
    import requests  # 延遲匯入:手動模式不需要

    url = f"{_TWSE_BWIBBU}?date={year}{month:02d}01&stockNo={stock_id}&response=json"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK":
        return []
    fields = j.get("fields") or []
    data = j.get("data") or []
    if "本益比" not in fields:
        return []
    idx = fields.index("本益比")  # ← 用欄名定位,關鍵!
    out: list[float] = []
    for row in data:
        try:
            v = float(str(row[idx]).replace(",", "").strip())
            if v > 0:  # 濾掉 '-'、0、負值(EPS 為負或無資料)
                out.append(v)
        except (ValueError, IndexError):
            continue
    return out


def fetch_pe_history_twse(
    stock_id: str = "2330",
    years: int = 10,
    polite_sleep: float = 0.35,
) -> tuple[PEBand, list[dict]]:
    """抓近 N 年每日本益比,聚合成年度高/低/平均,回傳 (PEBand, 年度明細)。

    - 逐月抓(每個 request 回傳一整月的每日值),過去月份永久快取、當月短快取。
    - 任一月份失敗只跳過該月,不讓整體壞掉;全部抓不到才 raise。
    """
    today = date.today()
    cur_year, cur_month = today.year, today.month
    start_year = cur_year - years + 1
    fetched_date = today.strftime("%Y-%m-%d")

    per_year: dict[int, list[float]] = {}
    for y in range(start_year, cur_year + 1):
        for m in range(1, 13):
            if y == cur_year and m > cur_month:
                break  # 未來月份還沒發生
            is_current = (y == cur_year and m == cur_month)
            key = f"twse_bwibbu_{stock_id}_{y}{m:02d}"
            # 過去月份不會變 → 永久快取(ttl=None);當月 → 6 小時
            cached = cache_get(key, ttl_seconds=(6 * 3600 if is_current else None))
            if cached is not None:
                month_vals = cached["data"]
            else:
                try:
                    month_vals = _twse_fetch_month(stock_id, y, m)
                except Exception:  # noqa: BLE001 — 單月失敗就跳過,不影響其他月
                    month_vals = []
                cache_set(key, month_vals)
                time.sleep(polite_sleep)  # 對 TWSE 禮貌一點,避免被擋
            if month_vals:
                per_year.setdefault(y, []).extend(month_vals)

    # --- 聚合 ---
    year_rows: list[dict] = []
    all_daily: list[float] = []
    for y in sorted(per_year):
        vals = per_year[y]
        if not vals:
            continue
        year_rows.append({
            "year": y,
            "pe_high": round(max(vals), 2),
            "pe_low": round(min(vals), 2),
            "pe_avg": round(sum(vals) / len(vals), 2),
            "days": len(vals),
        })
        all_daily.extend(vals)

    if not all_daily:
        raise RuntimeError("TWSE 未取得任何本益比資料(可能被限流或離線)")

    pe_low = min(all_daily)
    pe_high = max(all_daily)
    pe_mid = statistics.mean(yr["pe_avg"] for yr in year_rows)  # 各年日均值的平均
    years_covered = f"{year_rows[0]['year']}–{year_rows[-1]['year']},共 {len(year_rows)} 年"

    band = PEBand(
        pe_low=round(pe_low, 1),
        pe_mid=round(pe_mid, 1),
        pe_high=round(pe_high, 1),
        years_covered=years_covered,
        source=f"TWSE 個股日本益比 BWIBBU (抓取 {fetched_date})",
    )
    return band, year_rows


def save_pe_history_csv(year_rows: list[dict], path: str | Path, source: str) -> None:
    """把 TWSE 聚合後的年度本益比存成 CSV(方便檢查,也能當手動 fallback)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["year", "pe_high", "pe_low", "pe_avg", "days", "source"])
        for yr in year_rows:
            writer.writerow([
                yr["year"], yr["pe_high"], yr["pe_low"], yr["pe_avg"], yr["days"], source,
            ])


def fetch_current_price_twse(stock_id: str = "2330") -> tuple[float, str, str]:
    """抓最新收盤價(TWSE STOCK_DAY 個股每日成交)。

    回傳:(收盤價, 日期 'YYYY-MM-DD', 來源字串)。
    快取 1 小時(盤中會變),失敗會 raise 由 main.py 決定略過現價比較。
    """
    import requests  # 延遲匯入

    today = date.today()
    ym = today.strftime("%Y%m")
    key = f"twse_price_{stock_id}_{ym}"
    cached = cache_get(key, ttl_seconds=3600)  # 1 小時
    if cached is not None:
        rows = cached["data"]
    else:
        url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
               f"?date={ym}01&stockNo={stock_id}&response=json")
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        if j.get("stat") != "OK":
            raise RuntimeError("TWSE 未回傳當月股價")
        fields = j.get("fields") or []
        data = j.get("data") or []
        if "收盤價" not in fields:
            raise RuntimeError("TWSE 股價欄位找不到『收盤價』")
        idx = fields.index("收盤價")  # 用欄名定位
        rows = [[row[0], row[idx]] for row in data]
        cache_set(key, rows)

    # 從最後一筆往前找到第一個可解析的收盤價
    for dstr, close in reversed(rows):
        try:
            price = float(str(close).replace(",", "").strip())
            # 民國日期 '115/07/16' → 西元 '2026-07-16'
            p = dstr.split("/")
            iso = f"{int(p[0]) + 1911}-{p[1]}-{p[2]}"
            return price, iso, f"TWSE 收盤價 {iso}"
        except (ValueError, IndexError):
            continue
    raise RuntimeError("TWSE 股價解析失敗")


# ======================================================================
# E. 驗證:API 近 8 季 vs 原始手動 CSV(差異 > 門檻就列警告)
# ======================================================================
def validate_against_csv(
    api_quarters: list[QuarterFinancials],
    csv_path: str | Path,
    threshold_pct: float = 2.0,
) -> list[dict]:
    """把 API 抓的每季數字,和你原本手動 CSV 的同一季逐欄比對。

    回傳「差異 > threshold_pct%」的警告清單,讓你人工核對誰對誰錯。
    找不到原始 CSV(或無重疊季度)就回空清單。
    """
    try:
        csv_quarters = load_financials_csv(csv_path)
    except (FileNotFoundError, ValueError):
        return []

    csv_map = {q.quarter: q for q in csv_quarters}
    fields = [
        ("revenue_twd_bn", "營收(十億台幣)"),
        ("gross_margin_pct", "毛利率%"),
        ("opex_ratio_pct", "營業費用率%"),
        ("tax_rate_pct", "稅率%"),
        ("shares_bn", "股數(十億股)"),
        ("non_op_ratio_pct", "業外比%"),
        ("reported_eps", "EPS"),
    ]

    warnings: list[dict] = []
    for aq in api_quarters:
        cq = csv_map.get(aq.quarter)
        if cq is None:
            continue  # 該季原 CSV 沒有,無從比對
        for attr, label in fields:
            api_v = getattr(aq, attr)
            csv_v = getattr(cq, attr)
            if not csv_v:  # 原值為 0 / 空,略過避免除以 0
                continue
            diff_pct = (api_v - csv_v) / abs(csv_v) * 100.0
            if abs(diff_pct) > threshold_pct:
                warnings.append({
                    "quarter": aq.quarter,
                    "field": label,
                    "csv": csv_v,
                    "api": api_v,
                    "diff_pct": diff_pct,
                })
    return warnings


def save_financials_csv(quarters: list[QuarterFinancials], path: str | Path) -> None:
    """把(自動抓取的)財務數據存成 CSV,方便你事後檢查/微調/當作下次的手動來源。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "quarter", "revenue_twd_bn", "gross_margin_pct", "opex_ratio_pct",
            "tax_rate_pct", "shares_bn", "non_op_ratio_pct", "reported_eps", "source",
        ])
        for q in quarters:
            writer.writerow([
                q.quarter, q.revenue_twd_bn, q.gross_margin_pct, q.opex_ratio_pct,
                q.tax_rate_pct, q.shares_bn, q.non_op_ratio_pct, q.reported_eps, q.source,
            ])


# ======================================================================
# F. 估值儀表板原料:yfinance(分析師共識EPS、現金流FCF、EV/EBITDA元件)
# ======================================================================
def fetch_yfinance_metrics(ticker: str = "2330.TW") -> tuple[dict, str]:
    """一次抓齊儀表板需要的 yfinance 原始數據(TWD 口徑),回傳 (dict, 抓取日期)。

    抓的東西:
      - 分析師共識 EPS:當季(0q)、今年(0y)、明年(+1y) 平均值 + 分析師家數
      - info:EBITDA、總負債、現金、流通股數、市值(yfinance 版)
      - 季度現金流:近4季 營運現金流 / 資本支出 / 自由現金流(FCF)

    設計:
      - 12 小時快取(共識/財報不會分秒變),省流量也快。
      - 每個區塊各自 try/except,部分失敗不影響其他(缺的欄位給 None)。
      - 完全抓不到才 raise,由 main.py 決定退回 config 手填。
    """
    key = f"yf_metrics_{ticker}"
    cached = cache_get(key, ttl_seconds=12 * 3600)
    if cached is not None:
        return cached["data"], cached["fetched_date"]

    import yfinance as yf  # 延遲匯入

    t = yf.Ticker(ticker)
    out: dict = {}

    # --- 分析師共識 EPS ---
    try:
        ee = t.earnings_estimate

        def _eps(period):
            if ee is not None and period in ee.index:
                v = ee.loc[period, "avg"]
                n = ee.loc[period, "numberOfAnalysts"]
                return (float(v) if v == v else None,
                        int(n) if n == n else None)  # v==v 濾 NaN
            return None, None

        out["eps_q0"], out["n_q0"] = _eps("0q")
        out["eps_y0"], out["n_y0"] = _eps("0y")
        out["eps_y1"], out["n_y1"] = _eps("+1y")
    except Exception:  # noqa: BLE001
        pass

    # --- info:EBITDA / 負債 / 現金 / 股數 ---
    try:
        info = t.info
        for k in ("ebitda", "totalDebt", "totalCash", "sharesOutstanding",
                  "marketCap", "freeCashflow"):
            v = info.get(k)
            out[k] = float(v) if isinstance(v, (int, float)) else None
    except Exception:  # noqa: BLE001
        pass

    # --- 季度現金流:近4季 FCF ---
    try:
        cf = t.quarterly_cashflow

        def _sum4(name):
            if cf is not None and name in cf.index:
                s = cf.loc[name].dropna().iloc[:4]
                return float(s.sum()) if len(s) else None
            return None

        ocf = _sum4("Operating Cash Flow")
        capex = _sum4("Capital Expenditure")   # 通常為負
        fcf = _sum4("Free Cash Flow")
        if fcf is None and ocf is not None and capex is not None:
            fcf = ocf + capex
        out["ocf_ttm"], out["capex_ttm"], out["fcf_ttm"] = ocf, capex, fcf
    except Exception:  # noqa: BLE001
        pass

    if not out:
        raise RuntimeError("yfinance 未回傳任何儀表板數據(可能離線或限流)")

    obj = cache_set(key, out)
    return out, obj["fetched_date"]


# ======================================================================
# G. 共識 EPS 監控:歷史記錄 CSV(每次抓取都記一列,供比較上修/下修)
# ======================================================================
def load_consensus_history(path: str | Path) -> list[dict]:
    """讀共識歷史 CSV(不存在就回空清單)。"""
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def record_consensus_history(
    path: str | Path,
    eps_y0: float | None,
    eps_y1: float | None,
    growth_pct: float | None,
    source: str,
    as_of: str | None = None,
) -> dict | None:
    """把本次共識快照 append 到歷史 CSV,回傳「上一筆」(供比較上修/下修/持平)。

    採「每次執行都記一列」的監控日誌設計;共識沒變就會連續出現相同值(顯示持平)。
    """
    from datetime import datetime as _dt

    as_of = as_of or _dt.now().strftime("%Y-%m-%d %H:%M")
    rows = load_consensus_history(path)          # append 前先讀「上一筆」
    prev = rows[-1] if rows else None

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["datetime", "eps_y0", "eps_y1", "growth_pct", "source"])
        w.writerow([as_of, eps_y0, eps_y1, growth_pct, source])
    return prev


# ======================================================================
# H. 視覺化儀表板原料(--html 用):長區間財報 / 資產負債 / 現金流 / 日股價
# ----------------------------------------------------------------------
# 這些都用 FinMind,一次抓長區間(約 10 年)再由上層切用,並全部走 cache.py:
#   - 財報(綜合損益)   長區間 → 河流圖 TTM EPS、FCF 品質的營收/毛利(算 COGS)
#   - 資產負債表         長區間 → FCF 品質的存貨、應收帳款(算存貨天數/應收天數)
#   - 現金流量表         長區間 → FCF 品質的營運現金流 OCF、資本支出 Capex
#   - 日收盤價           長區間 → 河流圖的股價線 + 現價標記
# 皆快取 12 小時(財報季更新,綽綽有餘);失敗一律 raise,由上層決定略過該圖。
# ======================================================================
def _finmind_pivot(
    method: str,
    stock_id: str,
    start_date: str,
    cache_key: str,
    ttl_seconds: int = 12 * 3600,
) -> tuple[dict, str]:
    """通用:抓 FinMind『date/type/value』型資料表 → {季末日期: {科目: 值}}。

    財報 / 資產負債 / 現金流三張表結構相同(都有 date、type、value),故共用。
    回傳 (pivot, 抓取日期)。快取命中就不連網。
    """
    cached = cache_get(cache_key, ttl_seconds=ttl_seconds)
    if cached is not None:
        return cached["data"], cached["fetched_date"]

    dl = _finmind_loader()
    fn = getattr(dl, method)
    df = fn(stock_id=stock_id, start_date=start_date)
    if df is None or len(df) == 0:
        raise RuntimeError(f"FinMind {method} 未回傳資料(可能離線或超出額度)")
    piv: dict[str, dict[str, float]] = {}
    for _, r in df.iterrows():
        try:
            piv.setdefault(str(r["date"]), {})[str(r["type"])] = float(r["value"])
        except (TypeError, ValueError):
            continue
    obj = cache_set(cache_key, piv)
    return piv, obj["fetched_date"]


def fetch_income_pivot(stock_id: str = "2330", start_date: str = "2014-01-01") -> tuple[dict, str]:
    """長區間綜合損益表 pivot(河流圖 EPS/TTM、FCF 品質的營收與 COGS 用)。"""
    return _finmind_pivot(
        "taiwan_stock_financial_statement", stock_id, start_date, f"finmind_fs_long_{stock_id}"
    )


def fetch_balance_pivot(stock_id: str = "2330", start_date: str = "2014-01-01") -> tuple[dict, str]:
    """長區間資產負債表 pivot(FCF 品質的存貨、應收帳款用)。"""
    return _finmind_pivot(
        "taiwan_stock_balance_sheet", stock_id, start_date, f"finmind_bs_{stock_id}"
    )


def fetch_cashflow_pivot(stock_id: str = "2330", start_date: str = "2014-01-01") -> tuple[dict, str]:
    """長區間現金流量表 pivot(FCF 品質的 OCF、Capex 用)。

    注意:現金流量表是『年初至今累計(YTD)』,每年 Q1 歸零重算,
    故要取『全年』數字時,請用該年 12-31(第4季)的累計值(見 fcf_quality.py)。
    """
    return _finmind_pivot(
        "taiwan_stock_cash_flows_statement", stock_id, start_date, f"finmind_cf_{stock_id}"
    )


def fetch_price_daily_finmind(
    stock_id: str = "2330", start_date: str = "2015-01-01"
) -> tuple[list[dict], str]:
    """FinMind 日收盤價(河流圖股價線用)。一次抓約 10 年,回傳 ([{date, close}], 抓取日期)。

    快取 12 小時。相較 TWSE 逐月抓,FinMind 單一請求即可拿到整段日線,快又省。
    """
    key = f"finmind_price_{stock_id}"
    cached = cache_get(key, ttl_seconds=12 * 3600)
    if cached is not None:
        return cached["data"], cached["fetched_date"]

    dl = _finmind_loader()
    df = dl.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
    if df is None or len(df) == 0:
        raise RuntimeError("FinMind 未回傳日股價(可能離線或超出額度)")
    rows: list[dict] = []
    for _, r in df.iterrows():
        try:
            c = float(r["close"])
        except (TypeError, ValueError, KeyError):
            continue
        if c == c and c > 0:  # 濾 NaN / 非正值
            rows.append({"date": str(r["date"]), "close": round(c, 2)})
    if not rows:
        raise RuntimeError("FinMind 日股價解析不到有效收盤價")
    obj = cache_set(key, rows)
    return rows, obj["fetched_date"]


def fetch_daily_price_value(
    stock_id: str = "2330", start_date: str = "2024-01-01"
) -> tuple[list[dict], str]:
    """FinMind 日收盤 + 成交金額(選股流動性條件用)。回傳 ([{date, close, value}], 抓取日期)。

    value = Trading_money(當日成交金額,新台幣)。快取 12 小時。
    """
    key = f"finmind_pxv_{stock_id}"
    cached = cache_get(key, ttl_seconds=12 * 3600)
    if cached is not None:
        return cached["data"], cached["fetched_date"]

    dl = _finmind_loader()
    df = dl.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
    if df is None or len(df) == 0:
        raise RuntimeError("FinMind 未回傳日股價")
    rows: list[dict] = []
    for _, r in df.iterrows():
        try:
            c = float(r["close"])
            v = float(r.get("Trading_money", 0) or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if c == c and c > 0:  # 濾 NaN
            rows.append({"date": str(r["date"]), "close": round(c, 2), "value": v})
    if not rows:
        raise RuntimeError("FinMind 日股價/成交金額解析不到有效資料")
    obj = cache_set(key, rows)
    return rows, obj["fetched_date"]
