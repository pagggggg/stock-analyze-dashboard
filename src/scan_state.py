"""
訊號狀態引擎 (scan_state.py)
============================
把「本次掃描」和「上次狀態」相比,萃取出『訊號級』變化,並決定頂端狀態燈。

三層哲學的第一、二層就靠這裡:
  第一層 頂端狀態燈:
     🟢 綠 = 無訊號級變化
     🟡 黃 = 有共識EPS異動,或 FCF 品質燈變色
     🔴 紅 = 有股票『跨越估值門檻』(前瞻PE 判讀等級改變,如 合理→貴)
  第二層 訊號流水:
     只收「共識上下修 / FCF 燈變色 / 估值門檻跨越」這類事件,
     **不放股價漲跌雜訊**(股價每天在動,不是訊號)。

狀態持久化:`data/scan_state.json`(每檔上次快照)、`data/signal_log.csv`(事件日誌)。
在 GitHub Actions 每日重跑時,這兩個檔會被 commit 回 repo,隔天才能和今天比。

★ 門檻皆為經驗法則,寫死於此供調整;修正動能欄位僅為『標記』,
  依需求「等回測驗證後才加權重」,目前不納入任何評分。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 共識EPS 視為「異動」的最小相對變化(濾掉 yfinance 每日微幅浮動雜訊)
_CONSENSUS_MIN_PCT = 0.5
# 修正動能:近期共識上修/下修的判定門檻(%)
_MOMENTUM_MIN_PCT = 0.5

_LIGHT_ZH = {"green": "綠", "yellow": "黃", "red": "紅", "gray": "灰"}


@dataclass
class Event:
    """一則訊號流水事件。"""

    date: str
    stock_id: str
    name: str
    kind: str      # consensus / fcf / valuation
    level: str     # yellow / red
    message: str


# ---- 狀態存取 --------------------------------------------------------
def load_state(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: str | Path, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- 事件日誌 --------------------------------------------------------
def append_signal_log(path: str | Path, events: list[Event]) -> None:
    if not events:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_header = not p.exists()
    with p.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["date", "stock_id", "name", "kind", "level", "message"])
        for e in events:
            w.writerow([e.date, e.stock_id, e.name, e.kind, e.level, e.message])


def load_signal_log(path: str | Path, limit: int = 40) -> list[dict]:
    """讀事件日誌,回傳最近 limit 筆(新到舊)。"""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return list(reversed(rows))[:limit]


# ---- 修正動能(給掃描總表的欄位)------------------------------------
def revision_momentum(consensus_history: list[dict]) -> tuple[str, float | None]:
    """由每檔共識歷史,算『今年FY共識EPS』相對上一個不同值的變化。

    回傳 (方向, 變化%):方向 = up / down / flat / na。
    僅作『標記近期被上修的股票』用,不加權(等回測驗證後再談)。
    """
    vals: list[float] = []
    for r in consensus_history:
        v = r.get("eps_y0")
        try:
            f = float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            f = None
        if f is not None:
            vals.append(f)
    if len(vals) < 2:
        return "na", None
    cur = vals[-1]
    # 往回找第一個和現值「明顯不同」的舊值
    prev = None
    for v in reversed(vals[:-1]):
        if cur == 0:
            break
        if abs(cur - v) / abs(cur) * 100.0 >= _MOMENTUM_MIN_PCT:
            prev = v
            break
    if prev is None:
        return "flat", 0.0
    pct = (cur - prev) / abs(prev) * 100.0
    return ("up" if pct > 0 else "down"), round(pct, 1)


# ---- 核心:比對上次狀態,產生事件 + 狀態燈 --------------------------
def _pct_change(cur, prev) -> float | None:
    try:
        cur = float(cur)
        prev = float(prev)
    except (TypeError, ValueError):
        return None
    if prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100.0


def diff_snapshots(stock_id: str, name: str, prev: dict, cur: dict, today: str) -> list[Event]:
    """比較單檔『上次 vs 本次』快照,產生訊號事件。prev 為空 = 首次(不產生事件)。"""
    if not prev:
        return []
    events: list[Event] = []

    # 1) 共識EPS 上修/下修(今年FY)—— 濾掉微幅浮動
    chg = _pct_change(cur.get("eps_y0"), prev.get("eps_y0"))
    if chg is not None and abs(chg) >= _CONSENSUS_MIN_PCT:
        word = "上修" if chg > 0 else "下修"
        events.append(Event(
            today, stock_id, name, "consensus", "yellow",
            f"共識EPS(今年FY)【{word} {chg:+.1f}%】 {float(prev['eps_y0']):.1f} → {float(cur['eps_y0']):.1f}",
        ))

    # 2) FCF 品質燈變色(存貨/應收/OCF)
    kind_zh = {"dio": "存貨天數", "dso": "應收天數", "ocf": "營運現金流"}
    pl = prev.get("fcf_lights") or {}
    cl = cur.get("fcf_lights") or {}
    for k, zh in kind_zh.items():
        pv, cv = pl.get(k), cl.get(k)
        if pv and cv and pv != cv and "gray" not in (pv, cv):
            events.append(Event(
                today, stock_id, name, "fcf", "yellow",
                f"FCF品質・{zh} 燈號 {_LIGHT_ZH.get(pv, pv)}→{_LIGHT_ZH.get(cv, cv)}",
            ))

    # 3) 跨越估值門檻(前瞻PE 判讀等級改變)—— 紅色
    pv, cv = prev.get("forward_pe_verdict"), cur.get("forward_pe_verdict")
    if pv and cv and pv != cv:
        events.append(Event(
            today, stock_id, name, "valuation", "red",
            f"前瞻PE 判讀【{pv}→{cv}】跨越估值門檻",
        ))

    return events


def compute_signals(
    analyses: list,
    state_path: str | Path,
    log_path: str | Path,
    persist: bool = True,
) -> tuple[str, list[Event], bool]:
    """對整份觀察清單做狀態比對。

    回傳 (status_light, events, first_run):
      status_light : green / yellow / red(頂端狀態燈)
      events       : 本次所有訊號事件
      first_run    : 是否為首次(尚無任何上次狀態)
    會把新狀態寫回 state_path、事件 append 到 log_path(persist=False 則不寫,測試用)。
    """
    prev_state = load_state(state_path)
    first_run = not prev_state
    today = datetime.now().strftime("%Y-%m-%d")

    all_events: list[Event] = []
    new_state: dict = dict(prev_state)  # 保留沒掃到的舊檔
    for a in analyses:
        if not a.ok:
            continue
        cur = a.state_snapshot()
        prev = prev_state.get(a.stock_id, {})
        all_events.extend(diff_snapshots(a.stock_id, a.name, prev, cur, today))
        new_state[a.stock_id] = cur

    # 狀態燈:紅 > 黃 > 綠
    if any(e.level == "red" for e in all_events):
        status = "red"
    elif any(e.level == "yellow" for e in all_events):
        status = "yellow"
    else:
        status = "green"

    if persist:
        save_state(state_path, new_state)
        append_signal_log(log_path, all_events)

    return status, all_events, first_run
