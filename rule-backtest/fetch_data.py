"""
資料採集 (fetch_data.py)
========================
把四檔標的需要的原料抓齊並寫進 cache/,讓 run.py 可以離線、可重現地跑。

  台股:TWSE 每日本益比(免額度,逐月抓)+ FinMind 季財報(有額度)
  美股:yfinance 股價/財報日共識 + SEC EDGAR 季毛利率

FinMind 免費版有每小時上限。撞到時本程式**不會失敗離開**,而是記下來、
其他資料照抓,最後回報「還缺什麼」,稍後再跑一次即可續補(已抓的都在快取)。

用法:
    python3 fetch_data.py           # 抓缺的
    python3 fetch_data.py --wait    # 撞到 FinMind 額度就等待重試,直到補齊
"""

from __future__ import annotations

import sys
import time

import params as P
import sources_tw as TW
import sources_us as US
from prices import fetch_prices


def fetch_one(stock: dict) -> dict:
    """抓單一標的的所有原料,回傳狀態摘要(不中斷)。"""
    code, name, market = stock["code"], stock["name"], stock["market"]
    status = {"code": code, "name": name, "price": False, "pe": False, "fundamentals": False,
              "note": ""}

    # --- 股價(兩市場共用 yfinance)---
    try:
        px = fetch_prices(stock["yf"])
        status["price"] = True
        status["price_range"] = f"{px[0]['date']} ~ {px[-1]['date']} ({len(px)} 日)"
    except Exception as e:  # noqa: BLE001
        status["note"] += f"股價失敗:{e}; "

    if market == "TW":
        # --- TWSE 每日本益比(免額度)---
        try:
            pe = TW.fetch_pe_daily_tw(code)
            if pe:
                status["pe"] = True
                status["pe_range"] = f"{pe[0]['date']} ~ {pe[-1]['date']} ({len(pe)} 日)"
            else:
                status["note"] += "PE 無資料; "
        except Exception as e:  # noqa: BLE001
            status["note"] += f"PE 失敗:{e}; "

        # --- FinMind 季財報(有額度)---
        try:
            q = TW.quarterly_fundamentals_tw(code)
            if q:
                status["fundamentals"] = True
                status["fs_range"] = f"{q[0]['quarter_end']} ~ {q[-1]['quarter_end']} ({len(q)} 季)"
            else:
                status["note"] += "財報無資料; "
        except TW.QuotaExceeded:
            status["note"] += "FinMind 額度用盡(稍後續跑); "
        except Exception as e:  # noqa: BLE001
            status["note"] += f"財報失敗:{e}; "
    else:
        # --- 美股:yfinance 共識/實際 EPS + SEC EDGAR 毛利率 ---
        try:
            q = US.quarterly_fundamentals_us(stock["yf"], stock["cik"])
            n_cons = sum(1 for r in q if r.get("eps_consensus") is not None)
            n_gm = sum(1 for r in q if r.get("gross_margin") is not None)
            if q:
                status["pe"] = True  # 美股 PE 由 EPS 自算,有 EPS 就有 PE
                status["fundamentals"] = True
                status["fs_range"] = (f"{q[0]['quarter_end']} ~ {q[-1]['quarter_end']} "
                                      f"({len(q)} 季;共識 {n_cons} 季、毛利率 {n_gm} 季)")
        except Exception as e:  # noqa: BLE001
            status["note"] += f"美股基本面失敗:{e}; "

    return status


def main() -> int:
    wait = "--wait" in sys.argv
    attempt = 0
    while True:
        attempt += 1
        print(f"\n===== 採集第 {attempt} 輪 =====")
        results = [fetch_one(s) for s in P.UNIVERSE]
        for r in results:
            ok = "OK" if (r["price"] and r["pe"] and r["fundamentals"]) else "缺"
            print(f"[{ok}] {r['code']} {r['name']}: "
                  f"price={r.get('price_range', r['price'])} | "
                  f"pe={r.get('pe_range', r['pe'])} | fs={r.get('fs_range', r['fundamentals'])} "
                  f"{('| ' + r['note']) if r['note'] else ''}")

        missing = [r for r in results if not (r["price"] and r["pe"] and r["fundamentals"])]
        if not missing:
            print("\n全部資料已就緒。")
            return 0
        if not wait:
            print(f"\n尚缺 {len(missing)} 檔的部分資料。FinMind 額度每小時重置,"
                  f"稍後重跑本程式即可續補(已抓的都在 cache/)。")
            return 1
        print("\n等待 10 分鐘後重試(FinMind 額度回補中)…")
        time.sleep(600)


if __name__ == "__main__":
    raise SystemExit(main())
