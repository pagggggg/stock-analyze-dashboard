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
import os
from pathlib import Path

import yaml

from src.analysis import analyze_stock
from src.scan_state import compute_signals, load_signal_log
from src.site_html import write_site

ROOT = Path(__file__).resolve().parent


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


def run(args) -> None:
    _load_dotenv(ROOT / ".env")
    stocks, settings = load_watchlist(args.watchlist)
    pe_years = int(settings.get("pe_years", 10))
    if not stocks:
        raise SystemExit("watchlist.yaml 沒有任何股票,請先在 config/watchlist.yaml 填入 stocks。")

    analyses = []
    for i, s in enumerate(stocks, 1):
        sid = str(s["stock_id"]).strip()
        name = s.get("name", sid)
        guidance = s.get("guidance")
        print(f"[{i}/{len(stocks)}] 分析 {sid} {name} …")
        a = analyze_stock(sid, name, guidance_path=guidance, pe_years=pe_years,
                          record_consensus=not args.no_record)
        status_txt = "OK" if a.ok else "四指標不足"
        print(f"        → {status_txt}"
              + (f";現價 {a.price}" if a.price else "")
              + (f";警告 {len(a.errors)} 則" if a.errors else ""))
        for e in a.errors:
            print(f"          ! {e}")
        analyses.append(a)

    # 訊號比對 + 狀態燈(寫回 data/scan_state.json、append data/signal_log.csv)
    status, events, first_run = compute_signals(
        analyses,
        state_path=ROOT / "data/scan_state.json",
        log_path=ROOT / "data/signal_log.csv",
        persist=not args.no_record,
    )
    log_rows = load_signal_log(ROOT / "data/signal_log.csv", limit=40)

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    stats = write_site(analyses, status, events, first_run, log_rows, out)

    light = {"green": "🟢綠", "yellow": "🟡黃", "red": "🔴紅"}.get(status, status)
    print("─" * 56)
    print(f"狀態燈:{light}　本次事件:{len(events)} 則"
          + ("(首次建立基準,不產生事件)" if first_run else ""))
    for e in events:
        print(f"   [{e.level}] {e.stock_id} {e.name}:{e.message}")
    print(f"網站輸出:{stats['out']}（首頁 index.html + {stats['details']} 個股詳情頁）")
    print(f"本機預覽:open {out / 'index.html'}")


def main() -> None:
    p = argparse.ArgumentParser(description="多股個人選股分析儀表板網站產生器")
    p.add_argument("--watchlist", default="config/watchlist.yaml", help="觀察清單 YAML")
    p.add_argument("--out", default="public", help="網站輸出資料夾")
    p.add_argument("--no-record", action="store_true",
                   help="不寫入狀態/共識歷史(本機測試用,避免污染每日狀態)")
    run(p.parse_args())


if __name__ == "__main__":
    main()
