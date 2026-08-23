"""
多股網站產生器 (build_site.py)
==============================
讀 watchlist.yaml → 逐檔分析 → 比對訊號狀態 → 產出靜態網站到 public/。

用法:
    python build_site.py                     # 讀 config/watchlist.yaml,輸出到 public/
    python build_site.py --out public        # 指定輸出資料夾
    python build_site.py --no-record         # 不寫入狀態/共識歷史(本機測試用)

每日自動更新:GitHub Actions 定時跑這支,產出的 public/ 部署到 GitHub Pages,
並把 data/ 底下更新的狀態檔 commit 回 repo(隔天才能和今天比出「上修/下修」)。

★ 只用公開市場數據做估值研究,無任何持倉 / 交易紀錄。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.ai_chain import build_ai_chain_data, load_ai_chain_config
from src.ai_chain_html import build_ai_chain_page
from src.analysis import analyze_stock, analyze_us_record
from src.models import PEBand
from src.scan_state import (Event, append_signal_log, compute_signals, load_signal_log,
                            load_state, save_state)
from src.screener import load_config as load_screener_config, load_records, screen_all
from src.screener_html import build_screener_page
from src.site_html import write_site
from src.thesis import evaluate_thesis, load_thesis
from src.us_data import US_DETAIL_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parent
TW_TZ = timezone(timedelta(hours=8))


def _load_dotenv(path: Path) -> None:
    """極簡 .env 載入(免額外套件):把 KEY=VALUE 塞進 os.environ(不覆蓋既有)。

    本機把 FinMind token 放 .env 就能自動生效;CI 則用 GitHub Secret 注入環境變數,
    不需要 .env。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def load_watchlist(path: str | Path) -> tuple[list[dict], dict]:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return (raw.get("stocks") or []), (raw.get("settings") or {})


def load_universe_stocks() -> list[dict]:
    """讀 config/universe.yaml(可分析母體)的台股清單,並沿用 watchlist 的法說指引對應。"""
    p = ROOT / "config/universe.yaml"
    if not p.exists():
        raise SystemExit("找不到 config/universe.yaml,請先執行 python build_universe.py --market tw")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    items = doc.get("twse") or []
    us_items = doc.get("us") or []
    gmap: dict[str, str] = {}
    try:
        wl, _ = load_watchlist(ROOT / "config/watchlist.yaml")
        gmap = {str(s["stock_id"]): s["guidance"] for s in wl if s.get("guidance")}
    except Exception:  # noqa: BLE001
        pass
    tw = [{"stock_id": str(s["stock_id"]), "name": s.get("name", str(s["stock_id"])),
           "guidance": gmap.get(str(s["stock_id"])), "market": "twse"} for s in items]
    us = []
    for s in us_items:
        sid = str(s["stock_id"])
        record_path = ROOT / f"data/universe/{sid}.json"
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (record.get("detail") or {}).get("schema_version") == US_DETAIL_SCHEMA_VERSION:
            us.append({"stock_id": sid, "name": s.get("name", sid), "market": "us"})
    return tw + us


def _apply_screener_pe_snapshot(analysis, result, record: dict) -> None:
    """Make detail-page labels and river legend use the screener's persisted PE snapshot."""
    metrics = result.metrics
    ph = record.get("pe_hist") or {}
    analysis.trailing_pe = metrics.get("trailing_pe")
    analysis.pe_median = metrics.get("pe_median")
    analysis.pe_p90 = metrics.get("pe_p90")
    analysis.pe_percentile = metrics.get("pe_pct")
    analysis.pe_source_cache_regressed = bool(ph.get("source_cache_regressed"))
    analysis.valuation_flag = metrics.get("flag") or "na"
    snapshot_ok = (ph.get("status") == "ok" and all(ph.get(key) is not None
                   for key in ("p10", "median", "p90", "current_trailing_pe")))
    if snapshot_ok:
        analysis.pe_band = PEBand(
            pe_low=float(ph["p10"]), pe_mid=float(ph["median"]),
            pe_high=float(ph["p90"]),
            years_covered=f"{ph.get('window_start')}–{ph.get('as_of')},rolling {ph.get('years')} 年",
            source="persisted screener PE snapshot")
    if analysis.river is not None and snapshot_ok:
        analysis.river.pe_low = float(ph["p10"])
        analysis.river.pe_mid = float(ph["median"])
        analysis.river.pe_high = float(ph["p90"])
        analysis.river.current_pe = float(ph["current_trailing_pe"])
        analysis.river.source += "；目前 P10/P50/P90 與 trailing PE 採 persisted screener snapshot"


def run(args) -> None:
    _load_dotenv(ROOT / ".env")
    if args.from_universe:
        stocks = load_universe_stocks()
        # 河流圖與篩選器必須使用相同歷史期間；以 screener.yaml 的估值旗標設定
        # 為單一真相來源，避免河流圖與篩選器使用不同視窗。
        scfg = load_screener_config(ROOT / "config/screener.yaml")
        settings = {"pe_years": (scfg.get("valuation_flag") or {}).get("pe_history_years", 5)}
        src = f"母體 universe.yaml({len(stocks)} 檔)"
    else:
        stocks, settings = load_watchlist(args.watchlist)
        src = f"watchlist({len(stocks)} 檔)"
    pe_years = int((settings or {}).get("pe_years", 10))
    if not stocks:
        raise SystemExit("沒有任何股票可分析(檢查 watchlist.yaml 或 universe.yaml)。")
    print(f"儀表板來源:{src}")

    analyses = []
    for i, s in enumerate(stocks, 1):
        sid = str(s["stock_id"]).strip()
        name = s.get("name", sid)
        guidance = s.get("guidance")
        print(f"[{i}/{len(stocks)}] 分析 {sid} {name} …")
        if s.get("market") == "us":
            record_path = ROOT / f"data/universe/{sid}.json"
            if not record_path.exists():
                raise RuntimeError(f"美股母體資料不存在:{record_path}")
            a = analyze_us_record(json.loads(record_path.read_text(encoding="utf-8")), pe_years)
        else:
            a = analyze_stock(sid, name, guidance_path=guidance, pe_years=pe_years)
        status_txt = "OK" if a.ok else "四指標不足"
        print(f"        → {status_txt}"
              + (f";現價 {a.price}" if a.price else "")
              + (f";警告 {len(a.errors)} 則" if a.errors else ""))
        for e in a.errors:
            print(f"          ! {e}")
        analyses.append(a)

    build_now = datetime.now(TW_TZ)
    # 正式更新先把本次快照放進記憶體供圖表、訊號與 thesis 使用；所有頁面成功後才落盤。
    if not args.no_record:
        for a in analyses:
            if a.market == "us" or (a.eps_y0 is None and a.eps_y1 is None):
                continue
            a.consensus_history.append({
                "datetime": build_now.strftime("%Y-%m-%d %H:%M"),
                "eps_y0": a.eps_y0, "eps_y1": a.eps_y1,
                "growth_pct": round(a.growth_pct, 2) if a.growth_pct is not None else None,
                "source": a.consensus_source,
            })

    # 個人 thesis 追蹤。補充檔目前是台積電專用，只在 thesis 標的上合併，
    # 避免把 2330 的最新一季錯套到其他股票。
    thesis_path = ROOT / "config/thesis_2330.yaml"
    if thesis_path.exists():
        from src.data_layer import merge_supplement

        tcfg = load_thesis(thesis_path)
        target = next((a for a in analyses if a.stock_id == str(tcfg["stock_id"])), None)
        if target is None:
            if args.from_universe:
                raise RuntimeError(f"thesis 標的 {tcfg['stock_id']} 不在本次分析清單")
            print(f"[thesis] 略過：{tcfg['stock_id']} 不在本次 watchlist")
        else:
            thesis_quarters, _ = merge_supplement(
                target.quarters, ROOT / "data/financials_supplement.csv")
            target.thesis = evaluate_thesis(tcfg, thesis_quarters, target.consensus_history)
            print(f"[thesis] {target.stock_id}:{target.thesis.status};"
                  f"紅燈 {sum(x.status == 'red' for x in target.thesis.conditions)} 項")

    # 訊號比對 + 狀態燈(寫回 data/scan_state.json、append data/signal_log.csv)
    status, events, first_run = compute_signals(
        analyses,
        state_path=ROOT / "data/scan_state.json",
        log_path=ROOT / "data/signal_log.csv",
        persist=False,
    )
    if args.no_record:
        # 程式碼重建不產生或展示未持久化的「今日事件」；但現存 thesis 紅燈仍須告警。
        events = []
        status = ("red" if any(getattr(a, "thesis", None) and a.thesis.triggered
                               for a in analyses) else "green")
    log_rows = load_signal_log(ROOT / "data/signal_log.csv", limit=40)
    display_events = list(events)
    today = build_now.strftime("%Y-%m-%d")
    for a in analyses:
        thesis = getattr(a, "thesis", None)
        if not thesis:
            continue
        for item in thesis.conditions:
            if item.status != "red" or any(
                    e.kind == "thesis" and item.label in e.message for e in display_events):
                continue
            display_events.append(Event(
                today, a.stock_id, a.name, "thesis", "red",
                f"Thesis 目前仍為紅燈:{item.label}；{item.current_value}；{item.basis}",
            ))

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out

    # 選股篩選頁(有本地全市場資料 data/universe/ 才產生;沒有就略過)
    screener_html = screener_info = ai_chain_html = None
    try:
        recs = load_records(ROOT / "data/universe")
        if recs:
            scfg = load_screener_config(ROOT / "config/screener.yaml")
            sres, sfun = screen_all(recs, scfg)
            by_id = {r.stock_id: r for r in sres}
            record_by_id = {str(rec["stock_id"]): rec for rec in recs}
            for a in analyses:
                r = by_id.get(a.stock_id)
                if not r:
                    continue
                _apply_screener_pe_snapshot(a, r, record_by_id.get(a.stock_id) or {})
            generated = build_now.strftime("%Y-%m-%d %H:%M") + " (台北時間)"
            screener_html = build_screener_page(sres, sfun, scfg, generated)
            screener_info = {"layer1_pass": sfun["layer1_pass"], "both_pass": sfun["both_pass"]}
            try:
                acfg = load_ai_chain_config(ROOT / "config/ai_chain.yaml")
                adata = build_ai_chain_data(
                    acfg, scfg, recs, sres, ROOT / "data/ai_chain_quotes.json",
                    ROOT / "data/ai_chain_tw_quotes.json")
                detail_ids = {a.stock_id for a in analyses if a.ok}
                ai_chain_html = build_ai_chain_page(
                    adata, generated, detail_ids)
                screener_info["ai_layers"] = len(adata["layers"])
                screener_info["ai_unavailable"] = len(adata["unavailable"])
                print(f"[ai-chain] ai-chain.html:{len(adata['layers'])} 層,"
                      f"無法納入 {len(adata['unavailable'])} 檔")
            except Exception as e:  # noqa: BLE001 - 新頁是正式站點的一部分,失敗即中止
                raise RuntimeError(f"AI 產業鏈頁生成失敗:{type(e).__name__}:{e}") from e
            print(f"[screener] screener.html:評估 {len(recs)} 檔,通過第一層 {sfun['layer1_pass']}、"
                  f"兩層全過 {sfun['both_pass']}")
    except Exception as e:  # noqa: BLE001
        print(f"[screener] 略過(無本地資料或錯誤):{e}")

    if ai_chain_html is None:
        raise RuntimeError("AI 產業鏈頁未產生；已中止建站,避免部署缺頁版本")

    stats = write_site(analyses, status, display_events, first_run, log_rows, out,
                       screener_html=screener_html, screener_info=screener_info,
                       ai_chain_html=ai_chain_html,
                       momentum_min_pct=float(
                           scfg.get("layer2", {}).get("momentum", {}).get("min_pct", 0.5)))

    # 所有頁面成功產出後才寫共識與狀態，避免品質不足或建站例外先消耗訊號。
    if not args.no_record:
        from src.data_layer import record_consensus_history

        for a in analyses:
            if a.market == "us" or (a.eps_y0 is None and a.eps_y1 is None):
                continue
            record_consensus_history(
                ROOT / f"data/consensus/{a.stock_id}.csv", a.eps_y0, a.eps_y1,
                round(a.growth_pct, 2) if a.growth_pct is not None else None,
                a.consensus_source, as_of=build_now.strftime("%Y-%m-%d %H:%M"),
            )
        new_state = load_state(ROOT / "data/scan_state.json")
        for a in analyses:
            if (a.ok or getattr(a, "thesis", None)) and getattr(a, "track_signals", True):
                new_state[a.stock_id] = a.state_snapshot(new_state.get(a.stock_id))
        save_state(ROOT / "data/scan_state.json", new_state)
        append_signal_log(ROOT / "data/signal_log.csv", events)

    light = {"green": "🟢綠", "yellow": "🟡黃", "red": "🔴紅"}.get(status, status)
    print("─" * 56)
    print(f"狀態燈:{light}　本次事件:{len(events)} 則"
          + ("(首次建立基準)" if first_run else ""))
    for e in events:
        print(f"   [{e.level}] {e.stock_id} {e.name}:{e.message}")
    print(f"網站輸出:{stats['out']}（首頁 index.html + {stats['details']} 個股詳情頁）")
    print(f"本機預覽:open {out / 'index.html'}")


def main() -> None:
    p = argparse.ArgumentParser(description="多股個人選股分析儀表板網站產生器")
    p.add_argument("--watchlist", default="config/watchlist.yaml", help="觀察清單 YAML")
    p.add_argument("--out", default="public", help="網站輸出資料夾")
    p.add_argument("--from-universe", action="store_true",
                   help="改吃 config/universe.yaml(可分析母體)當儀表板清單,而非 watchlist")
    p.add_argument("--no-record", action="store_true",
                   help="不寫入狀態/共識歷史(本機測試用,避免污染每日狀態)")
    run(p.parse_args())


if __name__ == "__main__":
    main()
