"""
資料抓取總管(fetch_data.py)
============================
把階段一需要的所有資料抓齊並快取:
  - 三雄 + 對照(0050、2330)的:日收盤價、季財報、每日 PBR
  - 運價指數:container(940796/940798)、BDI(940793)
跑完印出每個資料集的「筆數 + 日期範圍」供人工確認資料長度(呼應需求:先確認資料可得性)。
可重複執行(全走快取)。
"""
from __future__ import annotations

import sources_finmind as F
import sources_freight as G
from util import load_config


def _span(rows, dk="date"):
    if not rows:
        return "0 筆"
    return f"{len(rows)} 筆 [{rows[0][dk]} → {rows[-1][dk]}]"


def main() -> None:
    cfg = load_config()
    stocks = [s["id"] for s in cfg["universe"]["phase1_targets"]]
    stocks += [cfg["universe"]["controls"]["benchmark"]["id"],
               cfg["universe"]["controls"]["overfit"]["id"]]

    print("=== FinMind:股價 / 財報 / PBR ===")
    for sid in stocks:
        px = F.fetch_prices(sid)
        fin = F.fetch_financials(sid)
        pbr = F.fetch_pbr(sid)
        gm = [r["gross_margin"] for r in fin if r["gross_margin"] is not None]
        print(f"  {sid}: 價 {_span(px)} | 財報 {_span(fin)} (毛利率有值 {len(gm)} 季) | PBR {_span(pbr)}")

    print("\n=== investing.com:運價指數(週線)===")
    fr = cfg["freight"]
    for key in ("container", "container_alt", "bdi"):
        pid = fr[key]["pair_id"]
        rows = G.fetch_freight(pid)
        print(f"  {fr[key]['label']} (#{pid}): {_span(rows)}")


if __name__ == "__main__":
    main()
