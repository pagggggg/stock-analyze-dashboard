"""
可分析母體建構執行 (build_universe.py)
======================================
產出「基礎池」:對候選股套 4 條母體條件 → 存 config/universe.yaml(供篩選器讀)
+ reports/universe_report.md(通過清單、排除原因統計、邊緣案例)。

用法:
    python build_universe.py --market tw            # 先跑台股(預設用 config 的 tw_test_ids)
    python build_universe.py --market tw --full     # 台股全市場(逐檔 yfinance,較久)
    python build_universe.py --market us            # 再跑美股(us_test_ids)

門檻(市值、覆蓋家數、流動性…)全在 config/screener.yaml → universe_builder,可調。
★ 只用公開市場數據,無持倉/交易紀錄;母體僅界定「可分析範圍」,非買進清單。
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

import yaml

from src.screener import load_config
from src.universe_builder import _U_LABELS, build, fetch_meeting_ids_tw

ROOT = Path(__file__).resolve().parent
UNIVERSE_YAML = ROOT / "config/universe.yaml"
REPORT = ROOT / "reports/universe_report.md"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_tw_stock_map() -> dict[str, dict]:
    """taiwan_stock_info(上市)→ {stock_id: {name, industry}},只留 4 碼普通股。"""
    import re

    from src.data_layer import _finmind_loader
    info = _finmind_loader().taiwan_stock_info()
    m: dict[str, dict] = {}
    for _, r in info.iterrows():
        if str(r["type"]) != "twse":
            continue
        sid = str(r["stock_id"]).strip()
        if re.fullmatch(r"[1-9]\d{3}", sid) and sid not in m:
            m[sid] = {"name": str(r["stock_name"]), "industry": str(r["industry_category"])}
    return m


def _money(v, market):
    if v is None:
        return "—"
    if market == "us":
        return f"{v/1e9:,.1f}十億美元" if v >= 1e9 else f"{v/1e6:,.0f}百萬美元"
    return f"{v/1e8:,.0f}億" if v >= 1e8 else f"{v/1e4:,.0f}萬"


def _build_candidates(market: str, cfg: dict, full: bool) -> list[dict]:
    ub = cfg["universe_builder"]
    if market == "us":
        return [{"stock_id": t, "name": t, "industry": ""} for t in (ub.get("us_test_ids") or [])]
    smap = load_tw_stock_map()
    if full:
        ids = list(smap)
    else:
        ids = [str(x) for x in (ub.get("tw_test_ids") or [])]
    return [{"stock_id": i, "name": smap.get(i, {}).get("name", i),
             "industry": smap.get(i, {}).get("industry", "")} for i in ids]


def _save_universe_yaml(market: str, passed: list) -> dict:
    """把本次通過清單寫進 config/universe.yaml(保留另一市場)。回傳寫入後的全量 dict。"""
    doc = {}
    if UNIVERSE_YAML.exists():
        doc = yaml.safe_load(UNIVERSE_YAML.read_text(encoding="utf-8")) or {}
    doc["generated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    doc[market] = [{"stock_id": r.stock_id, "name": r.name} for r in passed]
    UNIVERSE_YAML.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_YAML.write_text(
        "# 可分析母體(build_universe.py 產出)。篩選器讀這份當基礎池。\n"
        "# 只含『有分析師覆蓋、有法說會、資訊揭露充分』的中大型股。\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return doc


def _write_report(market: str, results: list, stats: dict, cfg: dict, doc: dict) -> None:
    ub = cfg["universe_builder"]
    conf = ub["us" if market == "us" else "tw"]
    mkt_zh = "美股" if market == "us" else "台股"
    A = []
    w = A.append
    w("# 可分析母體建構報告(universe_report.md)")
    w("")
    w(f"> 產生時間:{datetime.now().strftime('%Y-%m-%d %H:%M')}　|　本次市場:**{mkt_zh}**")
    w("")
    w("> ⚠️ 母體只界定「**可分析範圍**」(有分析師覆蓋、有法說會、揭露充分的中大型股),"
      "認知圈外一律排除。**非買進清單**,不構成投資建議。")
    w("")
    # 兩市場通過數(讀 universe.yaml)
    tw_n = len(doc.get("twse", []) or doc.get("tw", []))
    us_n = len(doc.get("us", []))
    w("## 一、母體規模(兩市場)")
    w("")
    w(f"- 台股(twse)母體:**{tw_n}** 檔　|　美股(us)母體:**{us_n}** 檔　"
      f"(存於 `config/universe.yaml`,供篩選器讀取)")
    w("")

    # 門檻
    w(f"## 二、{mkt_zh}門檻(config 可調)")
    w("")
    if market == "us":
        w(f"1. 市值 > **{conf['min_market_cap']/1e9:.0f} 十億美元**")
        w(f"2. 分析師覆蓋 ≥ **{conf['min_analyst_coverage']}** 家(yfinance)")
        w(f"3. 有季度法說(以有季度共識為代理):{conf.get('require_earnings_call', True)}")
        w(f"4. 近{ub['liquidity_days']}日日均成交額 > **{conf['min_avg_value']/1e6:.0f} 百萬美元**")
    else:
        w(f"1. 市值 > **{conf['min_market_cap']/1e8:.0f} 億台幣**")
        w(f"2. 分析師共識覆蓋 ≥ **{conf['min_analyst_coverage']}** 家(yfinance)")
        w(f"3. 近 **{conf['meeting_lookback_days']}** 天有召開法人說明會(公開資訊觀測站 MOPS)")
        w(f"4. 近{ub['liquidity_days']}日日均成交額 > **{conf['min_avg_value']/1e8:.0f} 億台幣**")
    w("")

    # 通過清單
    passed = [r for r in results if r.passed]
    passed.sort(key=lambda r: -(r.market_cap or 0))
    w(f"## 三、{mkt_zh}通過母體清單({len(passed)} / 評估 {stats['total']} 檔)")
    w("")
    if passed:
        w("| 代號 | 名稱 | 產業 | 市值 | 分析師家數 | 近60日日均額 |")
        w("| --- | --- | --- | ---: | ---: | ---: |")
        for r in passed:
            w(f"| {r.stock_id} | {r.name} | {r.industry} | {_money(r.market_cap, market)} | "
              f"{r.n_analysts if r.n_analysts is not None else '—'} | {_money(r.liq_avg, market)} |")
    else:
        w("_(本次無通過標的。)_")
    w("")

    # 排除原因統計
    w("## 四、排除原因統計(哪條刷掉最多)")
    w("")
    w("| 條件 | 未通過(fail) | 資料不足(na) |")
    w("| --- | ---: | ---: |")
    order = sorted(("u1", "u2", "u3", "u4"), key=lambda k: -(stats[k]["fail"] + stats[k]["na"]))
    for k in order:
        s = stats[k]
        w(f"| {_U_LABELS[k]} | {s['fail']} | {s['na']} |")
    w("")
    w("> 註:各條**獨立計**(一檔可能同時卡多條);「通過母體」是 4 條同時成立。"
      "「資料不足」一律**不算通過**(誠實排除,不冒充納入)。")
    w("")

    # 邊緣案例
    edge = stats["edge"]
    edge.sort(key=lambda r: -(r.market_cap or 0))
    w(f"## 五、邊緣案例:僅差一條就進母體({len(edge)} 檔)")
    w("")
    if edge:
        w("| 代號 | 名稱 | 差哪一條 | 該條實況 | 市值 | 分析師 | 日均額 |")
        w("| --- | --- | --- | --- | ---: | ---: | ---: |")
        for r in edge:
            miss_k = next(k for k, c in r.conds.items() if c.status != "pass")
            w(f"| {r.stock_id} | {r.name} | {_U_LABELS[miss_k]} | {r.conds[miss_k].detail} | "
              f"{_money(r.market_cap, market)} | {r.n_analysts if r.n_analysts is not None else '—'} | "
              f"{_money(r.liq_avg, market)} |")
        w("")
        w("> 邊緣案例值得你**人工複核**:也許門檻可微調、或該檔正好在納入邊界。")
    else:
        w("_(本次沒有『僅差一條』的標的。)_")
    w("")

    w("## 六、後續")
    w("")
    w("- 母體已存 `config/universe.yaml`;之後 `fetch_universe.py` / `screen.py` 會以它為基礎池。")
    w("- 門檻覺得太鬆/太嚴,改 `config/screener.yaml → universe_builder` 後重跑本程式即可。")
    w("")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(A), encoding="utf-8")


def run(args) -> None:
    _load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    market = "us" if args.market == "us" else "twse"
    mkt_zh = "美股" if market == "us" else "台股"

    candidates = _build_candidates("us" if market == "us" else "twse", cfg, args.full)
    if not candidates:
        raise SystemExit(f"{mkt_zh}沒有候選股(檢查 config 的 tw_test_ids / us_test_ids 或用 --full)。")

    meeting_ids: set[str] = set()
    if market == "twse":
        try:
            meeting_ids = fetch_meeting_ids_tw(cfg["universe_builder"]["tw"]["meeting_lookback_days"])
            print(f"MOPS 法說會:近一年有 {len(meeting_ids)} 家上市公司召開")
        except Exception as e:  # noqa: BLE001
            print(f"! MOPS 法說會抓取失敗({e}),③ 法說會條件將多為資料不足")

    print(f"{mkt_zh}母體評估:{len(candidates)} 檔(逐檔 yfinance,請稍候)")

    def _prog(i, n, r):
        tag = "✅進池" if r.passed else f"✗ 差{4 - r.n_pass}條"
        print(f"  [{i}/{n}] {r.stock_id} {r.name}　{tag}"
              f"（市值 {_money(r.market_cap, market)}／{r.n_analysts if r.n_analysts is not None else '—'}家／"
              f"日均 {_money(r.liq_avg, market)}）")

    results, stats = build(candidates, market, cfg, meeting_ids, progress=_prog)
    passed = [r for r in results if r.passed]
    doc = _save_universe_yaml(market, passed)
    _write_report(market, results, stats, cfg, doc)

    print("─" * 56)
    print(f"{mkt_zh}母體:通過 {stats['passed']} / {stats['total']} 檔;"
          f"邊緣案例 {len(stats['edge'])} 檔")
    print(f"已寫入:{UNIVERSE_YAML}(供篩選器讀)、{REPORT}")


def main() -> None:
    p = argparse.ArgumentParser(description="可分析母體建構(基礎池)")
    p.add_argument("--config", default=str(ROOT / "config/screener.yaml"))
    p.add_argument("--market", choices=["tw", "us"], default="tw")
    p.add_argument("--full", action="store_true", help="台股全市場(否則用 config 的測試清單)")
    run(p.parse_args())


if __name__ == "__main__":
    main()
