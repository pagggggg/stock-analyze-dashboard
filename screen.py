"""
選股篩選執行 (screen.py)
========================
讀本地全市場資料(data/universe/,由 fetch_universe.py 抓好)→ 套 config 門檻兩層篩選
→ 產出 reports/screener_result.md。

用法:
    python fetch_universe.py       # (先)抓全市場存本地,首次較久
    python screen.py               # (後)讀本地、跑篩選、出報告(快,可反覆調門檻重跑)
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from src.screener import load_config, load_records, screen_all
from src.screener_report import build_screener_report

ROOT = Path(__file__).resolve().parent


def run(args) -> None:
    cfg = load_config(args.config)
    records = load_records(ROOT / "data/universe")
    if not records:
        raise SystemExit("找不到本地全市場資料(data/universe/ 為空)。"
                         "請先執行:python fetch_universe.py")

    results, funnel = screen_all(records, cfg)

    deep = sum(1 for r in records if "annual" in r)
    universe_desc = (f"本地 {len(records)} 檔（深抓財報 {deep} 檔）"
                     f"｜市場 {cfg['universe']['market']}")
    md = build_screener_report(results, funnel, cfg,
                               datetime.now().strftime("%Y-%m-%d %H:%M"), universe_desc)

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    print(f"讀本地 {len(records)} 檔 → 通過第一層 {funnel['layer1_pass']}、"
          f"兩層全過 {funnel['both_pass']}")
    print(f"報告輸出:{out}")


def main() -> None:
    p = argparse.ArgumentParser(description="兩層選股篩選(讀本地資料 → screener_result.md)")
    p.add_argument("--config", default=str(ROOT / "config/screener.yaml"))
    p.add_argument("--out", default="reports/screener_result.md")
    run(p.parse_args())


if __name__ == "__main__":
    main()
