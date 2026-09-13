"""SEC data sources for the independent CAPEX transmission study.

The module uses only SEC filings, the standard library, and the project's
file cache.  Company-fact arithmetic is deliberately exposed as pure helpers
so it can be checked without making network requests.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence

from cache import cache_get, cache_set


_CACHE_VERSION = "sec-v2"
# Raw responses are immutable URL snapshots; parser-version changes should not
# force hundreds of unchanged SEC archive documents to be downloaded again.
_RAW_CACHE_VERSION = "sec-v1"
_RAW_CACHE_PREFIX = f"{_RAW_CACHE_VERSION}_raw"
_ASML_CACHE_KEY = f"{_CACHE_VERSION}_asml_quarterly_revenue"
_SEC_TIMEOUT_SECONDS = 30.0
_SEC_MIN_INTERVAL_SECONDS = 0.11
_SEC_REQUEST_LOCK = threading.Lock()
_LAST_SEC_REQUEST = 0.0

SEC_USER_AGENT_FALLBACK = (
    "Stock_analyze CAPEX study "
    "https://github.com/pagggggg/stock-analyze-dashboard "
    "(set SEC_USER_AGENT for a direct contact)"
)
SEC_USER_AGENT = SEC_USER_AGENT_FALLBACK
SEC_USER_AGENT_SOURCE = "local_research_fallback"

_FORM_RE = re.compile(r"^10-(?:Q|K)(?:/A)?$", re.IGNORECASE)
_QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")
_ASML_CIK = "0000937966"
_ASML_ARCHIVE_CIK = "937966"

_FACT_METADATA = (
    "start",
    "end",
    "val",
    "unit",
    "accn",
    "fy",
    "fp",
    "form",
    "filed",
    "frame",
    "taxonomy",
    "tag",
    "source_url",
)


def _load_env() -> None:
    """Load the repository-root .env without replacing process settings."""
    envp = Path(__file__).resolve().parents[1] / ".env"
    if envp.exists():
        for line in envp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def sec_request_identity() -> dict[str, str]:
    """Return and expose the User-Agent identity used for SEC requests."""
    global SEC_USER_AGENT, SEC_USER_AGENT_SOURCE
    _load_env()
    configured = os.environ.get("SEC_USER_AGENT", "").strip()
    if configured:
        SEC_USER_AGENT = configured
        SEC_USER_AGENT_SOURCE = "SEC_USER_AGENT"
    else:
        SEC_USER_AGENT = SEC_USER_AGENT_FALLBACK
        SEC_USER_AGENT_SOURCE = "local_research_fallback"
    return {
        "user_agent": SEC_USER_AGENT,
        "source": SEC_USER_AGENT_SOURCE,
    }


# Make the selected fallback/configured identity visible immediately on import.
sec_request_identity()


def _normalise_cik(cik: str | int) -> str:
    text = str(cik).strip()
    if text.upper().startswith("CIK"):
        text = text[3:]
    if not text.isdigit() or len(text) > 10:
        raise ValueError(f"Invalid SEC CIK: {cik!r}")
    return text.zfill(10)


def _raw_cache_key(kind: str, url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    safe_kind = re.sub(r"[^a-z0-9_-]+", "_", kind.lower())
    return f"{_RAW_CACHE_PREFIX}_{safe_kind}_{digest}"


def _request_text(url: str) -> tuple[str, str]:
    """Request one SEC resource, enforcing spacing between request starts."""
    global _LAST_SEC_REQUEST
    identity = sec_request_identity()
    headers = {
        "User-Agent": identity["user_agent"],
        "Accept-Encoding": "identity",
        "Accept": "application/json,text/html,application/xhtml+xml,text/plain,*/*",
    }
    last_error: Exception | str | None = None

    for attempt in range(3):
        try:
            with _SEC_REQUEST_LOCK:
                wait = _SEC_MIN_INTERVAL_SECONDS - (
                    time.monotonic() - _LAST_SEC_REQUEST
                )
                if wait > 0:
                    time.sleep(wait)
                _LAST_SEC_REQUEST = time.monotonic()
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(
                    request, timeout=_SEC_TIMEOUT_SECONDS
                ) as response:
                    raw = response.read()
                    content_type = response.headers.get("Content-Type", "")
                    charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace"), content_type
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))

    raise RuntimeError(f"SEC request failed for {url}: {last_error}")


def _fetch_text(url: str, kind: str, refresh: bool = False) -> str:
    """Fetch and cache a decoded raw SEC response under a versioned key."""
    key = _raw_cache_key(kind, url)
    if not refresh:
        cached = cache_get(key)
        payload = cached.get("data") if isinstance(cached, dict) else None
        if (
            isinstance(payload, dict)
            and payload.get("version") in {_RAW_CACHE_VERSION, _CACHE_VERSION}
            and payload.get("url") == url
            and isinstance(payload.get("body"), str)
        ):
            return payload["body"]

    body, content_type = _request_text(url)
    cache_set(
        key,
        {
            "version": _RAW_CACHE_VERSION,
            "url": url,
            "content_type": content_type,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "request_identity": sec_request_identity(),
            "body": body,
        },
    )
    return body


def _fetch_json(url: str, kind: str, refresh: bool = False) -> dict[str, Any]:
    text = _fetch_text(url, kind, refresh=refresh)
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"SEC returned invalid JSON for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"SEC returned a non-object JSON response for {url}")
    return payload


def fetch_companyfacts(cik: str | int, refresh: bool = False) -> dict[str, Any]:
    """Fetch one SEC companyfacts document."""
    cik10 = _normalise_cik(cik)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
    payload = _fetch_json(url, f"companyfacts_{cik10}", refresh=refresh)
    if not isinstance(payload.get("facts"), dict):
        raise RuntimeError(f"Malformed SEC companyfacts response for CIK {cik10}")
    return payload


def fetch_submissions(cik: str | int, refresh: bool = False) -> dict[str, Any]:
    """Fetch one SEC submissions document."""
    cik10 = _normalise_cik(cik)
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    payload = _fetch_json(url, f"submissions_{cik10}", refresh=refresh)
    if not isinstance(payload.get("filings"), dict):
        raise RuntimeError(f"Malformed SEC submissions response for CIK {cik10}")
    return payload


def extract_duration_facts(
    companyfacts: Mapping[str, Any],
    taxonomy: str,
    tag: str,
    unit: str,
    source_url: str | None = None,
    cik: str | int | None = None,
) -> list[dict[str, Any]]:
    """Extract duration facts while retaining SEC factual metadata."""
    facts_root = companyfacts.get("facts")
    if not isinstance(facts_root, Mapping):
        raise ValueError("companyfacts has no facts object")
    taxonomy_root = facts_root.get(taxonomy)
    if not isinstance(taxonomy_root, Mapping):
        return []
    concept = taxonomy_root.get(tag)
    if not isinstance(concept, Mapping):
        return []
    units = concept.get("units")
    if not isinstance(units, Mapping):
        raise ValueError(f"Malformed units for {taxonomy}:{tag}")
    entries = units.get(unit)
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError(f"Malformed {unit} facts for {taxonomy}:{tag}")

    cik10 = _normalise_cik(cik) if cik is not None else None
    if source_url is None and cik10 is not None:
        source_url = (
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
        )
    result: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not entry.get("start") or not entry.get("end"):
            continue
        row = {name: entry.get(name) for name in _FACT_METADATA}
        row.update(
            {
                "unit": unit,
                "taxonomy": taxonomy,
                "tag": tag,
                "source_url": source_url,
            }
        )
        if cik10 is not None:
            row["cik"] = cik10
        result.append(row)
    return result


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _plain_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _fact_sort_key(fact: Mapping[str, Any]) -> tuple[str, int, str, str]:
    filed = fact.get("filed")
    accession = fact.get("accn")
    form = fact.get("form")
    try:
        tag_index = int(fact.get("_tag_index", 0))
    except (TypeError, ValueError):
        tag_index = 0
    return (
        filed if isinstance(filed, str) and _as_date(filed) else "9999-12-31",
        tag_index,
        str(accession or ""),
        str(form or ""),
    )


def _fact_identity(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fact.get("cik"),
        fact.get("taxonomy"),
        fact.get("_metric_group", fact.get("tag")),
        fact.get("unit"),
    )


def _prepare_duration_facts(
    facts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    selected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for supplied in facts:
        if not isinstance(supplied, Mapping):
            continue
        form = str(supplied.get("form") or "").upper()
        start = _as_date(supplied.get("start"))
        end = _as_date(supplied.get("end"))
        value = _as_decimal(supplied.get("val"))
        if not _FORM_RE.fullmatch(form) or not start or not end or end < start:
            continue
        if value is None:
            continue
        fact = dict(supplied)
        fact["form"] = form
        fact["_start_date"] = start
        fact["_end_date"] = end
        fact["_decimal_value"] = value
        key = _fact_identity(fact) + (start, end)
        current = selected.get(key)
        if current is None or _fact_sort_key(fact) < _fact_sort_key(current):
            selected[key] = fact
    return list(selected.values())


def calendar_quarter_for_period(period_start: str, period_end: str) -> str:
    """Map a period to the calendar quarter containing its midpoint."""
    start = _as_date(period_start)
    end = _as_date(period_end)
    if not start or not end or end < start:
        raise ValueError(f"Invalid period: {period_start!r} to {period_end!r}")
    midpoint = start + timedelta(days=(end - start).days // 2)
    return f"{midpoint.year}Q{((midpoint.month - 1) // 3) + 1}"


def _component(fact: Mapping[str, Any], role: str) -> dict[str, Any]:
    component = {name: fact.get(name) for name in _FACT_METADATA}
    if fact.get("cik") is not None:
        component["cik"] = fact.get("cik")
    component["role"] = role
    return component


def _unique_present(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value is not None and value not in result:
            result.append(value)
    return result


def _quarter_record(
    period_start: date,
    period_end: date,
    value: Decimal,
    derivation: str,
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    facts = [current] if previous is None else [current, previous]
    roles = ["reported"] if previous is None else ["minuend", "subtrahend"]
    components = [_component(fact, role) for fact, role in zip(facts, roles)]
    filings = [fact.get("filed") for fact in facts]
    valid_filings = [item for item in filings if isinstance(item, str) and _as_date(item)]
    filed = max(valid_filings) if valid_filings else None
    starts = period_start.isoformat()
    ends = period_end.isoformat()
    number = _plain_number(value)
    accessions = [fact.get("accn") for fact in facts]
    tags = [fact.get("tag") for fact in facts]
    urls = [fact.get("source_url") for fact in facts]

    fp = current.get("fp")
    if derivation == "fy_difference":
        fiscal_quarter = "Q4"
    elif isinstance(fp, str) and fp.upper() in {"Q1", "Q2", "Q3", "Q4"}:
        fiscal_quarter = fp.upper()
    elif str(fp or "").upper() == "FY" and 70 <= (period_end - period_start).days + 1 <= 110:
        fiscal_quarter = "Q4"
    else:
        fiscal_quarter = None

    return {
        "calendar_quarter": calendar_quarter_for_period(starts, ends),
        "period_start": starts,
        "period_end": ends,
        "start": starts,
        "end": ends,
        "value": number,
        "val": number,
        "unit": current.get("unit"),
        "filed": filed,
        "accn": current.get("accn"),
        "fy": current.get("fy"),
        "fp": current.get("fp"),
        "fiscal_quarter": fiscal_quarter,
        "form": current.get("form"),
        "frame": current.get("frame"),
        "taxonomy": current.get("taxonomy"),
        "tag": current.get("tag"),
        "cik": current.get("cik"),
        "source_url": current.get("source_url"),
        "source_urls": _unique_present(urls),
        "source_accessions": _unique_present(accessions),
        "source_tags": _unique_present(tags),
        "component_accessions": accessions,
        "component_filing_dates": filings,
        "component_tags": tags,
        "components": components,
        "derivation": derivation,
        "status": "reported" if derivation == "reported_quarter" else "derived",
    }


def _difference_kind(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> str | None:
    if _fact_identity(previous) != _fact_identity(current):
        return None
    if previous.get("start") != current.get("start"):
        return None
    previous_fy = previous.get("fy")
    current_fy = current.get("fy")
    if previous_fy is not None and current_fy is not None and previous_fy != current_fy:
        return None

    pstart = previous["_start_date"]
    pend = previous["_end_date"]
    cstart = current["_start_date"]
    cend = current["_end_date"]
    if pstart != cstart or pend >= cend:
        return None
    quarter_days = (cend - pend).days
    previous_days = (pend - pstart).days + 1
    current_days = (cend - cstart).days + 1
    if not 70 <= quarter_days <= 110:
        return None

    pfp = str(previous.get("fp") or "").upper()
    cfp = str(current.get("fp") or "").upper()
    current_form = str(current.get("form") or "").upper().replace("/A", "")

    if current_form == "10-K" or cfp == "FY":
        if current_form != "10-K" or not (330 <= current_days <= 390):
            return None
        if not 220 <= previous_days <= 330 or (pfp and pfp != "Q3"):
            return None
        return "fy_difference"

    if current_form != "10-Q":
        return None
    if cfp == "Q2":
        if pfp and pfp != "Q1":
            return None
        return "ytd_difference" if 140 <= current_days <= 220 else None
    if cfp == "Q3":
        if pfp and pfp != "Q2":
            return None
        return "ytd_difference" if 220 <= current_days <= 330 else None
    if cfp:
        return None

    if 70 <= previous_days <= 110 and 140 <= current_days <= 220:
        return "ytd_difference"
    if 140 <= previous_days <= 220 and 220 <= current_days <= 330:
        return "ytd_difference"
    return None


def duration_facts_to_standalone_quarters(
    facts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert duration XBRL facts to reported or arithmetically derived quarters.

    Q2 and Q3 may be derived from adjacent YTD facts, and Q4 may be derived
    from FY less Q3 YTD. Earliest effective filing availability wins; a direct
    period is preferred over a derived period only when filing dates tie. No
    period is interpolated.
    """
    prepared = _prepare_duration_facts(facts)
    candidates: list[dict[str, Any]] = []

    for fact in prepared:
        days = (fact["_end_date"] - fact["_start_date"]).days + 1
        if 70 <= days <= 110:
            candidates.append(
                _quarter_record(
                    fact["_start_date"],
                    fact["_end_date"],
                    fact["_decimal_value"],
                    "reported_quarter",
                    fact,
                )
            )

    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for fact in prepared:
        grouped.setdefault(_fact_identity(fact) + (fact.get("start"),), []).append(fact)

    for group in grouped.values():
        ordered = sorted(
            group,
            key=lambda item: (item["_end_date"], _fact_sort_key(item)),
        )
        grouped_by_end: dict[date, list[dict[str, Any]]] = {}
        for fact in ordered:
            grouped_by_end.setdefault(fact["_end_date"], []).append(fact)
        end_representatives = [
            min(end_group, key=_fact_sort_key)
            for end_group in grouped_by_end.values()
        ]
        for current in end_representatives:
            possible: list[tuple[dict[str, Any], str]] = []
            for previous in end_representatives:
                kind = _difference_kind(previous, current)
                if kind:
                    possible.append((previous, kind))
            if not possible:
                continue
            previous, kind = max(
                possible,
                key=lambda pair: pair[0]["_end_date"],
            )
            value = current["_decimal_value"] - previous["_decimal_value"]
            if value < 0:
                continue
            candidates.append(
                _quarter_record(
                    previous["_end_date"] + timedelta(days=1),
                    current["_end_date"],
                    value,
                    kind,
                    current,
                    previous,
                )
            )

    # Preserve the earliest historical vintage; direct reporting is only a
    # tie-breaker when the effective filing dates are identical.
    selected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in candidates:
        key = (
            row.get("cik"),
            row.get("taxonomy"),
            row.get("tag"),
            row["period_start"],
            row["period_end"],
            row["calendar_quarter"],
            row.get("unit"),
        )
        rank = (
            row.get("filed") or "9999-12-31",
            0 if row["derivation"] == "reported_quarter" else 1,
        )
        old = selected.get(key)
        old_rank = (
            (old or {}).get("filed") or "9999-12-31",
            0 if old and old["derivation"] == "reported_quarter" else 1,
        )
        if old is None or rank < old_rank:
            selected[key] = row
    return sorted(
        selected.values(),
        key=lambda row: (row["period_end"], row.get("filed") or ""),
    )


# Short alias retained for callers that do not need the longer descriptive name.
duration_facts_to_quarters = duration_facts_to_standalone_quarters


def _priority_rank(value: Any, default: int) -> tuple[int, Any]:
    if value is None:
        return (0, float(default))
    if isinstance(value, bool):
        return (0, float(int(value)))
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return (0, float(value))
    try:
        return (0, float(str(value)))
    except (TypeError, ValueError):
        return (1, str(value))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def deduplicate_quarters(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Choose one record per calendar quarter using locked vintage precedence."""
    selected: dict[str, dict[str, Any]] = {}
    for supplied in records:
        quarter = supplied.get("calendar_quarter")
        if not isinstance(quarter, str) or not _QUARTER_RE.fullmatch(quarter):
            continue
        row = dict(supplied)
        source_index = _safe_int(row.get("_source_index"))
        tag_index = _safe_int(row.get("tag_priority", row.get("_tag_index")))
        rank = (
            row.get("filed") or "9999-12-31",
            _priority_rank(row.get("source_priority"), source_index),
            tag_index,
            0 if row.get("derivation") == "reported_quarter" else 1,
            source_index,
        )
        old = selected.get(quarter)
        if old is not None:
            old_source_index = _safe_int(old.get("_source_index"))
            old_rank = (
                old.get("filed") or "9999-12-31",
                _priority_rank(
                    old.get("source_priority"), old_source_index
                ),
                _safe_int(old.get("tag_priority", old.get("_tag_index"))),
                0 if old.get("derivation") == "reported_quarter" else 1,
                old_source_index,
            )
            if rank >= old_rank:
                continue
        selected[quarter] = row

    result: list[dict[str, Any]] = []
    for quarter in sorted(selected, key=_quarter_sort_key):
        row = selected[quarter]
        row.pop("_source_index", None)
        row.pop("_tag_index", None)
        result.append(row)
    return result


def _quarter_sort_key(quarter: str) -> tuple[int, int]:
    match = _QUARTER_RE.fullmatch(quarter)
    if not match:
        raise ValueError(f"Invalid calendar quarter: {quarter!r}")
    return int(match.group(1)), int(match.group(2))


def fetch_us_quarterly_metric(
    sources: list[dict[str, Any]],
    start_quarter: str = "2013Q1",
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Fetch and merge configured U.S. companyfacts concepts by calendar quarter.

    Earliest effective filing availability wins, followed by lower configured
    source ``priority`` and tag order. Direct reporting is only a later
    tie-breaker, so a contemporaneous derived row beats a delayed comparative
    direct row. Filing dates remain attached to every selected row.
    """
    start_key = _quarter_sort_key(start_quarter)
    if not isinstance(sources, list):
        raise TypeError("sources must be a list of dictionaries")

    companyfacts_by_cik: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for source_index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise TypeError("each SEC source must be a dictionary")
        if "cik" not in source:
            raise ValueError("each SEC source requires a cik")
        cik10 = _normalise_cik(source["cik"])
        tags = source.get("tags")
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise ValueError(f"SEC source {cik10} requires an ordered tags list")
        taxonomy = str(source.get("taxonomy", "us-gaap"))
        unit = str(source.get("unit", "USD"))
        source_priority = source.get("priority", source_index)

        if cik10 not in companyfacts_by_cik:
            companyfacts_by_cik[cik10] = fetch_companyfacts(cik10, refresh=refresh)
        payload = companyfacts_by_cik[cik10]
        source_url = (
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
        )

        source_facts: list[dict[str, Any]] = []
        metric_group = f"source:{source_index}:{cik10}:{taxonomy}:{unit}"
        for tag_index, tag in enumerate(tags):
            try:
                facts = extract_duration_facts(
                    payload,
                    taxonomy,
                    tag,
                    unit,
                    source_url=source_url,
                    cik=cik10,
                )
            except ValueError as exc:
                raise RuntimeError(
                    f"Malformed SEC concept data for CIK {cik10} "
                    f"{taxonomy}:{tag}/{unit}: {exc}"
                ) from exc
            if not facts:
                continue
            for fact in facts:
                fact["_metric_group"] = metric_group
                fact["_tag_index"] = tag_index
            source_facts.extend(facts)
        for row in duration_facts_to_standalone_quarters(source_facts):
            component_priorities = [
                tags.index(component_tag)
                for component_tag in row.get("component_tags") or []
                if component_tag in tags
            ]
            row["source_priority"] = source_priority
            row["tag_priority"] = min(component_priorities, default=len(tags))
            row["_source_index"] = source_index
            row["_tag_index"] = row["tag_priority"]
            candidates.append(row)

    rows = deduplicate_quarters(candidates)
    return [row for row in rows if _quarter_sort_key(row["calendar_quarter"]) >= start_key]


class _SecTableParser(HTMLParser):
    """Collect visible document text and table rows from filing HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.rows: list[list[str]] = []
        self._row_depth = 0
        self._cell_depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        tag = tag.lower()
        if tag == "tr":
            if self._row_depth == 0:
                self._row = []
            self._row_depth += 1
        elif tag in {"td", "th"} and self._row_depth:
            if self._cell_depth == 0:
                self._cell = []
            self._cell_depth += 1
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell_depth:
            self._cell_depth -= 1
            if self._cell_depth == 0 and self._row is not None:
                self._row.append(" ".join("".join(self._cell or []).split()))
                self._cell = None
        elif tag == "tr" and self._row_depth:
            self._row_depth -= 1
            if self._row_depth == 0 and self._row is not None:
                if any(self._row):
                    self.rows.append(self._row)
                self._row = None

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)


def _normalised_words(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _large_numbers(value: str) -> list[Decimal]:
    numbers: list[Decimal] = []
    for match in re.finditer(r"\(?\s*\d[\d,]*(?:\.\d+)?\s*\)?", value):
        token = match.group(0).strip()
        negative = token.startswith("(") and token.endswith(")")
        token = token.strip("() ").replace(",", "")
        try:
            number = Decimal(token)
        except InvalidOperation:
            continue
        if negative:
            number = -number
        if number > 100:
            numbers.append(number)
    return numbers


def _parse_asml_flat_statement_text(
    visible_text: str, expected_quarter: str | None
) -> int | None:
    """Parse newer ASML slide exhibits whose statements are plain text."""
    expected_year = _quarter_sort_key(expected_quarter)[0] if expected_quarter else None
    text = " ".join(visible_text.replace("\xa0", " ").split())
    lower = text.lower()
    if "summary us gaap consolidated statements of operations" not in lower:
        return None
    if "three months ended" not in lower or "in millions" not in lower:
        return None
    if not any(marker in lower for marker in ("eur", "euro", "€")):
        return None

    # The first statement presents the current and comparative three-month
    # columns before any YTD columns. Newer exhibits are image-based slides,
    # but SEC's HTML retains the slide text in reading order.
    statement = re.search(
        r"three\s+months\s+ended\s+(.*?)\bnet\s+system\s+sales\b"
        r".*?\btotal\s+net\s+sales\b\s+(.*?)\btotal\s+cost\s+of\s+sales\b",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not statement:
        return None
    header, value_text = statement.groups()
    years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", header)]
    values = _large_numbers(value_text)
    comparable = min(2, len(years), len(values))
    if comparable == 0:
        return None
    if expected_year is None:
        column = max(range(comparable), key=lambda index: years[index])
    elif expected_year in years[:comparable]:
        column = years[:comparable].index(expected_year)
    else:
        return None
    euros = values[column] * Decimal("1000000")
    return int(euros) if euros == euros.to_integral_value() else None


def parse_asml_gaap_quarterly_revenue(
    html_text: str, expected_quarter: str | None = None
) -> int | None:
    """Parse current-quarter Total net sales from an ASML U.S. GAAP exhibit."""
    expected_year = _quarter_sort_key(expected_quarter)[0] if expected_quarter else None
    parser = _SecTableParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:  # HTMLParser can surface malformed entity errors.
        raise ValueError(f"Malformed SEC exhibit HTML: {exc}") from exc

    visible_text = " ".join(parser.text_parts)
    document = _normalised_words(visible_text)
    title = re.search(
        r"summary (?:u s|us) gaap consolidated statements? of operations",
        document,
    )
    if not title or "three months ended" not in document:
        return None
    if "in millions" not in document or not (
        "eur" in document or "euro" in document or "\u20ac" in visible_text
    ):
        return None

    for row_index, row in enumerate(parser.rows):
        for index, cell in enumerate(row):
            words = _normalised_words(cell)
            if not re.search(r"\btotal net sales\b", words):
                continue
            local_headers = parser.rows[max(0, row_index - 12) : row_index]
            local_text = " ".join(" ".join(header) for header in local_headers)
            local_words = _normalised_words(local_text)
            if "three months ended" not in local_words:
                continue
            if "in millions" not in local_words or not (
                "eur" in local_words
                or "euro" in local_words
                or "\u20ac" in local_text
            ):
                continue
            value_text = " ".join(
                [
                    re.split(r"total net sales", cell, flags=re.IGNORECASE)[-1],
                    *row[index + 1 :],
                ]
            )
            numbers = _large_numbers(value_text)
            if not numbers:
                continue

            header_years: list[int] = []
            for header in reversed(local_headers):
                years = [
                    int(year)
                    for year in re.findall(r"\b(?:19|20)\d{2}\b", " ".join(header))
                ]
                if len(years) >= 2:
                    header_years = years
                    break

            if expected_year is not None and not header_years:
                return None

            column = 0
            comparable = min(2, len(numbers), len(header_years))
            if comparable:
                if expected_year is not None and expected_year in header_years[:comparable]:
                    column = header_years[:comparable].index(expected_year)
                elif expected_year is not None:
                    return None
                else:
                    column = max(
                        range(comparable), key=lambda item: header_years[item]
                    )
            euros = numbers[column] * Decimal("1000000")
            return int(euros) if euros == euros.to_integral_value() else None
    return _parse_asml_flat_statement_text(visible_text, expected_quarter)


def _submission_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    recent: Any = payload
    filings = payload.get("filings")
    if isinstance(filings, Mapping):
        recent = filings.get("recent")
    if not isinstance(recent, Mapping):
        return []
    accessions = recent.get("accessionNumber")
    if not isinstance(accessions, list):
        return []
    fields = (
        "accessionNumber",
        "filingDate",
        "reportDate",
        "form",
        "primaryDocument",
        "primaryDocDescription",
    )
    result: list[dict[str, Any]] = []
    for index in range(len(accessions)):
        row: dict[str, Any] = {}
        for field in fields:
            values = recent.get(field)
            row[field] = values[index] if isinstance(values, list) and index < len(values) else None
        result.append(row)
    return result


def _all_submission_rows(
    submissions: Mapping[str, Any], refresh: bool
) -> list[dict[str, Any]]:
    rows = _submission_rows(submissions)
    filings = submissions.get("filings")
    history = filings.get("files") if isinstance(filings, Mapping) else None
    if history is not None and not isinstance(history, list):
        raise RuntimeError("Malformed SEC submissions history file list")
    for item in history or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            continue
        name = item["name"]
        url = f"https://data.sec.gov/submissions/{urllib.parse.quote(name)}"
        payload = _fetch_json(url, f"submissions_history_{_ASML_CIK}", refresh=refresh)
        rows.extend(_submission_rows(payload))
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        accession = row.get("accessionNumber")
        if isinstance(accession, str):
            unique.setdefault(accession, row)
    return list(unique.values())


def _most_recent_completed_quarter(filing_date: date) -> str:
    completed = (filing_date.month - 1) // 3
    if completed == 0:
        return f"{filing_date.year - 1}Q4"
    return f"{filing_date.year}Q{completed}"


def _quarter_bounds(quarter: str) -> tuple[str, str]:
    year, number = _quarter_sort_key(quarter)
    start_month = (number - 1) * 3 + 1
    start = date(year, start_month, 1)
    if number == 4:
        after = date(year + 1, 1, 1)
    else:
        after = date(year, start_month + 3, 1)
    return start.isoformat(), (after - timedelta(days=1)).isoformat()


def _filing_likelihood(row: Mapping[str, Any]) -> tuple[int, int, str]:
    primary = str(row.get("primaryDocument") or "").lower()
    filed = _as_date(row.get("filingDate"))
    if "quarter" in primary or re.search(r"q[1-4].*result", primary):
        score = 0
    elif filed and 14 <= filed.day <= 29:
        score = 1
    else:
        score = 2
    return score, filed.day if filed else 99, str(row.get("accessionNumber") or "")


def _asml_exhibit_names(
    index_payload: Mapping[str, Any], primary_document: str
) -> list[str]:
    directory = index_payload.get("directory")
    items = directory.get("item") if isinstance(directory, Mapping) else None
    if not isinstance(items, list):
        raise RuntimeError("Malformed SEC filing archive index")
    ranked: list[tuple[tuple[int, int, str], str]] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            continue
        name = item["name"]
        lower = name.lower()
        if not lower.endswith((".htm", ".html")) or "-index" in lower:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if "financialstatement" in lower and ("usgaap" in lower or "gaap" in lower):
            category = 0
        elif "usgaap" in lower:
            category = 1
        elif "ex99" in lower and size >= 150000:
            category = 2
        elif "ex99" in lower:
            category = 3
        elif "financial" in lower:
            category = 4
        elif lower == primary_document.lower():
            category = 7
        elif "presentation" in lower or "pressrelease" in lower:
            category = 6
        else:
            category = 5
        ranked.append(((category, -size, lower), name))
    return [name for _, name in sorted(ranked)]


def _asml_record(
    quarter: str,
    value: int,
    accession: str,
    filed: str,
    source_url: str,
) -> dict[str, Any]:
    period_start, period_end = _quarter_bounds(quarter)
    return {
        "calendar_quarter": quarter,
        "period_start": period_start,
        "period_end": period_end,
        "start": period_start,
        "end": period_end,
        "value": value,
        "val": value,
        "unit": "EUR",
        "filed": filed,
        "accn": accession,
        "form": "6-K",
        "taxonomy": "U.S. GAAP exhibit",
        "tag": "Total net sales",
        "cik": _ASML_CIK,
        "source_url": source_url,
        "source_urls": [source_url],
        "source_accessions": [accession],
        "source_tags": ["Total net sales"],
        "component_accessions": [accession],
        "component_filing_dates": [filed],
        "component_tags": ["Total net sales"],
        "components": [
            {
                "role": "reported",
                "accn": accession,
                "filed": filed,
                "form": "6-K",
                "tag": "Total net sales",
                "unit": "EUR millions",
                "source_url": source_url,
            }
        ],
        "derivation": "reported_quarter",
        "status": "reported",
    }


def fetch_asml_quarterly_revenue(
    start_quarter: str = "2013Q1", refresh: bool = False
) -> list[dict[str, Any]]:
    """Fetch ASML quarterly U.S. GAAP Total net sales from SEC 6-K exhibits."""
    start_key = _quarter_sort_key(start_quarter)
    if not refresh:
        cached = cache_get(_ASML_CACHE_KEY)
        payload = cached.get("data") if isinstance(cached, dict) else None
        if (
            isinstance(payload, dict)
            and payload.get("version") == _CACHE_VERSION
            and isinstance(payload.get("rows"), list)
        ):
            return [
                dict(row)
                for row in payload["rows"]
                if isinstance(row, Mapping)
                and _quarter_sort_key(str(row.get("calendar_quarter"))) >= start_key
            ]

    submissions = fetch_submissions(_ASML_CIK, refresh=refresh)
    filings_by_quarter: dict[str, list[dict[str, Any]]] = {}
    for row in _all_submission_rows(submissions, refresh=refresh):
        form = str(row.get("form") or "").upper()
        filed = _as_date(row.get("filingDate"))
        accession = row.get("accessionNumber")
        if form not in {"6-K", "6-K/A"} or not filed or not isinstance(accession, str):
            continue
        if filed.year < 2013 or filed.month not in {1, 4, 7, 10}:
            continue
        quarter = _most_recent_completed_quarter(filed)
        if _quarter_sort_key(quarter) < (2013, 1):
            continue
        filings_by_quarter.setdefault(quarter, []).append(row)

    parsed: dict[str, dict[str, Any]] = {}
    for quarter, filing_rows in filings_by_quarter.items():
        for filing in sorted(filing_rows, key=_filing_likelihood):
            accession = str(filing["accessionNumber"])
            compact_accession = accession.replace("-", "")
            base_url = (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{_ASML_ARCHIVE_CIK}/{compact_accession}"
            )
            index_url = f"{base_url}/index.json"
            archive_index = _fetch_json(
                index_url, f"asml_filing_index_{compact_accession}", refresh=refresh
            )
            primary = str(filing.get("primaryDocument") or "")
            for name in _asml_exhibit_names(archive_index, primary):
                source_url = f"{base_url}/{urllib.parse.quote(name)}"
                exhibit = _fetch_text(
                    source_url,
                    f"asml_exhibit_{compact_accession}_{name}",
                    refresh=refresh,
                )
                try:
                    value = parse_asml_gaap_quarterly_revenue(
                        exhibit, expected_quarter=quarter
                    )
                except ValueError:
                    value = None
                if value is None:
                    continue
                candidate = _asml_record(
                    quarter,
                    value,
                    accession,
                    str(filing["filingDate"]),
                    source_url,
                )
                old = parsed.get(quarter)
                if old is None or (
                    candidate["filed"], candidate["accn"]
                ) < (old["filed"], old["accn"]):
                    parsed[quarter] = candidate
                break
            if quarter in parsed:
                break

    rows = [parsed[quarter] for quarter in sorted(parsed, key=_quarter_sort_key)]
    if not rows:
        raise RuntimeError(
            "SEC submissions were available, but no ASML quarterly U.S. GAAP "
            "Total net sales exhibits could be parsed"
        )
    cache_set(
        _ASML_CACHE_KEY,
        {
            "version": _CACHE_VERSION,
            "normalised_at": datetime.now(timezone.utc).isoformat(),
            "request_identity": sec_request_identity(),
            "rows": rows,
        },
    )
    return [row for row in rows if _quarter_sort_key(row["calendar_quarter"]) >= start_key]
