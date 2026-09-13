"""Pure transformations and statistics for the CAPEX transmission study.

The module performs no I/O.  Quarterly aggregates use frozen membership and
fixed caller-supplied FX rates; inferential results use stationary bootstrap
paths rather than IID permutations.
"""
from __future__ import annotations

import math
import random
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

try:  # NumPy is optional, but makes the preregistered bootstrap practical.
    import numpy as _np
except Exception:  # pragma: no cover - exercised only in minimal environments.
    _np = None


DEFAULT_BOOTSTRAP_DRAWS = 99_999
DEFAULT_BOOTSTRAP_SEED = 20_260_807
DEFAULT_BLOCK_LENGTH = 8.0
MIN_DESCRIPTIVE_N = 12
MIN_INFERENTIAL_N = 32
FAIL_CONCLUSION = "傳導關係不穩定,不進入策略設計階段"

_QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")
_PATH_CHUNK_SIZE = 2_048
_USABLE_STATUSES = frozenset(
    {
        "available",
        "complete",
        "derived",
        "observed",
        "ok",
        "present",
        "reported",
        "usable",
        "valid",
    }
)


# ---------------------------------------------------------------------------
# Quarter helpers
# ---------------------------------------------------------------------------


def parse_quarter(quarter: str) -> tuple[int, int]:
    """Return ``(year, quarter_number)`` for a strict ``YYYYQn`` label."""
    if not isinstance(quarter, str):
        raise ValueError(f"Invalid calendar quarter: {quarter!r}")
    match = _QUARTER_RE.fullmatch(quarter)
    if not match or int(match.group(1)) < 1:
        raise ValueError(f"Invalid calendar quarter: {quarter!r}")
    return int(match.group(1)), int(match.group(2))


def quarter_index(quarter: str) -> int:
    """Map a quarter label to a monotonically increasing integer."""
    year, number = parse_quarter(quarter)
    return year * 4 + number - 1


def quarter_from_index(index: int) -> str:
    """Convert a positive integer quarter index back to ``YYYYQn``."""
    if isinstance(index, bool) or not isinstance(index, int) or index < 4:
        raise ValueError(f"Invalid quarter index: {index!r}")
    year, offset = divmod(index, 4)
    return f"{year:04d}Q{offset + 1}"


def shift_quarter(quarter: str, periods: int) -> str:
    """Shift a quarter label by an integer number of quarters."""
    if isinstance(periods, bool) or not isinstance(periods, int):
        raise TypeError("periods must be an integer")
    return quarter_from_index(quarter_index(quarter) + periods)


def quarter_range(start_quarter: str, end_quarter: str) -> list[str]:
    """Return all quarter labels in an inclusive period."""
    start = quarter_index(start_quarter)
    end = quarter_index(end_quarter)
    if start > end:
        raise ValueError("start_quarter must not follow end_quarter")
    return [quarter_from_index(index) for index in range(start, end + 1)]


def quarter_in_window(
    quarter: str, start_quarter: str, end_quarter: str
) -> bool:
    """Return whether a quarter lies in an inclusive analysis window."""
    index = quarter_index(quarter)
    return quarter_index(start_quarter) <= index <= quarter_index(end_quarter)


def filter_quarters(
    quarters: Sequence[str], start_quarter: str, end_quarter: str
) -> list[str]:
    """Filter quarter labels to an inclusive period, preserving input order."""
    return [
        quarter
        for quarter in quarters
        if quarter_in_window(quarter, start_quarter, end_quarter)
    ]


def filter_quarter_rows(
    rows: Sequence[Mapping[str, Any]],
    start_quarter: str,
    end_quarter: str,
    quarter_key: str = "calendar_quarter",
) -> list[dict[str, Any]]:
    """Copy rows whose quarter is inside an inclusive period."""
    result: list[dict[str, Any]] = []
    for supplied in rows:
        quarter = supplied.get(quarter_key)
        if isinstance(quarter, str) and quarter_in_window(
            quarter, start_quarter, end_quarter
        ):
            result.append(dict(supplied))
    return result


def _contiguous_quarter_slices(quarters: Sequence[str]) -> list[tuple[int, int]]:
    """Return half-open slices whose quarter labels have no calendar gaps."""
    if not quarters:
        return []
    slices: list[tuple[int, int]] = []
    start = 0
    previous = quarter_index(quarters[0])
    for position, quarter in enumerate(quarters[1:], start=1):
        current = quarter_index(quarter)
        if current != previous + 1:
            slices.append((start, position))
            start = position
        previous = current
    slices.append((start, len(quarters)))
    return slices


# ---------------------------------------------------------------------------
# Fixed-member level and YoY aggregates
# ---------------------------------------------------------------------------


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _member_id(member: Any) -> Any:
    if isinstance(member, Mapping):
        for key in ("id", "member", "ticker", "company"):
            if member.get(key) is not None:
                return member[key]
        raise ValueError(f"Frozen member has no identifier: {member!r}")
    return member


def _canonical_member(member: Any) -> str:
    if member is None:
        raise ValueError("Member identifiers may not be null")
    return str(member)


def _row_member(row: Mapping[str, Any], member_key: str) -> Any:
    for key in (member_key, "member_id", "id", "ticker", "company"):
        if row.get(key) is not None:
            return row[key]
    return None


def _normalise_member_rows(
    member_rows: Mapping[Any, Sequence[Mapping[str, Any]]]
    | Sequence[Mapping[str, Any]],
    member_key: str,
) -> list[tuple[Any, dict[str, Any]]]:
    normalised: list[tuple[Any, dict[str, Any]]] = []
    if isinstance(member_rows, Mapping):
        for member, rows in member_rows.items():
            if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
                raise TypeError("Each member's rows must be a sequence")
            for supplied in rows:
                if not isinstance(supplied, Mapping):
                    raise TypeError("Member rows must be mappings")
                normalised.append((member, dict(supplied)))
        return normalised

    if isinstance(member_rows, (str, bytes)) or not isinstance(
        member_rows, Sequence
    ):
        raise TypeError("member_rows must be a mapping or sequence")
    for supplied in member_rows:
        if not isinstance(supplied, Mapping):
            raise TypeError("Member rows must be mappings")
        member = _row_member(supplied, member_key)
        if member is None:
            raise ValueError(f"Member row has no {member_key!r}: {supplied!r}")
        normalised.append((member, dict(supplied)))
    return normalised


def _fixed_rate(
    direct: Any, fixed_rates: Mapping[str, Any] | None, names: Sequence[str]
) -> float | None:
    value = direct
    if value is None and fixed_rates:
        for name in names:
            if name in fixed_rates:
                value = fixed_rates[name]
                break
    return _finite_number(value)


def _missing_reason(row: Mapping[str, Any], fallback: str) -> str:
    for key in ("missing_reason", "reason", "status_reason", "error"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


def _convert_row_to_usd(
    row: Mapping[str, Any],
    *,
    value_key: str,
    currency_key: str,
    status_key: str,
    usable_statuses: frozenset[str],
    twd_per_usd: float | None,
    usd_per_eur: float | None,
    absolute_values: bool,
) -> tuple[float | None, str | None]:
    status = row.get(status_key)
    status_text = str(status).strip().lower() if status is not None else ""
    if status_text not in usable_statuses:
        fallback = "missing_status" if not status_text else f"status:{status}"
        return None, _missing_reason(row, fallback)

    value = _finite_number(row.get(value_key))
    if value is None:
        return None, _missing_reason(row, "missing_or_non_finite_value")
    currency = str(row.get(currency_key) or "").strip().upper()
    if currency == "USD":
        usd = value
    elif currency == "TWD":
        if twd_per_usd is None or twd_per_usd <= 0:
            return None, _missing_reason(row, "invalid_twd_per_usd_rate")
        usd = value / twd_per_usd
    elif currency == "EUR":
        if usd_per_eur is None or usd_per_eur <= 0:
            return None, _missing_reason(row, "invalid_usd_per_eur_rate")
        usd = value * usd_per_eur
    else:
        return None, _missing_reason(row, f"unsupported_currency:{currency or 'missing'}")
    if absolute_values:
        usd = abs(usd)
    return usd, None


def yoy_from_complete_levels(
    levels: Sequence[Mapping[str, Any]],
    frozen_members: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """Compute t versus t-4 YoY only when both aggregate levels are complete."""
    by_quarter = {
        str(row["calendar_quarter"]): row
        for row in levels
        if isinstance(row.get("calendar_quarter"), str)
    }
    members = list(frozen_members or [])
    result: list[dict[str, Any]] = []
    for quarter in sorted(by_quarter, key=quarter_index):
        current = by_quarter[quarter]
        base_quarter = shift_quarter(quarter, -4)
        base = by_quarter.get(base_quarter)
        current_reasons = dict(current.get("member_reasons") or {})
        base_reasons = dict((base or {}).get("member_reasons") or {})
        status = "complete"
        yoy: float | None = None
        if not current.get("complete"):
            status = "current_incomplete"
        elif base is None:
            status = "base_quarter_outside_panel"
            base_reasons = {
                _canonical_member(member): "base_quarter_outside_panel"
                for member in members
            }
        elif not base.get("complete"):
            status = "base_incomplete"
        else:
            current_value = _finite_number(current.get("value_usd"))
            base_value = _finite_number(base.get("value_usd"))
            if base_value == 0:
                status = "zero_base"
            elif current_value is None or base_value is None:
                status = "invalid_level"
            else:
                yoy = (current_value / base_value - 1.0) * 100.0
        result.append(
            {
                "calendar_quarter": quarter,
                "quarter": quarter,
                "base_quarter": base_quarter,
                "yoy": yoy,
                "value": yoy,
                "usable": yoy is not None,
                "complete": yoy is not None,
                "status": status,
                "missing_member_reasons": {
                    "current": current_reasons,
                    "base": base_reasons,
                },
            }
        )
    return result


def build_fixed_member_panel(
    member_rows: Mapping[Any, Sequence[Mapping[str, Any]]]
    | Sequence[Mapping[str, Any]],
    frozen_members: Sequence[Any],
    twd_per_usd: float | None = None,
    usd_per_eur: float | None = None,
    *,
    fixed_rates: Mapping[str, Any] | None = None,
    start_quarter: str | None = None,
    end_quarter: str | None = None,
    member_key: str = "member",
    value_key: str = "value",
    currency_key: str = "currency",
    status_key: str = "status",
    usable_statuses: Sequence[str] | None = None,
    absolute_values: bool = True,
) -> dict[str, Any]:
    """Build a complete fixed-member panel, aggregate USD levels, and YoY.

    Duplicate member-quarter rows are unusable rather than silently selected.
    TWD is divided by TWD/USD and EUR is multiplied by USD/EUR.  No values are
    interpolated and frozen membership never changes.
    """
    if isinstance(frozen_members, (str, bytes)) or not frozen_members:
        raise ValueError("frozen_members must be a non-empty sequence")
    canonical = [
        _canonical_member(_member_id(member)) for member in frozen_members
    ]
    if len(set(canonical)) != len(canonical):
        raise ValueError("frozen_members contains duplicate identifiers")
    member_lookup = {member: member for member in canonical}

    rows = _normalise_member_rows(member_rows, member_key)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    observed_quarters: list[str] = []
    ignored_members: list[Any] = []
    ignored_seen: set[str] = set()
    for member, row in rows:
        member_id = _canonical_member(member)
        if member_id not in member_lookup:
            if member_id not in ignored_seen:
                ignored_members.append(member_id)
                ignored_seen.add(member_id)
            continue
        quarter = row.get("calendar_quarter")
        if not isinstance(quarter, str):
            continue
        parse_quarter(quarter)
        grouped[(member_id, quarter)].append(row)
        observed_quarters.append(quarter)

    if start_quarter is None:
        if not observed_quarters:
            raise ValueError("start_quarter is required when no valid rows exist")
        start_quarter = min(observed_quarters, key=quarter_index)
    if end_quarter is None:
        if not observed_quarters:
            raise ValueError("end_quarter is required when no valid rows exist")
        end_quarter = max(observed_quarters, key=quarter_index)
    quarters = quarter_range(start_quarter, end_quarter)

    twd_rate = _fixed_rate(
        twd_per_usd,
        fixed_rates,
        ("twd_per_usd", "TWD_per_USD", "TWD_PER_USD"),
    )
    eur_rate = _fixed_rate(
        usd_per_eur,
        fixed_rates,
        ("usd_per_eur", "USD_per_EUR", "USD_PER_EUR"),
    )
    statuses = frozenset(
        str(status).strip().lower()
        for status in (usable_statuses or _USABLE_STATUSES)
    )
    if not statuses:
        raise ValueError("usable_statuses may not be empty")

    panel: list[dict[str, Any]] = []
    levels: list[dict[str, Any]] = []
    for quarter in quarters:
        quarter_rows: list[dict[str, Any]] = []
        missing_members: list[dict[str, Any]] = []
        values: list[float] = []
        for member_id in canonical:
            candidates = grouped.get((member_id, quarter), [])
            source = candidates[0] if len(candidates) == 1 else None
            reason: str | None
            usd_value: float | None
            if not candidates:
                usd_value, reason = None, "missing_row"
            elif len(candidates) > 1:
                usd_value, reason = None, "duplicate_member_quarter_rows"
            else:
                usd_value, reason = _convert_row_to_usd(
                    source or {},
                    value_key=value_key,
                    currency_key=currency_key,
                    status_key=status_key,
                    usable_statuses=statuses,
                    twd_per_usd=twd_rate,
                    usd_per_eur=eur_rate,
                    absolute_values=absolute_values,
                )
            output_member = member_lookup[member_id]
            source_currency = (source or {}).get(currency_key)
            source_status = (source or {}).get(status_key)
            panel_row = {
                "calendar_quarter": quarter,
                "quarter": quarter,
                "member": output_member,
                "source_value": _finite_number((source or {}).get(value_key)),
                "currency": str(source_currency) if source_currency is not None else None,
                "source_status": str(source_status) if source_status is not None else None,
                "value_usd": usd_value,
                "usd_value": usd_value,
                "usable": usd_value is not None,
                "reason": reason,
            }
            quarter_rows.append(panel_row)
            panel.append(panel_row)
            if usd_value is None:
                missing_members.append(
                    {
                        "member": output_member,
                        "reason": reason,
                        "status": str(source_status) if source_status is not None else None,
                    }
                )
            else:
                values.append(usd_value)
        complete = len(values) == len(canonical)
        member_reasons = {
            _canonical_member(item["member"]): item["reason"]
            for item in missing_members
        }
        levels.append(
            {
                "calendar_quarter": quarter,
                "quarter": quarter,
                "value_usd": sum(values) if complete else None,
                "value": sum(values) if complete else None,
                "complete": complete,
                "status": "complete" if complete else "incomplete",
                "member_count": len(canonical),
                "usable_member_count": len(values),
                "member_values_usd": {
                    _canonical_member(row["member"]): row["value_usd"]
                    for row in quarter_rows
                },
                "missing_members": missing_members,
                "member_reasons": member_reasons,
            }
        )

    yoy = yoy_from_complete_levels(levels, canonical)
    return {
        "frozen_members": canonical,
        "start_quarter": start_quarter,
        "end_quarter": end_quarter,
        "fixed_rates": {
            "twd_per_usd": twd_rate,
            "usd_per_eur": eur_rate,
            "rate_period": "2014Q1",
        },
        "panel": panel,
        "aggregate": levels,
        "levels": levels,
        "yoy": yoy,
        "complete_quarters": [
            row["calendar_quarter"] for row in levels if row["complete"]
        ],
        "ignored_nonmembers": ignored_members,
        "composition_changed": False,
        "interpolated": False,
    }


def build_fixed_member_revenue_aggregate(
    member_rows: Mapping[Any, Sequence[Mapping[str, Any]]]
    | Sequence[Mapping[str, Any]],
    frozen_members: Sequence[Any],
    twd_per_usd: float | None = None,
    usd_per_eur: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build the preregistered fixed-member absolute-USD revenue aggregate."""
    result = build_fixed_member_panel(
        member_rows,
        frozen_members,
        twd_per_usd,
        usd_per_eur,
        absolute_values=True,
        **kwargs,
    )
    result["metric"] = "revenue"
    return result


def build_four_company_capex_aggregate(
    company_rows: Mapping[Any, Sequence[Mapping[str, Any]]]
    | Sequence[Mapping[str, Any]],
    frozen_companies: Sequence[Any],
    twd_per_usd: float | None = None,
    usd_per_eur: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Aggregate absolute CAPEX, requiring the same four companies at t and t-4."""
    if len(frozen_companies) != 4:
        raise ValueError("The CAPEX aggregate requires exactly four frozen companies")
    result = build_fixed_member_panel(
        company_rows,
        frozen_companies,
        twd_per_usd,
        usd_per_eur,
        absolute_values=True,
        **kwargs,
    )
    result["metric"] = "capex"
    return result


# Descriptive aliases retained for discoverability.
build_fixed_member_quarterly_panel = build_fixed_member_panel
build_layer_revenue_aggregate = build_fixed_member_revenue_aggregate
build_capex_aggregate = build_four_company_capex_aggregate


# ---------------------------------------------------------------------------
# Correlation and detrending
# ---------------------------------------------------------------------------


def pearson_r(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Return Pearson's r, or ``None`` for mismatched/degenerate data."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    x_values = [_finite_number(value) for value in xs]
    y_values = [_finite_number(value) for value in ys]
    if any(value is None for value in x_values + y_values):
        return None
    x = [float(value) for value in x_values if value is not None]
    y = [float(value) for value in y_values if value is not None]
    scale_x = max(abs(value) for value in x)
    scale_y = max(abs(value) for value in y)
    if scale_x == 0 or scale_y == 0:
        return None
    scaled_x = [value / scale_x for value in x]
    scaled_y = [value / scale_y for value in y]
    mean_x = math.fsum(scaled_x) / len(scaled_x)
    mean_y = math.fsum(scaled_y) / len(scaled_y)
    centered_x = [value - mean_x for value in scaled_x]
    centered_y = [value - mean_y for value in scaled_y]
    sum_xx = math.fsum(value * value for value in centered_x)
    sum_yy = math.fsum(value * value for value in centered_y)
    denominator = math.sqrt(sum_xx * sum_yy)
    if denominator <= 0:
        return None
    correlation = math.fsum(
        left * right for left, right in zip(centered_x, centered_y)
    ) / denominator
    return max(-1.0, min(1.0, correlation))


def pearson_t_pvalue(r: float | None, n: int) -> tuple[float | None, str]:
    """Return the ordinary two-sided Pearson t p-value when SciPy is present."""
    if r is None or n < MIN_DESCRIPTIVE_N:
        return None, "not_computed"
    try:
        from scipy.stats import t as student_t
    except Exception:  # pragma: no cover - depends on the runtime environment.
        return None, "scipy_unavailable"
    absolute = abs(float(r))
    if absolute >= 1.0:
        return 0.0, "ok"
    statistic = absolute * math.sqrt((n - 2) / max(1.0 - absolute * absolute, 1e-300))
    return float(2.0 * student_t.sf(statistic, n - 2)), "ok"


def _series_rows(series: Any) -> list[Mapping[str, Any]]:
    if isinstance(series, Mapping):
        for key in ("yoy", "series", "rows"):
            candidate = series.get(key)
            if isinstance(candidate, Sequence) and not isinstance(
                candidate, (str, bytes)
            ):
                return list(candidate)
        if all(isinstance(key, str) for key in series):
            return [
                {"calendar_quarter": quarter, "yoy": value}
                for quarter, value in series.items()
            ]
    if isinstance(series, Sequence) and not isinstance(series, (str, bytes)):
        return list(series)
    raise TypeError("A quarterly series must be a row sequence or quarter mapping")


def _finite_series_map(series: Any, value_key: str = "yoy") -> dict[str, float]:
    candidates: dict[str, list[float]] = defaultdict(list)
    for row in _series_rows(series):
        if not isinstance(row, Mapping):
            continue
        quarter = row.get("calendar_quarter", row.get("quarter"))
        if not isinstance(quarter, str):
            continue
        parse_quarter(quarter)
        value = _finite_number(row.get(value_key))
        if value is None and value_key != "value":
            value = _finite_number(row.get("value"))
        if value is not None:
            candidates[quarter].append(value)
    # Ambiguous duplicates are excluded rather than selected silently.
    return {
        quarter: values[0]
        for quarter, values in candidates.items()
        if len(values) == 1
    }


def autocorrelation(values: Sequence[float], lag: int) -> float | None:
    """Return the usual sample autocorrelation at a positive lag."""
    if isinstance(lag, bool) or not isinstance(lag, int) or lag < 1:
        raise ValueError("lag must be a positive integer")
    numbers = [_finite_number(value) for value in values]
    if any(value is None for value in numbers) or len(numbers) <= lag:
        return None
    clean = [float(value) for value in numbers if value is not None]
    scale = max(abs(value) for value in clean)
    if scale == 0:
        return None
    scaled = [value / scale for value in clean]
    mean = math.fsum(scaled) / len(scaled)
    denominator = math.fsum((value - mean) ** 2 for value in scaled)
    if denominator <= 0:
        return None
    numerator = math.fsum(
        (scaled[index] - mean) * (scaled[index - lag] - mean)
        for index in range(lag, len(scaled))
    )
    return numerator / denominator


def autocorrelation_effective_sample_size(
    xs: Sequence[float], ys: Sequence[float] | None = None, max_lag: int = 8
) -> dict[str, Any]:
    """Return the lag-8 autocorrelation ESS diagnostic, clipped to ``[3, n]``.

    For a correlation, the Bartlett/Pyper-Peterman product of X and Y
    autocorrelations is used.  For one series, its own autocorrelations are
    summed.
    """
    if isinstance(max_lag, bool) or not isinstance(max_lag, int) or max_lag < 0:
        raise ValueError("max_lag must be a non-negative integer")
    if ys is not None and len(xs) != len(ys):
        raise ValueError("xs and ys must have equal lengths")
    x_values = [_finite_number(value) for value in xs]
    y_values = [_finite_number(value) for value in ys] if ys is not None else None
    if y_values is None:
        paired = [float(value) for value in x_values if value is not None]
        x_clean = paired
        y_clean = None
    else:
        pairs = [
            (float(left), float(right))
            for left, right in zip(x_values, y_values)
            if left is not None and right is not None
        ]
        x_clean = [pair[0] for pair in pairs]
        y_clean = [pair[1] for pair in pairs]
    n = len(x_clean)
    if n < 3:
        return {
            "n": n,
            "effective_n": None,
            "raw_effective_n": None,
            "max_lag": min(max_lag, max(0, n - 1)),
            "x_autocorrelations": [],
            "y_autocorrelations": [] if ys is not None else None,
            "status": "insufficient_n",
        }
    scale_x = max(abs(value) for value in x_clean)
    scaled_x = [value / scale_x for value in x_clean] if scale_x else []
    mean_x = math.fsum(scaled_x) / n if scaled_x else 0.0
    zero_x_variance = not scaled_x or math.fsum(
        (value - mean_x) ** 2 for value in scaled_x
    ) <= 0
    zero_y_variance = False
    if y_clean is not None:
        scale_y = max(abs(value) for value in y_clean)
        scaled_y = [value / scale_y for value in y_clean] if scale_y else []
        mean_y = math.fsum(scaled_y) / n if scaled_y else 0.0
        zero_y_variance = not scaled_y or math.fsum(
            (value - mean_y) ** 2 for value in scaled_y
        ) <= 0
    if zero_x_variance or zero_y_variance:
        return {
            "n": n,
            "effective_n": None,
            "raw_effective_n": None,
            "max_lag": min(max_lag, n - 1),
            "x_autocorrelations": [],
            "y_autocorrelations": [] if ys is not None else None,
            "status": "zero_variance",
        }

    x_acf: list[float | None] = []
    y_acf: list[float | None] | None = [] if y_clean is not None else None
    terms: list[float] = []
    through = min(max_lag, n - 1)
    for lag in range(1, through + 1):
        rho_x = autocorrelation(x_clean, lag)
        x_acf.append(rho_x)
        if y_clean is None:
            if rho_x is not None:
                terms.append(rho_x)
        else:
            rho_y = autocorrelation(y_clean, lag)
            assert y_acf is not None
            y_acf.append(rho_y)
            if rho_x is not None and rho_y is not None:
                terms.append(rho_x * rho_y)
    denominator = 1.0 + 2.0 * sum(terms)
    raw = n / denominator if denominator > 0 else float(n)
    effective = max(3.0, min(float(n), raw))
    return {
        "n": n,
        "effective_n": effective,
        "raw_effective_n": raw,
        "max_lag": through,
        "x_autocorrelations": x_acf,
        "y_autocorrelations": y_acf,
        "status": "ok",
    }


def effective_sample_size(
    xs: Sequence[float], ys: Sequence[float] | None = None, max_lag: int = 8
) -> float | None:
    """Return only the clipped effective sample size diagnostic value."""
    value = autocorrelation_effective_sample_size(xs, ys, max_lag)["effective_n"]
    return float(value) if value is not None else None


def lagged_correlation(
    capex_yoy: Any,
    revenue_yoy: Any,
    lag: int,
    start_quarter: str,
    end_quarter: str,
    *,
    capex_value_key: str = "yoy",
    revenue_value_key: str = "yoy",
) -> dict[str, Any]:
    """Pair CAPEX YoY(t) with revenue YoY(t+lag) inside one fixed window."""
    if isinstance(lag, bool) or not isinstance(lag, int) or not 0 <= lag <= 8:
        raise ValueError("lag must be an integer from 0 through 8")
    quarter_range(start_quarter, end_quarter)  # Validate the window.
    capex = _finite_series_map(capex_yoy, capex_value_key)
    revenue = _finite_series_map(revenue_yoy, revenue_value_key)
    pairs: list[dict[str, Any]] = []
    for capex_quarter in sorted(capex, key=quarter_index):
        revenue_quarter = shift_quarter(capex_quarter, lag)
        if not quarter_in_window(capex_quarter, start_quarter, end_quarter):
            continue
        if not quarter_in_window(revenue_quarter, start_quarter, end_quarter):
            continue
        if revenue_quarter not in revenue:
            continue
        pairs.append(
            {
                "capex_quarter": capex_quarter,
                "revenue_quarter": revenue_quarter,
                "lag": lag,
                "capex_yoy": capex[capex_quarter],
                "revenue_yoy": revenue[revenue_quarter],
                "x": capex[capex_quarter],
                "y": revenue[revenue_quarter],
            }
        )
    xs = [row["capex_yoy"] for row in pairs]
    ys = [row["revenue_yoy"] for row in pairs]
    n = len(pairs)
    segment_slices = _contiguous_quarter_slices(
        [row["capex_quarter"] for row in pairs]
    )
    segment_lengths = [stop - start for start, stop in segment_slices]
    segment_boundaries = [
        {
            "start_capex_quarter": pairs[start]["capex_quarter"],
            "end_capex_quarter": pairs[stop - 1]["capex_quarter"],
            "start_revenue_quarter": pairs[start]["revenue_quarter"],
            "end_revenue_quarter": pairs[stop - 1]["revenue_quarter"],
        }
        for start, stop in segment_slices
    ]
    correlation = pearson_r(xs, ys)
    ordinary_p, p_status = pearson_t_pvalue(correlation, n)
    if correlation is None and n >= 2:
        status = "zero_variance"
    elif n < MIN_DESCRIPTIVE_N:
        status = "insufficient_n"
    elif n < MIN_INFERENTIAL_N:
        status = "descriptive_only"
    else:
        status = "inferential_eligible"
    ess = autocorrelation_effective_sample_size(xs, ys, max_lag=8)
    return {
        "lag": lag,
        "analysis_window": {
            "start_quarter": start_quarter,
            "end_quarter": end_quarter,
        },
        "pairs": pairs,
        "paired_rows": pairs,
        "n": n,
        "segment_lengths": segment_lengths,
        "segment_count": len(segment_lengths),
        "segment_boundaries": segment_boundaries,
        "r": correlation,
        "p": ordinary_p,
        "pearson_p": ordinary_p,
        "pearson_p_status": p_status,
        "status": status,
        "inferential_eligible": n >= MIN_INFERENTIAL_N and correlation is not None,
        "positive": correlation is not None and correlation > 0,
        "effective_sample_size": ess,
    }


def _detrend_result(
    series: Any,
    *,
    value_key: str,
    start_quarter: str | None,
    end_quarter: str | None,
    break_quarter: str,
) -> dict[str, Any]:
    values = _finite_series_map(series, value_key)
    if start_quarter is not None or end_quarter is not None:
        if start_quarter is None or end_quarter is None:
            raise ValueError("Both start_quarter and end_quarter are required")
        values = {
            quarter: value
            for quarter, value in values.items()
            if quarter_in_window(quarter, start_quarter, end_quarter)
        }
    ordered = sorted(values, key=quarter_index)
    n = len(ordered)
    if not ordered:
        return {
            "series": [],
            "n": 0,
            "rank": 0,
            "terms": [],
            "break_quarter": break_quarter,
            "status": "no_data",
        }

    origin = quarter_index(ordered[0])
    break_index = quarter_index(break_quarter)
    columns = [
        ("intercept", [1.0] * n),
        ("linear_quarter_index", [float(quarter_index(q) - origin) for q in ordered]),
        ("post_level", [1.0 if quarter_index(q) >= break_index else 0.0 for q in ordered]),
        (
            "post_slope",
            [
                float(max(0, quarter_index(q) - break_index))
                if quarter_index(q) >= break_index
                else 0.0
                for q in ordered
            ],
        ),
    ]
    # Modified Gram-Schmidt obtains the fitted projection without requiring a
    # matrix package and remains well-defined when a window is wholly pre/post.
    basis: list[list[float]] = []
    retained_terms: list[str] = []
    tolerance = max(1.0, float(n)) * 1e-12
    for name, column in columns:
        vector = list(column)
        for unit in basis:
            projection = sum(a * b for a, b in zip(vector, unit))
            vector = [a - projection * b for a, b in zip(vector, unit)]
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= tolerance:
            continue
        basis.append([value / norm for value in vector])
        retained_terms.append(name)
    response = [values[quarter] for quarter in ordered]
    fitted = [0.0] * n
    for unit in basis:
        coefficient = sum(value * direction for value, direction in zip(response, unit))
        fitted = [
            old + coefficient * direction
            for old, direction in zip(fitted, unit)
        ]
    response_scale = max(abs(value) for value in response)
    residual_tolerance = math.ulp(response_scale) * max(16.0, float(n))
    residuals = [
        0.0 if abs(value - fit) <= residual_tolerance else value - fit
        for value, fit in zip(response, fitted)
    ]
    output = [
        {
            "calendar_quarter": quarter,
            "quarter": quarter,
            "original_yoy": response[index],
            "fitted_yoy": fitted[index],
            "detrended_yoy": residuals[index],
            "yoy": residuals[index],
            "value": residuals[index],
            "status": "detrended",
        }
        for index, quarter in enumerate(ordered)
    ]
    return {
        "series": output,
        "n": n,
        "rank": len(basis),
        "terms": retained_terms,
        "break_quarter": break_quarter,
        "status": "ok" if n > len(basis) else "saturated",
    }


def detrend_yoy_with_diagnostics(
    series: Any,
    *,
    value_key: str = "yoy",
    start_quarter: str | None = None,
    end_quarter: str | None = None,
    break_quarter: str = "2020Q1",
) -> dict[str, Any]:
    """OLS-residualize YoY on time and post-2020Q1 level/slope terms."""
    return _detrend_result(
        series,
        value_key=value_key,
        start_quarter=start_quarter,
        end_quarter=end_quarter,
        break_quarter=break_quarter,
    )


def detrend_yoy_series(
    series: Any,
    *,
    value_key: str = "yoy",
    start_quarter: str | None = None,
    end_quarter: str | None = None,
    break_quarter: str = "2020Q1",
) -> list[dict[str, Any]]:
    """Return only the residualized quarterly YoY rows."""
    return detrend_yoy_with_diagnostics(
        series,
        value_key=value_key,
        start_quarter=start_quarter,
        end_quarter=end_quarter,
        break_quarter=break_quarter,
    )["series"]


# ---------------------------------------------------------------------------
# Stationary bootstrap
# ---------------------------------------------------------------------------


def _numpy_rng(seed: int, stream: int) -> Any:
    assert _np is not None
    return _np.random.default_rng(_np.random.SeedSequence([int(seed), stream]))


def _python_rng(seed: int, stream: int) -> random.Random:
    return random.Random((int(seed) << 16) ^ (stream * 1_000_003))


def _normalise_segment_lengths(
    n: int, segment_lengths: Sequence[int] | None
) -> tuple[int, ...]:
    if segment_lengths is None:
        return (n,) if n else ()
    if isinstance(segment_lengths, (str, bytes)) or not isinstance(
        segment_lengths, Sequence
    ):
        raise TypeError("segment_lengths must be a sequence of positive integers")
    normalised: list[int] = []
    for length in segment_lengths:
        if isinstance(length, bool) or not isinstance(length, int) or length < 1:
            raise ValueError("segment_lengths must contain positive integers")
        normalised.append(length)
    if sum(normalised) != n:
        raise ValueError(f"segment_lengths sum to {sum(normalised)}, expected {n}")
    return tuple(normalised)


def _numpy_path_chunk(rng: Any, draws: int, n: int, restart: float) -> Any:
    indices = _np.empty((draws, n), dtype=_np.int32)
    indices[:, 0] = rng.integers(0, n, size=draws)
    for column in range(1, n):
        restart_here = rng.random(draws) < restart
        fresh = rng.integers(0, n, size=draws)
        indices[:, column] = _np.where(
            restart_here, fresh, (indices[:, column - 1] + 1) % n
        )
    return indices


def _python_path_chunk(
    rng: random.Random, draws: int, n: int, restart: float
) -> list[list[int]]:
    paths: list[list[int]] = []
    for _ in range(draws):
        current = rng.randrange(n)
        path = [current]
        for _position in range(1, n):
            if rng.random() < restart:
                current = rng.randrange(n)
            else:
                current = (current + 1) % n
            path.append(current)
        paths.append(path)
    return paths


def _numpy_segmented_path_chunk(
    rng: Any, draws: int, segment_lengths: Sequence[int], restart: float
) -> Any:
    chunks = []
    offset = 0
    for length in segment_lengths:
        chunks.append(_numpy_path_chunk(rng, draws, length, restart) + offset)
        offset += length
    return _np.concatenate(chunks, axis=1)


def _python_segmented_path_chunk(
    rng: random.Random,
    draws: int,
    segment_lengths: Sequence[int],
    restart: float,
) -> list[list[int]]:
    paths = [[] for _ in range(draws)]
    offset = 0
    for length in segment_lengths:
        segment_paths = _python_path_chunk(rng, draws, length, restart)
        for path, segment_path in zip(paths, segment_paths):
            path.extend(position + offset for position in segment_path)
        offset += length
    return paths


def _generated_path_chunks(
    n: int,
    draws: int,
    expected_block_length: float,
    seed: int,
    segment_lengths: Sequence[int] | None = None,
):
    segments = _normalise_segment_lengths(n, segment_lengths)
    restart = 1.0 / expected_block_length
    if _np is not None:
        rng_x = _numpy_rng(seed, 101)
        rng_y = _numpy_rng(seed, 202)
        rng_paired = _numpy_rng(seed, 303)
        for offset in range(0, draws, _PATH_CHUNK_SIZE):
            count = min(_PATH_CHUNK_SIZE, draws - offset)
            yield (
                _numpy_segmented_path_chunk(rng_x, count, segments, restart),
                _numpy_segmented_path_chunk(rng_y, count, segments, restart),
                _numpy_segmented_path_chunk(rng_paired, count, segments, restart),
            )
    else:  # pragma: no cover - NumPy is present in normal study runs.
        rng_x = _python_rng(seed, 101)
        rng_y = _python_rng(seed, 202)
        rng_paired = _python_rng(seed, 303)
        for offset in range(0, draws, _PATH_CHUNK_SIZE):
            count = min(_PATH_CHUNK_SIZE, draws - offset)
            yield (
                _python_segmented_path_chunk(rng_x, count, segments, restart),
                _python_segmented_path_chunk(rng_y, count, segments, restart),
                _python_segmented_path_chunk(rng_paired, count, segments, restart),
            )


def stationary_bootstrap_paths(
    n: int,
    draws: int = DEFAULT_BOOTSTRAP_DRAWS,
    expected_block_length: float = DEFAULT_BLOCK_LENGTH,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    segment_lengths: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Precompute JSON-safe independent-X/Y and paired stationary paths.

    The result can be supplied to :func:`stationary_bootstrap_test` or under
    its string/integer ``n`` key to :func:`full_lag_grid`.
    """
    if isinstance(n, bool) or not isinstance(n, int) or n < 2:
        raise ValueError("n must be an integer of at least 2")
    if isinstance(draws, bool) or not isinstance(draws, int) or draws < 0:
        raise ValueError("draws must be a non-negative integer")
    block = _finite_number(expected_block_length)
    if block is None or block < 1.0:
        raise ValueError("expected_block_length must be at least 1")
    segments = _normalise_segment_lengths(n, segment_lengths)
    x_paths: list[list[int]] = []
    y_paths: list[list[int]] = []
    paired_paths: list[list[int]] = []
    for x_chunk, y_chunk, paired_chunk in _generated_path_chunks(
        n, draws, block, seed, segments
    ):
        if _np is not None:
            x_paths.extend(x_chunk.tolist())
            y_paths.extend(y_chunk.tolist())
            paired_paths.extend(paired_chunk.tolist())
        else:  # pragma: no cover
            x_paths.extend(x_chunk)
            y_paths.extend(y_chunk)
            paired_paths.extend(paired_chunk)
    return {
        "n": n,
        "segment_lengths": list(segments),
        "segment_count": len(segments),
        "draws": draws,
        "expected_block_length": block,
        "seed": int(seed),
        "x_paths": x_paths,
        "y_paths": y_paths,
        "paired_paths": paired_paths,
        "independent_null_paths": True,
        "circular": True,
    }


def _path_shape_key(n: int, segment_lengths: Sequence[int]) -> str:
    if tuple(segment_lengths) == (n,):
        return str(n)
    return f"{n}|{','.join(str(length) for length in segment_lengths)}"


def precompute_stationary_bootstrap_paths(
    lengths: Sequence[int | Sequence[int]],
    draws: int = DEFAULT_BOOTSTRAP_DRAWS,
    expected_block_length: float = DEFAULT_BLOCK_LENGTH,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    segment_lengths: Mapping[Any, Sequence[int]] | Sequence[int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Precompute paths keyed by length, or by length and segment shape."""
    requested = list(lengths)
    shared_segments: Sequence[int] | None = None
    segments_by_n: Mapping[Any, Sequence[int]] | None = None
    if segment_lengths is not None:
        if isinstance(segment_lengths, Mapping):
            segments_by_n = segment_lengths
        else:
            if len(requested) != 1 or not isinstance(requested[0], int):
                raise ValueError(
                    "A single segment_lengths sequence requires one sample length"
                )
            shared_segments = segment_lengths

    shapes: set[tuple[int, tuple[int, ...]]] = set()
    for supplied in requested:
        if isinstance(supplied, bool):
            raise ValueError("Sample lengths must be integers of at least 2")
        if isinstance(supplied, int):
            n = supplied
            selected = shared_segments
            if segments_by_n is not None:
                selected = segments_by_n.get(n, segments_by_n.get(str(n)))
            segments = _normalise_segment_lengths(n, selected)
        elif isinstance(supplied, Sequence) and not isinstance(
            supplied, (str, bytes)
        ):
            if segment_lengths is not None:
                raise ValueError(
                    "Do not combine explicit segment shapes with segment_lengths"
                )
            raw_segments = tuple(supplied)
            if any(
                isinstance(length, bool)
                or not isinstance(length, int)
                or length < 1
                for length in raw_segments
            ):
                raise ValueError("Segment shapes must contain positive integers")
            n = sum(raw_segments)
            segments = _normalise_segment_lengths(n, raw_segments)
        else:
            raise TypeError("Each requested length must be an integer or segment shape")
        if n < 2:
            raise ValueError("Sample lengths must be integers of at least 2")
        shapes.add((n, segments))

    return {
        _path_shape_key(n, segments): stationary_bootstrap_paths(
            n,
            draws=draws,
            expected_block_length=expected_block_length,
            seed=seed,
            segment_lengths=segments,
        )
        for n, segments in sorted(shapes)
    }


def _paths_from_precomputed(
    paths: Mapping[str, Any],
    n: int,
    draws: int,
    segment_lengths: Sequence[int] | None = None,
) -> tuple[Any, Any, Any]:
    expected_segments = _normalise_segment_lengths(n, segment_lengths)
    if paths.get("n") != n:
        raise ValueError(f"Precomputed paths have n={paths.get('n')!r}, expected {n}")
    stored_segments = _normalise_segment_lengths(
        n, paths.get("segment_lengths")
    )
    if stored_segments != expected_segments:
        raise ValueError(
            "Precomputed paths have segment_lengths="
            f"{list(stored_segments)!r}, expected {list(expected_segments)!r}"
        )
    aliases = (
        ("x_paths", "x"),
        ("y_paths", "y"),
        ("paired_paths", "paired"),
    )
    selected: list[Any] = []
    for names in aliases:
        value = next((paths.get(name) for name in names if paths.get(name) is not None), None)
        if value is None or len(value) < draws:
            raise ValueError("Precomputed paths do not contain the requested draws")
        subset = value[:draws]
        if any(len(path) != n for path in subset):
            raise ValueError("A precomputed path has the wrong sample length")
        for path in subset:
            start = 0
            for length in expected_segments:
                stop = start + length
                for supplied_position in path[start:stop]:
                    try:
                        position = int(supplied_position)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError(
                            "A precomputed path contains an invalid index"
                        ) from exc
                    if (
                        isinstance(supplied_position, bool)
                        or position != supplied_position
                        or not start <= position < stop
                    ):
                        raise ValueError(
                            "A precomputed path crosses a segment boundary"
                        )
                start = stop
        selected.append(_np.asarray(subset, dtype=_np.int32) if _np is not None else subset)
    return selected[0], selected[1], selected[2]


def _path_chunks(
    n: int,
    draws: int,
    expected_block_length: float,
    seed: int,
    paths: Mapping[str, Any] | None,
    segment_lengths: Sequence[int] | None = None,
):
    if paths is None:
        yield from _generated_path_chunks(
            n, draws, expected_block_length, seed, segment_lengths
        )
        return
    x_paths, y_paths, paired_paths = _paths_from_precomputed(
        paths, n, draws, segment_lengths
    )
    for offset in range(0, draws, _PATH_CHUNK_SIZE):
        stop = min(draws, offset + _PATH_CHUNK_SIZE)
        yield x_paths[offset:stop], y_paths[offset:stop], paired_paths[offset:stop]


def _numpy_row_correlations(left: Any, right: Any) -> Any:
    left_scale = _np.max(_np.abs(left), axis=1, keepdims=True)
    right_scale = _np.max(_np.abs(right), axis=1, keepdims=True)
    with _np.errstate(divide="ignore", invalid="ignore"):
        scaled_left = _np.where(left_scale > 0, left / left_scale, 0.0)
        scaled_right = _np.where(right_scale > 0, right / right_scale, 0.0)
    centered_left = scaled_left - scaled_left.mean(axis=1, keepdims=True)
    centered_right = scaled_right - scaled_right.mean(axis=1, keepdims=True)
    numerator = _np.einsum("ij,ij->i", centered_left, centered_right)
    denominator = _np.sqrt(
        _np.einsum("ij,ij->i", centered_left, centered_left)
        * _np.einsum("ij,ij->i", centered_right, centered_right)
    )
    with _np.errstate(divide="ignore", invalid="ignore"):
        return _np.where(denominator > 0, numerator / denominator, _np.nan)


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _bootstrap_many(
    datasets: Sequence[
        tuple[Sequence[float], Sequence[float], float, Sequence[int]]
    ],
    *,
    draws: int,
    expected_block_length: float,
    seed: int,
    paths: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not datasets:
        return []
    n = len(datasets[0][0])
    segments = _normalise_segment_lengths(n, datasets[0][3])
    if any(
        len(xs) != n
        or len(ys) != n
        or _normalise_segment_lengths(n, supplied_segments) != segments
        for xs, ys, _, supplied_segments in datasets
    ):
        raise ValueError(
            "Shared bootstrap datasets must have the same sample length and segments"
        )
    extreme = [0] * len(datasets)
    valid_null = [0] * len(datasets)
    ci_counts = [0] * len(datasets)
    if _np is not None:
        ci_values: Any = _np.empty((len(datasets), draws), dtype=float)
    else:  # pragma: no cover
        ci_values = [[] for _ in datasets]
    for x_paths, y_paths, paired_paths in _path_chunks(
        n, draws, expected_block_length, seed, paths, segments
    ):
        if _np is not None:
            for index, (xs, ys, observed, _) in enumerate(datasets):
                x_array = _np.asarray(xs, dtype=float)
                y_array = _np.asarray(ys, dtype=float)
                null = _numpy_row_correlations(x_array[x_paths], y_array[y_paths])
                finite_null = null[_np.isfinite(null)]
                valid_null[index] += int(finite_null.size)
                extreme[index] += int(
                    _np.count_nonzero(_np.abs(finite_null) >= abs(observed) - 1e-15)
                )
                paired = _numpy_row_correlations(
                    x_array[paired_paths], y_array[paired_paths]
                )
                finite_paired = paired[_np.isfinite(paired)]
                start = ci_counts[index]
                stop = start + int(finite_paired.size)
                ci_values[index, start:stop] = finite_paired
                ci_counts[index] = stop
        else:  # pragma: no cover
            for path_x, path_y, path_paired in zip(
                x_paths, y_paths, paired_paths
            ):
                for index, (xs, ys, observed, _) in enumerate(datasets):
                    null_r = pearson_r(
                        [xs[position] for position in path_x],
                        [ys[position] for position in path_y],
                    )
                    if null_r is not None:
                        valid_null[index] += 1
                        if abs(null_r) >= abs(observed) - 1e-15:
                            extreme[index] += 1
                    paired_r = pearson_r(
                        [xs[position] for position in path_paired],
                        [ys[position] for position in path_paired],
                    )
                    if paired_r is not None:
                        ci_values[index].append(paired_r)
                        ci_counts[index] += 1
    results: list[dict[str, Any]] = []
    for index in range(len(datasets)):
        valid = valid_null[index]
        p_value = (extreme[index] + 1.0) / (valid + 1.0) if valid else None
        if _np is not None and ci_counts[index]:
            low, high = (
                float(value)
                for value in _np.percentile(
                    ci_values[index, : ci_counts[index]], [2.5, 97.5]
                )
            )
        elif _np is None:
            low = _percentile(ci_values[index], 0.025)
            high = _percentile(ci_values[index], 0.975)
        else:
            low = high = None
        results.append(
            {
                "p": p_value,
                "p_value": p_value,
                "null_p": p_value,
                "ci_95": [low, high] if low is not None and high is not None else None,
                "ci_lower": low,
                "ci_upper": high,
                "draws": draws,
                "valid_null_draws": valid,
                "valid_ci_draws": ci_counts[index],
                "segment_lengths": list(segments),
                "segment_count": len(segments),
                "status": "ok" if p_value is not None else "degenerate_bootstrap",
            }
        )
    return results


def stationary_bootstrap_test(
    xs: Sequence[float],
    ys: Sequence[float],
    *,
    draws: int = DEFAULT_BOOTSTRAP_DRAWS,
    expected_block_length: float = DEFAULT_BLOCK_LENGTH,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    paths: Mapping[str, Any] | None = None,
    segment_lengths: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Test correlation using independent stationary paths and paired-path CI."""
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have equal lengths")
    if isinstance(draws, bool) or not isinstance(draws, int) or draws < 0:
        raise ValueError("draws must be a non-negative integer")
    block = _finite_number(expected_block_length)
    if block is None or block < 1.0:
        raise ValueError("expected_block_length must be at least 1")
    supplied_segments = _normalise_segment_lengths(len(xs), segment_lengths)
    clean_pairs: list[tuple[float, float]] = []
    clean_segment_lengths: list[int] = []
    start = 0
    for length in supplied_segments:
        run_length = 0
        for position in range(start, start + length):
            left = _finite_number(xs[position])
            right = _finite_number(ys[position])
            if left is None or right is None:
                if run_length:
                    clean_segment_lengths.append(run_length)
                    run_length = 0
                continue
            clean_pairs.append((left, right))
            run_length += 1
        if run_length:
            clean_segment_lengths.append(run_length)
        start += length
    clean_x = [float(pair[0]) for pair in clean_pairs]
    clean_y = [float(pair[1]) for pair in clean_pairs]
    n = len(clean_x)
    if segment_lengths is None:
        clean_segment_lengths = [n] if n else []
    segments = _normalise_segment_lengths(n, clean_segment_lengths)
    observed = pearson_r(clean_x, clean_y)
    base = {
        "n": n,
        "segment_lengths": list(segments),
        "segment_count": len(segments),
        "observed_r": observed,
        "p": None,
        "p_value": None,
        "null_p": None,
        "ci_95": None,
        "ci_lower": None,
        "ci_upper": None,
        "draws": draws,
        "valid_null_draws": 0,
        "valid_ci_draws": 0,
        "expected_block_length": block,
        "seed": int(seed),
        "independent_null_paths": True,
        "paired_ci_paths": True,
        "inferential_eligible": n >= MIN_INFERENTIAL_N and observed is not None,
    }
    if n < MIN_DESCRIPTIVE_N:
        return {**base, "status": "insufficient_n"}
    if observed is None:
        return {**base, "status": "zero_variance"}
    if draws == 0:
        return {**base, "status": "not_run"}
    result = _bootstrap_many(
        [(clean_x, clean_y, observed, segments)],
        draws=draws,
        expected_block_length=block,
        seed=seed,
        paths=paths,
    )[0]
    output = {**base, **result}
    if n < MIN_INFERENTIAL_N and output["status"] == "ok":
        output["status"] = "descriptive_only"
    return output


# ---------------------------------------------------------------------------
# Holm family and lag grids
# ---------------------------------------------------------------------------


def holm_adjusted_pvalues(
    p_values: Sequence[float | None], eligible: Sequence[bool] | None = None
) -> list[float]:
    """Holm-adjust a fixed family, assigning ineligible/missing tests p=1."""
    if eligible is not None and len(eligible) != len(p_values):
        raise ValueError("eligible must have the same length as p_values")
    effective: list[float] = []
    for index, supplied in enumerate(p_values):
        allowed = eligible[index] if eligible is not None else True
        value = _finite_number(supplied)
        effective.append(
            max(0.0, min(1.0, value))
            if allowed and value is not None
            else 1.0
        )
    family_size = len(effective)
    ordered = sorted(range(family_size), key=lambda index: (effective[index], index))
    adjusted = [1.0] * family_size
    running = 0.0
    for rank, index in enumerate(ordered):
        candidate = min(1.0, (family_size - rank) * effective[index])
        running = max(running, candidate)
        adjusted[index] = running
    return adjusted


def holm_correct_family(
    hypotheses: Sequence[Mapping[str, Any]],
    *,
    p_key: str = "bootstrap_p",
    eligibility_key: str = "inferential_eligible",
) -> list[dict[str, Any]]:
    """Copy a planned hypothesis family and store its fixed-family Holm p."""
    p_values = [hypothesis.get(p_key) for hypothesis in hypotheses]
    eligibility = [
        bool(hypothesis.get(eligibility_key, True)) for hypothesis in hypotheses
    ]
    adjusted = holm_adjusted_pvalues(p_values, eligibility)
    result: list[dict[str, Any]] = []
    for hypothesis, adjusted_p, allowed in zip(
        hypotheses, adjusted, eligibility
    ):
        row = dict(hypothesis)
        supplied = _finite_number(hypothesis.get(p_key))
        row["holm_input_p"] = supplied if allowed and supplied is not None else 1.0
        row["holm_adjusted_p"] = adjusted_p
        row["adjusted_p"] = adjusted_p
        result.append(row)
    return result


def _normalise_windows(windows: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if isinstance(windows, Mapping):
        iterable = list(windows.items())
    elif isinstance(windows, Sequence) and not isinstance(windows, (str, bytes)):
        iterable = [(None, item) for item in windows]
    else:
        raise TypeError("windows must be a mapping or sequence")
    for supplied_name, supplied in iterable:
        if isinstance(supplied, Mapping):
            name = supplied_name or supplied.get("name") or supplied.get("id")
            start = supplied.get("start_quarter", supplied.get("start"))
            end = supplied.get("end_quarter", supplied.get("end"))
        elif isinstance(supplied, Sequence) and not isinstance(supplied, (str, bytes)):
            if len(supplied) != 2:
                raise ValueError("A window sequence must contain start and end")
            name, start, end = supplied_name, supplied[0], supplied[1]
        else:
            raise TypeError("Each window must be a mapping or two-item sequence")
        if name is None or not isinstance(start, str) or not isinstance(end, str):
            raise ValueError("Each window requires name, start_quarter, and end_quarter")
        quarter_range(start, end)
        result.append(
            {
                "name": str(name),
                "start_quarter": start,
                "end_quarter": end,
            }
        )
    names = [window["name"] for window in result]
    if len(set(names)) != len(names):
        raise ValueError("Window names must be unique")
    if not result:
        raise ValueError("At least one analysis window is required")
    return result


def _normalise_layers(layers: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(layers, Mapping):
        iterable = list(layers.items())
    elif isinstance(layers, Sequence) and not isinstance(layers, (str, bytes)):
        iterable = [(None, item) for item in layers]
    else:
        raise TypeError("layers must be a mapping or sequence")
    for supplied_name, supplied in iterable:
        if supplied_name is not None:
            if isinstance(supplied, Mapping) and any(
                key in supplied for key in ("series", "yoy", "rows", "revenue_yoy")
            ):
                series = next(
                    supplied[key]
                    for key in ("series", "yoy", "rows", "revenue_yoy")
                    if key in supplied
                )
                control = bool(supplied.get("is_control", supplied.get("control", False)))
            else:
                series = supplied
                control = False
            name = supplied_name
        else:
            if not isinstance(supplied, Mapping):
                raise TypeError("Layer sequence entries must be mappings")
            name = supplied.get("name", supplied.get("id", supplied.get("layer")))
            series = next(
                (
                    supplied[key]
                    for key in ("series", "yoy", "rows", "revenue_yoy")
                    if key in supplied
                ),
                None,
            )
            control = bool(supplied.get("is_control", supplied.get("control", False)))
        if name is None or series is None:
            raise ValueError("Each layer requires a name/id and quarterly series")
        result.append({"name": str(name), "series": series, "is_control": control})
    names = [layer["name"] for layer in result]
    if len(set(names)) != len(names):
        raise ValueError("Layer names must be unique")
    if not result:
        raise ValueError("At least one layer is required")
    return result


def _precomputed_for_shape(
    paths: Mapping[Any, Any] | None,
    n: int,
    segment_lengths: Sequence[int],
) -> Any:
    if paths is None:
        return None
    if paths.get("n") == n and (
        "x_paths" in paths or "x" in paths
    ):
        return paths
    segments = tuple(segment_lengths)
    key = _path_shape_key(n, segments)
    selected = paths.get((n, segments), paths.get(key))
    if selected is not None:
        return selected
    if segments == (n,):
        return paths.get(n, paths.get(str(n)))
    return None


def _attach_bootstrap_to_cells(
    cells: list[dict[str, Any]],
    *,
    draws: int,
    expected_block_length: float,
    seed: int,
    precomputed_paths: Mapping[Any, Any] | None,
) -> None:
    for cell in cells:
        cell.update(
            {
                "bootstrap_p": None,
                "bootstrap_ci_95": None,
                "bootstrap_draws": draws,
                "bootstrap_valid_draws": 0,
                "bootstrap_status": "not_eligible",
            }
        )
    if draws == 0:
        for cell in cells:
            if cell["n"] >= MIN_DESCRIPTIVE_N and cell["r"] is not None:
                cell["bootstrap_status"] = "not_run"
        return
    groups: dict[tuple[int, tuple[int, ...]], list[int]] = defaultdict(list)
    for index, cell in enumerate(cells):
        if cell["n"] >= MIN_DESCRIPTIVE_N and cell["r"] is not None:
            segments = _normalise_segment_lengths(
                cell["n"], cell.get("segment_lengths")
            )
            groups[(cell["n"], segments)].append(index)
    for (n, segments), indices in groups.items():
        datasets: list[
            tuple[Sequence[float], Sequence[float], float, Sequence[int]]
        ] = []
        for index in indices:
            pairs = cells[index]["pairs"]
            datasets.append(
                (
                    [row["capex_yoy"] for row in pairs],
                    [row["revenue_yoy"] for row in pairs],
                    cells[index]["r"],
                    segments,
                )
            )
        results = _bootstrap_many(
            datasets,
            draws=draws,
            expected_block_length=expected_block_length,
            seed=seed,
            paths=_precomputed_for_shape(precomputed_paths, n, segments),
        )
        for index, result in zip(indices, results):
            bootstrap_status = result["status"]
            if n < MIN_INFERENTIAL_N and bootstrap_status == "ok":
                bootstrap_status = "descriptive_only"
            cells[index].update(
                {
                    "bootstrap_p": result["p_value"],
                    "bootstrap_ci_95": result["ci_95"],
                    "bootstrap_valid_draws": result["valid_null_draws"],
                    "bootstrap_status": bootstrap_status,
                }
            )


def _selection_record(cell: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if cell is None:
        return None
    return {
        key: cell.get(key)
        for key in (
            "lag",
            "n",
            "segment_lengths",
            "segment_count",
            "segment_boundaries",
            "r",
            "p",
            "bootstrap_p",
            "bootstrap_ci_95",
            "holm_adjusted_p",
            "status",
        )
    }


def _summarise_grid(
    cells: Sequence[Mapping[str, Any]],
    layers: Sequence[Mapping[str, Any]],
    windows: Sequence[Mapping[str, str]],
    alpha: float,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for layer in layers:
        window_results: list[dict[str, Any]] = []
        for window in windows:
            selected = [
                cell
                for cell in cells
                if cell["layer"] == layer["name"]
                and cell["window"] == window["name"]
            ]
            supported = [
                cell
                for cell in selected
                if cell.get("inferential_eligible")
                and cell.get("r") is not None
                and cell["r"] > 0
                and cell.get("holm_adjusted_p") is not None
                and cell["holm_adjusted_p"] < alpha
            ]
            best = max(supported, key=lambda cell: (cell["r"], -cell["lag"])) if supported else None
            descriptive = [
                cell
                for cell in selected
                if cell.get("n", 0) >= MIN_DESCRIPTIVE_N
                and cell.get("r") is not None
                and cell["r"] > 0
            ]
            peak = max(descriptive, key=lambda cell: (cell["r"], -cell["lag"])) if descriptive else None
            window_results.append(
                {
                    "window": window["name"],
                    "start_quarter": window["start_quarter"],
                    "end_quarter": window["end_quarter"],
                    "best_supported_lag": best["lag"] if best else None,
                    "best_supported": _selection_record(best),
                    "descriptive_peak_lag": peak["lag"] if peak else None,
                    "descriptive_peak": _selection_record(peak),
                    "status": (
                        "supported"
                        if best
                        else "not_significant"
                        if peak and peak.get("n", 0) >= MIN_INFERENTIAL_N
                        else "descriptive_only"
                        if peak
                        and MIN_DESCRIPTIVE_N
                        <= peak.get("n", 0)
                        < MIN_INFERENTIAL_N
                        else "not_assessable"
                    ),
                }
            )
        summaries.append(
            {
                "layer": layer["name"],
                "is_control": bool(layer.get("is_control")),
                "windows": window_results,
            }
        )
    return summaries


def full_lag_grid(
    capex_yoy: Any,
    layer_yoy: Any,
    windows: Any,
    *,
    max_lag: int = 8,
    draws: int = DEFAULT_BOOTSTRAP_DRAWS,
    expected_block_length: float = DEFAULT_BLOCK_LENGTH,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    alpha: float = 0.05,
    detrend: bool = False,
    break_quarter: str = "2020Q1",
    precomputed_paths: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """Run one fixed Holm family over every supplied layer, window, and lag."""
    if isinstance(max_lag, bool) or not isinstance(max_lag, int) or not 0 <= max_lag <= 8:
        raise ValueError("max_lag must be an integer from 0 through 8")
    if isinstance(draws, bool) or not isinstance(draws, int) or draws < 0:
        raise ValueError("draws must be a non-negative integer")
    block = _finite_number(expected_block_length)
    significance = _finite_number(alpha)
    if block is None or block < 1.0:
        raise ValueError("expected_block_length must be at least 1")
    if significance is None or not 0 < significance < 1:
        raise ValueError("alpha must lie strictly between zero and one")
    normalised_windows = _normalise_windows(windows)
    normalised_layers = _normalise_layers(layer_yoy)
    cells: list[dict[str, Any]] = []
    detrending_details: list[dict[str, Any]] = []

    for window in normalised_windows:
        capex_for_window = capex_yoy
        if detrend:
            capex_detail = detrend_yoy_with_diagnostics(
                capex_yoy,
                start_quarter=window["start_quarter"],
                end_quarter=window["end_quarter"],
                break_quarter=break_quarter,
            )
            capex_for_window = capex_detail["series"]
            detrending_details.append(
                {
                    "window": window["name"],
                    "series": "CAPEX",
                    "n": capex_detail["n"],
                    "rank": capex_detail["rank"],
                    "terms": capex_detail["terms"],
                    "status": capex_detail["status"],
                }
            )
        for layer in normalised_layers:
            revenue_for_window = layer["series"]
            if detrend:
                revenue_detail = detrend_yoy_with_diagnostics(
                    layer["series"],
                    start_quarter=window["start_quarter"],
                    end_quarter=window["end_quarter"],
                    break_quarter=break_quarter,
                )
                revenue_for_window = revenue_detail["series"]
                detrending_details.append(
                    {
                        "window": window["name"],
                        "series": layer["name"],
                        "n": revenue_detail["n"],
                        "rank": revenue_detail["rank"],
                        "terms": revenue_detail["terms"],
                        "status": revenue_detail["status"],
                    }
                )
            for lag in range(max_lag + 1):
                cell = lagged_correlation(
                    capex_for_window,
                    revenue_for_window,
                    lag,
                    window["start_quarter"],
                    window["end_quarter"],
                )
                cell.update(
                    {
                        "layer": layer["name"],
                        "is_control": layer["is_control"],
                        "window": window["name"],
                        "detrended": detrend,
                    }
                )
                cells.append(cell)

    _attach_bootstrap_to_cells(
        cells,
        draws=draws,
        expected_block_length=block,
        seed=seed,
        precomputed_paths=precomputed_paths,
    )
    corrected = holm_correct_family(cells)
    for cell in corrected:
        cell["supports_transmission"] = bool(
            cell["inferential_eligible"]
            and cell.get("r") is not None
            and cell["r"] > 0
            and cell["holm_adjusted_p"] < significance
        )
        cell["support_status"] = (
            "supported"
            if cell["supports_transmission"]
            else "ineligible"
            if not cell["inferential_eligible"]
            else "negative_or_zero"
            if cell.get("r") is not None and cell["r"] <= 0
            else "not_significant"
        )
    summaries = _summarise_grid(
        corrected, normalised_layers, normalised_windows, significance
    )
    return {
        "cells": corrected,
        "layers": summaries,
        "windows": normalised_windows,
        "family_size": len(corrected),
        "max_lag": max_lag,
        "alpha": significance,
        "detrended": detrend,
        "detrending": {
            "break_quarter": break_quarter,
            "terms": [
                "intercept",
                "linear_quarter_index",
                "post_level",
                "post_slope",
            ],
            "details": detrending_details,
        },
        "bootstrap": {
            "method": "circular_stationary",
            "draws": draws,
            "expected_block_length": block,
            "seed": int(seed),
            "independent_x_y_null_paths": True,
            "paired_ci_paths": True,
            "path_sharing": "shared across equal-length, equal-segment-shape cells",
            "path_generator": "stationary_bootstrap_paths",
        },
    }


run_full_lag_grid = full_lag_grid


def _window_summary(
    grid: Mapping[str, Any], layer_name: str, window_name: str
) -> Mapping[str, Any] | None:
    for layer in grid.get("layers") or []:
        if layer.get("layer") != layer_name:
            continue
        for window in layer.get("windows") or []:
            if window.get("window") == window_name:
                return window
    return None


def assess_split_consistency(
    raw_grid: Mapping[str, Any],
    *,
    full_window: str = "full",
    early_window: str = "early",
    late_window: str = "late",
) -> dict[str, Any]:
    """Require each split's exact positive descriptive peak to equal full lag."""
    results: list[dict[str, Any]] = []
    for layer in raw_grid.get("layers") or []:
        name = layer.get("layer")
        full = _window_summary(raw_grid, name, full_window)
        early = _window_summary(raw_grid, name, early_window)
        late = _window_summary(raw_grid, name, late_window)
        full_lag = (full or {}).get("best_supported_lag")
        early_peak = (early or {}).get("descriptive_peak")
        late_peak = (late or {}).get("descriptive_peak")
        assessable = bool(full_lag is not None and early_peak and late_peak)
        passed = bool(
            assessable
            and early_peak.get("n", 0) >= MIN_DESCRIPTIVE_N
            and late_peak.get("n", 0) >= MIN_DESCRIPTIVE_N
            and early_peak.get("lag") == full_lag
            and late_peak.get("lag") == full_lag
            and early_peak.get("r") is not None
            and late_peak.get("r") is not None
            and early_peak["r"] > 0
            and late_peak["r"] > 0
        )
        results.append(
            {
                "layer": name,
                "full_supported_lag": full_lag,
                "early_descriptive_peak_lag": (early_peak or {}).get("lag"),
                "late_descriptive_peak_lag": (late_peak or {}).get("lag"),
                "early_n": (early_peak or {}).get("n"),
                "late_n": (late_peak or {}).get("n"),
                "early_r": (early_peak or {}).get("r"),
                "late_r": (late_peak or {}).get("r"),
                "assessable": assessable,
                "pass": passed,
                "status": "pass" if passed else "fail" if assessable else "not_assessable",
            }
        )
    return {
        "full_window": full_window,
        "early_window": early_window,
        "late_window": late_window,
        "layers": results,
        "all_pass": bool(results) and all(result["pass"] for result in results),
    }


def assess_trend_robustness(
    raw_grid: Mapping[str, Any],
    detrended_grid: Mapping[str, Any],
    *,
    full_window: str = "full",
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Check significance and positivity at each raw full supported exact lag."""
    results: list[dict[str, Any]] = []
    for layer in raw_grid.get("layers") or []:
        name = layer.get("layer")
        raw_summary = _window_summary(raw_grid, name, full_window)
        lag = (raw_summary or {}).get("best_supported_lag")
        matching = next(
            (
                cell
                for cell in detrended_grid.get("cells") or []
                if cell.get("layer") == name
                and cell.get("window") == full_window
                and cell.get("lag") == lag
            ),
            None,
        ) if lag is not None else None
        assessable = bool(
            lag is not None
            and matching
            and matching.get("inferential_eligible")
            and matching.get("holm_adjusted_p") is not None
            and matching.get("r") is not None
        )
        passed = bool(
            assessable
            and matching["r"] > 0
            and matching["holm_adjusted_p"] < alpha
        )
        results.append(
            {
                "layer": name,
                "raw_supported_lag": lag,
                "detrended_r": (matching or {}).get("r"),
                "detrended_holm_adjusted_p": (matching or {}).get(
                    "holm_adjusted_p"
                ),
                "detrended_n": (matching or {}).get("n"),
                "assessable": assessable,
                "pass": passed,
                "status": "pass" if passed else "fail" if assessable else "not_assessable",
            }
        )
    return {
        "full_window": full_window,
        "layers": results,
        "all_pass": bool(results) and all(result["pass"] for result in results),
    }


def run_raw_and_detrended_families(
    capex_yoy: Any,
    layer_yoy: Any,
    windows: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run raw and OLS-detrended versions of the identical planned family."""
    raw_kwargs = dict(kwargs)
    raw_kwargs.pop("detrend", None)
    raw = full_lag_grid(capex_yoy, layer_yoy, windows, detrend=False, **raw_kwargs)
    detrended = full_lag_grid(
        capex_yoy, layer_yoy, windows, detrend=True, **raw_kwargs
    )
    return {
        "raw": raw,
        "detrended": detrended,
        "trend_robustness": assess_trend_robustness(
            raw, detrended, alpha=float(raw["alpha"])
        ),
    }


# ---------------------------------------------------------------------------
# Cycle diagnostics and final decision
# ---------------------------------------------------------------------------


def effective_cycle_diagnostics(
    capex_yoy: Any,
    *,
    value_key: str = "yoy",
    start_quarter: str | None = None,
    end_quarter: str | None = None,
    smoothing_quarters: int = 4,
    extrema_radius: int = 2,
    min_phase_quarters: int = 4,
    min_cycle_quarters: int = 8,
) -> dict[str, Any]:
    """Count complete alternating trough-to-trough cycles in smoothed CAPEX."""
    if smoothing_quarters != 4 or extrema_radius != 2:
        raise ValueError("The preregistered smoothing window/radius are 4 and 2")
    if min_phase_quarters < 1 or min_cycle_quarters < 1:
        raise ValueError("Minimum phase and cycle lengths must be positive")
    values = _finite_series_map(capex_yoy, value_key)
    if start_quarter is not None or end_quarter is not None:
        if start_quarter is None or end_quarter is None:
            raise ValueError("Both start_quarter and end_quarter are required")
        values = {
            quarter: value
            for quarter, value in values.items()
            if quarter_in_window(quarter, start_quarter, end_quarter)
        }
    if not values:
        return {
            "smoothed": [],
            "segment_boundaries": [],
            "segment_count": 0,
            "candidate_extrema": [],
            "extrema": [],
            "cycles": [],
            "cycle_boundaries": [],
            "cycle_count": 0,
            "status": "no_data",
        }
    first = min(values, key=quarter_index)
    last = max(values, key=quarter_index)
    smoothed: list[dict[str, Any]] = []
    smooth_map: dict[str, float] = {}
    for quarter in quarter_range(first, last):
        trailing = [shift_quarter(quarter, offset) for offset in range(-3, 1)]
        if all(item in values for item in trailing):
            smooth = sum(values[item] for item in trailing) / 4.0
            smooth_map[quarter] = smooth
            status = "complete"
        else:
            smooth = None
            status = "incomplete_trailing_window"
        smoothed.append(
            {
                "calendar_quarter": quarter,
                "quarter": quarter,
                "trailing_4q_mean": smooth,
                "value": smooth,
                "status": status,
            }
        )

    smooth_quarters = sorted(smooth_map, key=quarter_index)
    segment_slices = _contiguous_quarter_slices(smooth_quarters)
    segment_boundaries = [
        {
            "segment": segment,
            "start_quarter": smooth_quarters[start],
            "end_quarter": smooth_quarters[stop - 1],
            "length_quarters": stop - start,
        }
        for segment, (start, stop) in enumerate(segment_slices, start=1)
    ]
    candidates: list[dict[str, Any]] = []
    for segment, (start, stop) in enumerate(segment_slices, start=1):
        segment_quarters = smooth_quarters[start:stop]
        segment_set = set(segment_quarters)
        for quarter in segment_quarters:
            neighborhood = [
                shift_quarter(quarter, offset)
                for offset in range(-extrema_radius, extrema_radius + 1)
            ]
            if not all(item in segment_set for item in neighborhood):
                continue
            local = [smooth_map[item] for item in neighborhood]
            value = smooth_map[quarter]
            if value == min(local) and local.count(value) == 1:
                candidates.append(
                    {
                        "quarter": quarter,
                        "type": "trough",
                        "value": value,
                        "segment": segment,
                    }
                )
            elif value == max(local) and local.count(value) == 1:
                candidates.append(
                    {
                        "quarter": quarter,
                        "type": "peak",
                        "value": value,
                        "segment": segment,
                    }
                )

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        if not accepted or candidate["segment"] != accepted[-1]["segment"]:
            accepted.append(dict(candidate))
            continue
        previous = accepted[-1]
        if candidate["type"] == previous["type"]:
            more_extreme = (
                candidate["value"] < previous["value"]
                if candidate["type"] == "trough"
                else candidate["value"] > previous["value"]
            )
            if more_extreme:
                removed = dict(previous)
                removed["reason"] = "superseded_by_more_extreme_same_type"
                rejected.append(removed)
                accepted[-1] = dict(candidate)
            else:
                removed = dict(candidate)
                removed["reason"] = "less_extreme_same_type"
                rejected.append(removed)
            continue
        distance = quarter_index(candidate["quarter"]) - quarter_index(
            previous["quarter"]
        )
        if distance < min_phase_quarters:
            removed = dict(candidate)
            removed["reason"] = "phase_shorter_than_minimum"
            rejected.append(removed)
            continue
        accepted.append(dict(candidate))

    cycles: list[dict[str, Any]] = []
    for index in range(len(accepted) - 2):
        start, peak, end = accepted[index : index + 3]
        if len({start["segment"], peak["segment"], end["segment"]}) != 1:
            continue
        if [start["type"], peak["type"], end["type"]] != [
            "trough",
            "peak",
            "trough",
        ]:
            continue
        rise = quarter_index(peak["quarter"]) - quarter_index(start["quarter"])
        fall = quarter_index(end["quarter"]) - quarter_index(peak["quarter"])
        length = quarter_index(end["quarter"]) - quarter_index(start["quarter"])
        if rise < min_phase_quarters or fall < min_phase_quarters:
            continue
        if length < min_cycle_quarters:
            continue
        if not all(
            quarter in smooth_map
            for quarter in quarter_range(start["quarter"], end["quarter"])
        ):
            continue
        cycles.append(
            {
                "start_trough": start["quarter"],
                "peak": peak["quarter"],
                "end_trough": end["quarter"],
                "start_value": start["value"],
                "peak_value": peak["value"],
                "end_value": end["value"],
                "rise_quarters": rise,
                "fall_quarters": fall,
                "length_quarters": length,
                "segment": start["segment"],
            }
        )
    return {
        "smoothed": smoothed,
        "segment_boundaries": segment_boundaries,
        "segment_count": len(segment_boundaries),
        "candidate_extrema": candidates,
        "extrema": accepted,
        "rejected_extrema": rejected,
        "cycles": cycles,
        "cycle_boundaries": [
            {
                "start_trough": cycle["start_trough"],
                "end_trough": cycle["end_trough"],
            }
            for cycle in cycles
        ],
        "cycle_count": len(cycles),
        "settings": {
            "smoothing_quarters": smoothing_quarters,
            "extrema_radius": extrema_radius,
            "min_phase_quarters": min_phase_quarters,
            "min_cycle_quarters": min_cycle_quarters,
        },
        "status": "ok",
    }


def _layer_names(grid: Mapping[str, Any]) -> list[str]:
    return [
        str(layer.get("layer"))
        for layer in grid.get("layers") or []
        if layer.get("layer") is not None
    ]


def transmission_decision(
    raw_grid: Mapping[str, Any],
    detrended_grid: Mapping[str, Any],
    *,
    split_consistency: Mapping[str, Any] | None = None,
    trend_robustness: Mapping[str, Any] | None = None,
    control_layer: str = "TSMC",
    full_window: str = "full",
    early_window: str = "early",
    late_window: str = "late",
    min_supported_layers: int = 3,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Apply all four preregistered global criteria; non-assessable means fail."""
    split = split_consistency or assess_split_consistency(
        raw_grid,
        full_window=full_window,
        early_window=early_window,
        late_window=late_window,
    )
    trend = trend_robustness or assess_trend_robustness(
        raw_grid, detrended_grid, full_window=full_window, alpha=alpha
    )
    layer_metadata = {
        str(layer.get("layer")): bool(layer.get("is_control"))
        for layer in raw_grid.get("layers") or []
        if layer.get("layer") is not None
    }
    control_matches = [
        name
        for name, marked in layer_metadata.items()
        if marked or name.casefold() == control_layer.casefold()
    ]
    control_name = control_matches[0] if len(control_matches) == 1 else None
    supported_noncontrol: list[str] = []
    for name in _layer_names(raw_grid):
        if name == control_name or layer_metadata.get(name, False):
            continue
        summary = _window_summary(raw_grid, name, full_window)
        if (summary or {}).get("best_supported_lag") is not None:
            supported_noncontrol.append(name)

    noncontrol_full_cells = [
        cell
        for cell in raw_grid.get("cells") or []
        if cell.get("window") == full_window
        and not bool(cell.get("is_control"))
    ]
    criterion_one_assessable = any(
        bool(cell.get("inferential_eligible")) for cell in noncontrol_full_cells
    )
    criterion_one = bool(
        criterion_one_assessable
        and len(supported_noncontrol) >= min_supported_layers
    )
    split_by_layer = {
        row.get("layer"): row for row in split.get("layers") or []
    }
    split_assessable = bool(supported_noncontrol) and all(
        name in split_by_layer and split_by_layer[name].get("assessable")
        for name in supported_noncontrol
    )
    criterion_two = bool(
        split_assessable
        and all(split_by_layer[name].get("pass") for name in supported_noncontrol)
    )
    trend_by_layer = {
        row.get("layer"): row for row in trend.get("layers") or []
    }
    trend_assessable = bool(supported_noncontrol) and all(
        name in trend_by_layer and trend_by_layer[name].get("assessable")
        for name in supported_noncontrol
    )
    criterion_three = bool(
        trend_assessable
        and all(trend_by_layer[name].get("pass") for name in supported_noncontrol)
    )

    control_cells = [
        cell
        for cell in raw_grid.get("cells") or []
        if control_name is not None
        and cell.get("layer") == control_name
        and cell.get("window") == full_window
    ]
    # Planned cells below the inferential n threshold remain in the fixed Holm
    # family with p=1. They cannot create a finding, but they also must not make
    # the control structurally unassessable whenever high lags lose observations.
    control_assessable = bool(control_cells) and any(
        cell.get("inferential_eligible")
        and cell.get("bootstrap_p") is not None
        and cell.get("r") is not None
        for cell in control_cells
    ) and all(cell.get("holm_adjusted_p") is not None for cell in control_cells)
    positive_control_hits = [
        cell["lag"]
        for cell in control_cells
        if cell.get("r") is not None
        and cell["r"] > 0
        and cell.get("holm_adjusted_p") is not None
        and cell["holm_adjusted_p"] < alpha
    ]
    criterion_four = bool(control_assessable and not positive_control_hits)
    criteria = [criterion_one, criterion_two, criterion_three, criterion_four]
    passed = all(criteria)
    return {
        "pass": passed,
        "proceed_to_strategy": passed,
        "decision": "pass" if passed else "fail",
        "conclusion": (
            "傳導關係穩定,進入策略設計階段" if passed else FAIL_CONCLUSION
        ),
        "supported_noncontrol_layers": supported_noncontrol,
        "criteria": {
            "at_least_three_noncontrol_layers_supported": {
                "pass": criterion_one,
                "assessable": criterion_one_assessable,
                "count": len(supported_noncontrol),
                "required": min_supported_layers,
            },
            "exact_split_consistency": {
                "pass": criterion_two,
                "assessable": split_assessable,
            },
            "exact_lag_detrended_significance": {
                "pass": criterion_three,
                "assessable": trend_assessable,
            },
            "tsmc_control_no_positive_significant_lag": {
                "pass": criterion_four,
                "assessable": control_assessable,
                "control_layer": control_name,
                "positive_significant_lags": positive_control_hits,
            },
        },
        "split_consistency": split,
        "trend_robustness": trend,
    }


decision = transmission_decision


__all__ = [
    "DEFAULT_BLOCK_LENGTH",
    "DEFAULT_BOOTSTRAP_DRAWS",
    "DEFAULT_BOOTSTRAP_SEED",
    "FAIL_CONCLUSION",
    "MIN_DESCRIPTIVE_N",
    "MIN_INFERENTIAL_N",
    "assess_split_consistency",
    "assess_trend_robustness",
    "autocorrelation",
    "autocorrelation_effective_sample_size",
    "build_capex_aggregate",
    "build_fixed_member_panel",
    "build_fixed_member_quarterly_panel",
    "build_fixed_member_revenue_aggregate",
    "build_four_company_capex_aggregate",
    "build_layer_revenue_aggregate",
    "decision",
    "detrend_yoy_series",
    "detrend_yoy_with_diagnostics",
    "effective_cycle_diagnostics",
    "effective_sample_size",
    "filter_quarter_rows",
    "filter_quarters",
    "full_lag_grid",
    "holm_adjusted_pvalues",
    "holm_correct_family",
    "lagged_correlation",
    "parse_quarter",
    "pearson_r",
    "pearson_t_pvalue",
    "precompute_stationary_bootstrap_paths",
    "quarter_from_index",
    "quarter_in_window",
    "quarter_index",
    "quarter_range",
    "run_full_lag_grid",
    "run_raw_and_detrended_families",
    "shift_quarter",
    "stationary_bootstrap_paths",
    "stationary_bootstrap_test",
    "transmission_decision",
    "yoy_from_complete_levels",
]
