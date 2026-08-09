"""個人持有 thesis 的設定載入與證偽條件判定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml


@dataclass
class ThesisCondition:
    id: str
    label: str
    status: str                 # green / yellow / red / gray
    current_value: str
    basis: str
    validation: str
    validation_note: str
    manual: bool = False
    last_updated: str = ""


@dataclass
class ThesisResult:
    stock_id: str
    holding_assumption: str
    conditions: list[ThesisCondition]
    as_of_period: str
    gross_margins: list[dict] = field(default_factory=list)
    position_rules: dict = field(default_factory=dict)
    check_frequency: str = ""
    disclaimer: str = ""
    gross_margin_floor_pct: float = 55.0

    @property
    def triggered(self) -> bool:
        return any(x.status == "red" for x in self.conditions)

    @property
    def status(self) -> str:
        if self.triggered:
            return "red"
        if any(x.status == "yellow" for x in self.conditions):
            return "yellow"
        if any(x.status == "gray" for x in self.conditions):
            return "gray"
        return "green"


def load_thesis(path: str | Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not cfg.get("stock_id") or not cfg.get("holding_assumption"):
        raise ValueError("thesis config 需要 stock_id/holding_assumption")
    if not __import__("re").fullmatch(r"\d{4}Q[1-4]", str(cfg.get("as_of_period") or "")):
        raise ValueError("thesis config 需要 YYYYQn as_of_period")
    if not isinstance(cfg.get("manual_max_age_days"), int) or cfg["manual_max_age_days"] <= 0:
        raise ValueError("manual_max_age_days 必須為正整數")
    conditions = cfg.get("conditions") or {}
    required = {"gross_margin_two_quarter_decline",
                "consensus_eps_two_quarter_downgrades",
                "gross_margin_floor", "advanced_process_competition"}
    if set(conditions) != required:
        raise ValueError(f"thesis conditions 必須精確包含:{sorted(required)}")
    manual = conditions["advanced_process_competition"]
    if manual.get("status") not in {"not_triggered", "watch", "triggered", "unknown"}:
        raise ValueError("advanced_process_competition.status 格式錯誤")
    try:
        updated = date.fromisoformat(str(manual["last_updated"]))
    except (KeyError, ValueError) as e:
        raise ValueError("人工競爭判定需要 YYYY-MM-DD last_updated") from e
    if updated > _taipei_today():
        raise ValueError("人工競爭判定 last_updated 不可晚於今天")
    return cfg


def _taipei_today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def _quarter_index(period: str) -> int:
    return int(period[:4]) * 4 + int(period[-1]) - 1


def _quarter_end(period: str) -> date:
    year, quarter = int(period[:4]), int(period[-1])
    return date(year, quarter * 3, 31 if quarter in (1, 4) else 30)


def _decline_streak(rows: list[dict], min_decline_pct: float = 0.0) -> int:
    streak = 0
    for newer, older in zip(reversed(rows), reversed(rows[:-1])):
        if _quarter_index(newer["period"]) != _quarter_index(older["period"]) + 1:
            break
        decline = ((float(newer["value"]) - float(older["value"]))
                   / abs(float(older["value"])) * 100) if older["value"] else 0.0
        if float(newer["value"]) < float(older["value"]) and decline <= -min_decline_pct:
            streak += 1
        else:
            break
    return streak


def _quarterly_consensus(history: list[dict], as_of: str) -> list[dict]:
    """每日共識壓成 target FY × 觀測季度快照，跨年銜接前一年的 eps_y1。"""
    parsed = []
    for row in history:
        try:
            dt = datetime.fromisoformat(str(row["datetime"]))
        except (KeyError, ValueError):
            continue
        if dt.date() > _taipei_today():
            continue
        period = f"{dt.year}Q{(dt.month - 1) // 3 + 1}"
        if _quarter_index(period) > _quarter_index(as_of):
            continue
        for field, target_fy in (("eps_y0", dt.year), ("eps_y1", dt.year + 1)):
            try:
                value = float(row[field])
            except (KeyError, TypeError, ValueError):
                continue
            parsed.append((dt, target_fy, value))
    by_quarter = {}
    for dt, target_fy, value in sorted(parsed):
        period = f"{dt.year}Q{(dt.month - 1) // 3 + 1}"
        by_quarter[(target_fy, period)] = {"period": period, "target_fy": target_fy,
                                           "date": dt.strftime("%Y-%m-%d"), "value": value}
    if not by_quarter:
        return []
    target_fy = int(as_of[:4])
    return [by_quarter[k] for k in sorted(by_quarter) if k[0] == target_fy]


def evaluate_thesis(cfg: dict, quarters: list, consensus_history: list[dict]) -> ThesisResult:
    cond_cfg = cfg["conditions"]
    as_of = str(cfg["as_of_period"])
    all_gm_rows = sorted(({"quarter": q.quarter, "period": q.quarter,
                           "value": round(float(q.gross_margin_pct), 2), "source": q.source}
                          for q in quarters if q.gross_margin_pct is not None),
                         key=lambda x: _quarter_index(x["quarter"]))
    future_quarters = [x["quarter"] for x in all_gm_rows
                       if _quarter_index(x["quarter"]) > _quarter_index(as_of)]
    if future_quarters:
        raise ValueError(f"thesis as_of_period {as_of} 落後最新財報季 {future_quarters[-1]}")
    gm_rows = [x for x in all_gm_rows
               if _quarter_index(x["quarter"]) <= _quarter_index(as_of)][-8:]
    gm_values = [x["value"] for x in gm_rows]
    latest_gm_is_as_of = bool(gm_rows) and gm_rows[-1]["quarter"] == as_of
    conditions = []

    c = cond_cfg["gross_margin_two_quarter_decline"]
    decline_rows = gm_rows[-3:]
    decline_ready = (len(decline_rows) == 3 and latest_gm_is_as_of
                     and all(_quarter_index(b["quarter"]) == _quarter_index(a["quarter"]) + 1
                             for a, b in zip(decline_rows, decline_rows[1:])))
    if not decline_ready:
        status, current = "gray", "資料不足"
        basis = f"需截至 {as_of} 的最近 3 個連續季度毛利率"
    else:
        streak = _decline_streak(decline_rows)
        status = ("red" if streak >= int(c["red_after_consecutive_declines"])
                  else "yellow" if streak >= int(c["yellow_after_consecutive_declines"])
                  else "green")
        current = " → ".join(f"{x['quarter']} {x['value']:.2f}%" for x in decline_rows)
        basis = f"最新連續季減 {streak} 季；紅燈門檻 {c['red_after_consecutive_declines']} 季"
    conditions.append(ThesisCondition("gross_margin_two_quarter_decline", c["label"], status,
                                      current, basis, c["validation"], c["validation_note"]))

    c = cond_cfg["consensus_eps_two_quarter_downgrades"]
    today = _taipei_today()
    consensus_as_of = f"{today.year}Q{(today.month - 1) // 3 + 1}"
    snapshots = _quarterly_consensus(consensus_history, consensus_as_of)
    values = [x["value"] for x in snapshots]
    required_snapshots = int(c["red_after_consecutive_downgrades"]) + 1
    recent_snapshots = snapshots[-required_snapshots:]
    consensus_ready = (len(recent_snapshots) == required_snapshots
                       and recent_snapshots[-1]["period"] == consensus_as_of
                       and all(_quarter_index(b["period"]) == _quarter_index(a["period"]) + 1
                               for a, b in zip(recent_snapshots, recent_snapshots[1:])))
    if not consensus_ready:
        latest = f"；目前 {values[-1]:.2f}" if values else ""
        status, current = "gray", f"季度快照不足（{len(recent_snapshots)}/{required_snapshots}）{latest}"
        basis = "需同一 target FY 的相鄰季度快照；資料不足不當作未觸發"
    else:
        streak = _decline_streak(recent_snapshots, float(c.get("min_revision_pct", 0)))
        status = ("red" if streak >= int(c["red_after_consecutive_downgrades"])
                  else "yellow" if streak >= int(c["yellow_after_consecutive_downgrades"])
                  else "green")
        current = " → ".join(f"{x['period']} {x['value']:.2f}" for x in recent_snapshots)
        basis = (f"FY{recent_snapshots[-1]['target_fy']} 共識連續下修 {streak} 季；"
                  f"每次至少 {float(c.get('min_revision_pct', 0)):.1f}%；紅燈門檻 "
                  f"{c['red_after_consecutive_downgrades']} 季")
    conditions.append(ThesisCondition("consensus_eps_two_quarter_downgrades", c["label"], status,
                                      current, basis, c["validation"], c["validation_note"]))

    c = cond_cfg["gross_margin_floor"]
    if not gm_values or not latest_gm_is_as_of:
        status, current, basis = "gray", "資料不足", f"無截至 {as_of} 的毛利率"
    else:
        latest = gm_values[-1]
        floor = float(c["red_below_pct"])
        status = "red" if latest < floor else "yellow" if latest < floor + float(c["yellow_buffer_pp"]) else "green"
        current = f"{gm_rows[-1]['quarter']} {latest:.2f}%"
        basis = f"紅燈 < {floor:g}%；黃燈為距門檻 {float(c['yellow_buffer_pp']):g} 個百分點內"
    conditions.append(ThesisCondition("gross_margin_floor", c["label"], status, current, basis,
                                      c["validation"], c["validation_note"]))

    c = cond_cfg["advanced_process_competition"]
    status_map = {"not_triggered": "green", "watch": "yellow", "triggered": "red", "unknown": "gray"}
    current_map = {"not_triggered": "人工標記：未觸發", "watch": "人工標記：接近／觀察中",
                   "triggered": "人工標記：已觸發", "unknown": "人工標記：待判定"}
    updated = date.fromisoformat(str(c["last_updated"]))
    stale = ((_taipei_today() - updated).days > int(cfg["manual_max_age_days"])
             or updated < _quarter_end(as_of))
    # 已確認觸發必須由使用者明確解除；不能只因更新日逾期就自動清掉紅燈。
    if c["status"] == "triggered":
        manual_status, manual_current = "red", current_map[c["status"]]
    else:
        manual_status = "gray" if stale else status_map[c["status"]]
        manual_current = "人工標記：已逾期，待更新" if stale else current_map[c["status"]]
    conditions.append(ThesisCondition(
        "advanced_process_competition", c["label"], manual_status, manual_current,
        str(c.get("note") or "—"), c["validation"], c["validation_note"], True,
        str(c["last_updated"])))

    return ThesisResult(
        stock_id=str(cfg["stock_id"]), holding_assumption=cfg["holding_assumption"],
        conditions=conditions, as_of_period=as_of, gross_margins=gm_rows,
        position_rules=cfg.get("position_rules") or {},
        check_frequency=str(cfg.get("check_frequency") or ""),
        disclaimer=str(cfg.get("disclaimer") or ""),
        gross_margin_floor_pct=float(cond_cfg["gross_margin_floor"]["red_below_pct"]),
    )
