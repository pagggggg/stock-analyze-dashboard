#!/usr/bin/env python3
"""Track protected deployment skips and deployed market-data staleness."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import exchange_calendars as xcals

SCHEMA_VERSION = 1
MARKETS = {
    "tw": ("XTAI", timedelta(minutes=90)),
    "us": ("XNYS", timedelta(minutes=15)),
}


def deployed_market_dates(html: str) -> dict[str, str]:
    match = re.search(
        r"台股行情更新 .*?（收盤日 ([^）]+)）.*?"
        r"美股行情更新 .*?（收盤日 ([^）]+)）",
        html,
        re.S,
    )
    if not match:
        raise ValueError("ai-chain.html 缺少台美股收盤日")
    dates = {}
    for market, value in (("tw", match.group(1)), ("us", match.group(2))):
        candidates = re.findall(r"\d{4}-\d{2}-\d{2}", value)
        if not candidates:
            raise ValueError(f"{market}:收盤日格式錯誤:{value}")
        dates[market] = max(candidates)
    return dates


def ready_session_lag(market: str, deployed: str,
                      now: datetime | None = None) -> dict:
    if market not in MARKETS:
        raise ValueError(f"未知市場:{market}")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now 必須含時區")
    calendar_name, delay = MARKETS[market]
    calendar = xcals.get_calendar(calendar_name)
    start = (datetime.fromisoformat(deployed).date() - timedelta(days=10)).isoformat()
    end = (now.date() + timedelta(days=1)).isoformat()
    schedule = calendar.schedule.loc[start:end]
    ready = []
    for session, row in schedule.iterrows():
        close = row["close"]
        if close.tzinfo is None:
            close = close.tz_localize("UTC")
        if close.to_pydatetime() + delay <= now.astimezone(timezone.utc):
            ready.append(session.date())
    if not ready:
        raise ValueError(f"{market}:找不到已完成交易日")
    deployed_date = datetime.fromisoformat(deployed).date()
    lag = sum(session > deployed_date for session in ready)
    return {"deployed": deployed, "expected": ready[-1].isoformat(), "sessions": lag}


def evaluate_health(previous: dict, quality_ok: bool, deployed: dict[str, str],
                    now: datetime | None = None,
                    run_id: str | None = None) -> tuple[dict, set[str], set[str]]:
    if previous and previous.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("deployment health state schema 不相容")
    old_active = set(previous.get("active_reasons") or [])
    run_id = run_id if run_id is not None else (os.getenv("GITHUB_RUN_ID") or "")
    same_run = bool(run_id and previous.get("last_run_id") == run_id)
    if quality_ok:
        streak = 0
    elif same_run:
        streak = int(previous.get("quality_skip_streak") or 0)
    else:
        streak = int(previous.get("quality_skip_streak") or 0) + 1
    lag = {market: ready_session_lag(market, deployed[market], now)
           for market in MARKETS}
    active = set()
    if streak >= 2:
        active.add("quality-gate")
    for market in MARKETS:
        if lag[market]["sessions"] > 1:
            active.add(f"stale-{market}")
    new = active - old_active
    resolved = old_active - active
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="seconds")
    state = {
        "schema_version": SCHEMA_VERSION,
        "quality_skip_streak": streak,
        "active_reasons": sorted(active),
        "market_lag": lag,
        "updated_at": stamp,
        "last_run_id": run_id,
    }
    if active:
        state["opened_at"] = previous.get("opened_at") or stamp
    elif old_active:
        state["resolved_at"] = stamp
    return state, new, resolved


def describe(reasons: set[str], state: dict) -> str:
    labels = []
    if "quality-gate" in reasons:
        labels.append(f"品質閘門連續略過 {state['quality_skip_streak']} 次")
    for market, label in (("tw", "台股"), ("us", "美股")):
        if f"stale-{market}" in reasons:
            lag = state["market_lag"][market]
            labels.append(
                f"{label}線上收盤 {lag['deployed']}，應有 {lag['expected']}，"
                f"落後 {lag['sessions']} 個交易日")
    return "；".join(labels)


def write_outputs(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def write_summary(state: dict, new: set[str], resolved: set[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = [
        "## Deployment health",
        "",
        f"- Consecutive protected skips: `{state['quality_skip_streak']}`",
    ]
    for market, label in (("tw", "Taiwan"), ("us", "US")):
        lag = state["market_lag"][market]
        rows.append(
            f"- {label}: deployed `{lag['deployed']}`, expected `{lag['expected']}`, "
            f"lag `{lag['sessions']}` session(s)")
    rows.append(f"- Active reasons: `{','.join(state['active_reasons']) or 'none'}`")
    if new:
        rows.append(f"- Newly opened: `{','.join(sorted(new))}`")
    if resolved:
        rows.append(f"- Resolved: `{','.join(sorted(resolved))}`")
    with Path(path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(rows) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--html", required=True)
    parser.add_argument("--quality-ok", choices=("true", "false"), required=True)
    args = parser.parse_args()

    state_path = Path(args.state)
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    deployed = deployed_market_dates(Path(args.html).read_text(encoding="utf-8"))
    state, new, resolved = evaluate_health(
        previous, args.quality_ok == "true", deployed)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    alert_text = describe(new, state)
    active_text = describe(set(state["active_reasons"]), state)
    if new:
        print(f"::warning title=部署健康告警::{alert_text}")
    elif resolved:
        print(f"::notice title=部署健康恢復::已解除:{','.join(sorted(resolved))}")
    elif active_text:
        print(f"部署健康告警持續中:{active_text}")
    else:
        print("部署健康正常")
    write_outputs(os.getenv("GITHUB_OUTPUT"), {
        "alert": "true" if new else "false",
        "alert_text": alert_text.replace("\n", " "),
        "active": ",".join(state["active_reasons"]),
    })
    write_summary(state, new, resolved)


if __name__ == "__main__":
    main()
