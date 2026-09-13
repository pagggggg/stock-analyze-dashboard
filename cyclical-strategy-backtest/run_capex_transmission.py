#!/usr/bin/env python3
"""Run the preregistered CAPEX-to-revenue transmission research pipeline."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

import capex_transmission as core
import sources_finmind
import sources_sec


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "capex_transmission_config.yaml"

AMAZON_CAPEX_TAG = "PaymentsToAcquireProductiveAssets"
AMAZON_CAPEX_LABEL = (
    "Amazon gross productive-assets cash purchases, including PP&E, "
    "internal-use software, and other intangibles"
)
AMAZON_FIRST_STANDALONE_QUARTER = "2017Q3"
AMAZON_EXCLUDED_LEGACY_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsForProceedsFromProductiveAssets",
    "CapitalExpenditures",
]
AMAZON_EXCLUDED_LEGACY_REASON = (
    "Semantically different legacy, net, and generic CAPEX tags are excluded; "
    "they are not coalesced with, used to reconstruct, or substituted for "
    "PaymentsToAcquireProductiveAssets."
)
FINMIND_KNOWN_NON_STANDALONE_EXCLUSIONS = {
    "3711": {"2018Q2": "FinMind selected six-month cumulative value"},
    "6488": {
        "2014Q2": "H1 cumulative",
        "2014Q4": "H2 aggregate",
    },
}
VINTAGE_SELECTION_ORDER = [
    "earliest_effective_filing_availability",
    "configured_source_priority",
    "configured_tag_priority",
    "reported_quarter_over_derived_tie_breaker",
]
INDEPENDENT_REVIEW_CHANGELOG_NOTE = (
    "Independent-review corrections lock source-semantic exclusions, "
    "historical-vintage handling, and gap-aware audit disclosure; they are not "
    "outcome-driven."
)

CAPEX_HISTORY_FIELDS = [
    "company",
    "company_name",
    "calendar_quarter",
    "analysis_role",
    "period_start",
    "period_end",
    "currency",
    "ppe_capex_usd",
    "finance_lease_principal_usd",
    "total_capex_usd",
    "status",
    "missing_reason",
    "derivation",
    "ppe_source_tags",
    "ppe_source_accessions",
    "ppe_filing_dates",
    "ppe_source_urls",
    "finance_lease_source_tags",
    "finance_lease_source_accessions",
    "finance_lease_filing_dates",
    "finance_lease_source_urls",
    "source_tags",
    "source_accessions",
    "filing_dates",
    "filed",
    "source_urls",
    "notes",
]

REVENUE_AUDIT_FIELDS = [
    "row_type",
    "layer_id",
    "layer_name",
    "is_control",
    "member",
    "calendar_quarter",
    "analysis_role",
    "base_quarter",
    "period_start",
    "period_end",
    "original_currency",
    "original_value",
    "excluded_source_value",
    "fixed_twd_per_usd",
    "fixed_usd_per_eur",
    "fixed_usd_value",
    "layer_fixed_usd_total",
    "layer_yoy_pct",
    "status",
    "missing_reason",
    "missing_members",
    "source",
    "source_tags",
    "source_accessions",
    "filing_dates",
    "filed",
    "filing_metadata_status",
    "source_urls",
    "derivation",
    "notes",
]

LAG_FIELDS = [
    "mode",
    "window",
    "layer",
    "layer_name",
    "lag",
    "n",
    "segment_count",
    "segment_lengths",
    "segment_boundaries",
    "r",
    "pearson_p",
    "pearson_p_status",
    "bootstrap_p",
    "bootstrap_ci",
    "holm_p",
    "inferential_eligible",
    "support_status",
    "ess",
    "ess_status",
    "paired_capex_range",
    "paired_revenue_range",
]

SOURCE_FAILURES = (RuntimeError, ImportError, OSError)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Independent CAPEX transmission research; not a trading strategy."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="refresh SEC sources; FinMind retains its source-module cache policy",
    )
    parser.add_argument(
        "--draws",
        type=nonnegative_int,
        default=None,
        metavar="N",
        help="bootstrap draws (default: the preregistered YAML value, 99999)",
    )
    parser.add_argument(
        "--output-dir",
        default="reports",
        help="output directory, relative to this script unless absolute (default: reports)",
    )
    return parser.parse_args(argv)


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ValueError("CAPEX transmission config must be a YAML mapping")
    validate_config(loaded)
    return loaded


def validate_config(config: Mapping[str, Any]) -> None:
    prereg = require_mapping(config, "preregistration")
    if (
        prereg.get("locked") is not True
        or prereg.get("guidance_data") != "prohibited"
        or prereg.get("no_imputation") is not True
        or prereg.get("scope") != "independent_research_not_trading_strategy"
    ):
        raise ValueError(
            "preregistration must be locked, non-trading research with no "
            "guidance or imputation"
        )

    vintage = require_mapping(config, "vintage_policy")
    if (
        vintage.get("basis")
        != "historical_actual_research_not_point_in_time_trading"
        or list(vintage.get("selection_order") or []) != VINTAGE_SELECTION_ORDER
        or vintage.get("later_comparative_disclosures")
        != "allowed_only_when_no_earlier_standalone_quarter_candidate_exists"
        or vintage.get("filing_date_policy")
        != "expose_selected_and_component_filing_dates"
    ):
        raise ValueError("locked historical-vintage policy changed")

    periods = require_mapping(config, "periods")
    expected_periods = {
        "fetch_start": "2013Q1",
        "output_start": "2014Q1",
    }
    for key, expected in expected_periods.items():
        if str(periods.get(key)) != expected:
            raise ValueError(f"locked period {key} must be {expected}")
    for name, expected in {
        "early": ("2014Q1", "2019Q4"),
        "late": ("2020Q1", "analysis_endpoint"),
    }.items():
        window = require_mapping(periods, name)
        if (str(window.get("start")), str(window.get("end"))) != expected:
            raise ValueError(f"locked {name} window is invalid")
    full = require_mapping(periods, "full")
    if (str(full.get("start")), str(full.get("end"))) != (
        "2014Q1",
        "analysis_endpoint",
    ):
        raise ValueError("locked full window is invalid")
    endpoint_rule = require_mapping(periods, "analysis_endpoint")
    if endpoint_rule.get("rule") != (
        "latest_completed_calendar_quarter_capped_by_last_quarter_with_all_four_capex_totals"
    ):
        raise ValueError("analysis endpoint rule changed")

    fx = require_mapping(config, "fixed_fx")
    if str(fx.get("period")) != "2014Q1":
        raise ValueError("fixed FX period must be 2014Q1")
    twd = require_mapping(fx, "twd_per_usd")
    eur = require_mapping(fx, "usd_per_eur")
    if float(twd.get("value")) != 30.2796721311 or int(twd.get("observations")) != 61:
        raise ValueError("locked TWD/USD fixed FX input is invalid")
    if float(eur.get("value")) != 1.3705049180 or int(eur.get("observations")) != 61:
        raise ValueError("locked USD/EUR fixed FX input is invalid")
    if not twd.get("source_url") or not eur.get("source_url"):
        raise ValueError("fixed FX source URLs are required")

    capex = require_mapping(config, "capex")
    if list(capex.get("frozen_companies") or []) != ["MSFT", "GOOGL", "AMZN", "META"]:
        raise ValueError("CAPEX universe must be exactly MSFT, GOOGL, AMZN, META")
    capex_definition = require_mapping(capex, "definition")
    if (
        capex_definition.get("default_total")
        != "MSFT and GOOGL PPE cash CAPEX"
        or capex_definition.get("amazon_total") != AMAZON_CAPEX_LABEL
        or capex_definition.get("meta_total")
        != "PPE cash CAPEX plus financing-lease principal, only when both components exist"
        or capex_definition.get("missing_meta_lease") != "missing, never zero"
        or capex_definition.get("standalone_quarters")
        != "reported quarter or mechanically derived from adjacent YTD/FY facts"
        or capex_definition.get("sign_policy")
        != "absolute cash-outflow magnitude"
    ):
        raise ValueError("locked company CAPEX definitions changed")
    companies = require_mapping(capex, "companies")
    if set(companies) != {"MSFT", "GOOGL", "AMZN", "META"}:
        raise ValueError("CAPEX company configurations do not match the frozen universe")
    for company, details in companies.items():
        company_config = mapping_value(details, f"capex.companies.{company}")
        validate_sec_sources(company_config.get("ppe_sources"), f"{company}.ppe_sources")
        if company == "META":
            validate_sec_sources(
                company_config.get("finance_lease_sources"),
                "META.finance_lease_sources",
            )
    amazon = mapping_value(companies["AMZN"], "capex.companies.AMZN")
    amazon_sources = amazon.get("ppe_sources")
    if not isinstance(amazon_sources, list) or len(amazon_sources) != 1:
        raise ValueError("AMZN must have exactly one primary SEC source")
    amazon_source = mapping_value(amazon_sources[0], "AMZN.ppe_sources[0]")
    expected_amazon_source_url = (
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0001018724.json"
    )
    if (
        amazon_source.get("cik") != "0001018724"
        or amazon_source.get("taxonomy") != "us-gaap"
        or amazon_source.get("unit") != "USD"
        or amazon_source.get("priority") != 0
        or list(amazon_source.get("tags") or []) != [AMAZON_CAPEX_TAG]
        or amazon_source.get("source_url") != expected_amazon_source_url
        or amazon.get("metric_label") != AMAZON_CAPEX_LABEL
        or str(amazon.get("first_standalone_quarter"))
        != AMAZON_FIRST_STANDALONE_QUARTER
        or list(amazon.get("excluded_legacy_tags") or [])
        != AMAZON_EXCLUDED_LEGACY_TAGS
        or amazon.get("excluded_legacy_reason")
        != AMAZON_EXCLUDED_LEGACY_REASON
    ):
        raise ValueError("locked AMZN productive-assets definition changed")

    expected_layers = [
        ("chip_design", ["NVDA", "AMD", "AVGO"], False),
        ("foundry", ["2330"], True),
        ("packaging", ["3711"], False),
        ("memory", ["MU", "2408"], False),
        ("equipment", ["ASML", "AMAT", "LRCX", "KLAC"], False),
        ("materials", ["6488"], False),
        ("power_cooling", ["2308", "3017"], False),
        ("facilities", ["6139", "6196"], False),
    ]
    supplied_layers = config.get("layers")
    if not isinstance(supplied_layers, list):
        raise ValueError("layers must be a list")
    actual_layers = []
    for layer in supplied_layers:
        item = mapping_value(layer, "layer")
        actual_layers.append(
            (
                str(item.get("id")),
                [str(member) for member in item.get("members") or []],
                bool(item.get("control")),
            )
        )
    if actual_layers != expected_layers:
        raise ValueError("the eight frozen layers or their membership changed")

    revenue = require_mapping(config, "revenue")
    sec_issuers = require_mapping(revenue, "sec_issuers")
    if set(sec_issuers) != {"NVDA", "AMD", "AVGO", "MU", "AMAT", "LRCX", "KLAC"}:
        raise ValueError("U.S. revenue issuer universe is invalid")
    for member, details in sec_issuers.items():
        item = mapping_value(details, f"revenue.sec_issuers.{member}")
        validate_sec_sources(item.get("sources"), f"{member}.sources")
    asml = require_mapping(revenue, "asml")
    if set(asml) != {"ASML"} or asml["ASML"].get("function") != "fetch_asml_quarterly_revenue":
        raise ValueError("ASML must use fetch_asml_quarterly_revenue")
    taiwan = require_mapping(revenue, "taiwan")
    if taiwan.get("function") != "fetch_financials" or taiwan.get("field") != "revenue":
        raise ValueError("Taiwan revenue must use fetch_financials quarterly revenue")
    taiwan_members = require_mapping(taiwan, "members")
    expected_tw = {"2330", "3711", "2408", "6488", "2308", "3017", "6139", "6196"}
    if set(str(member) for member in taiwan_members) != expected_tw or "3416" in taiwan_members:
        raise ValueError("Taiwan universe must use corrected ID 6196, not 3416")
    supplied_exclusions = require_mapping(
        taiwan, "known_non_standalone_revenue_exclusions"
    )
    actual_exclusions: dict[str, dict[str, str]] = {}
    for member, quarter_reasons in supplied_exclusions.items():
        reason_map = mapping_value(
            quarter_reasons,
            f"revenue.taiwan.known_non_standalone_revenue_exclusions.{member}",
        )
        actual_exclusions[str(member)] = {
            str(quarter): str(reason) for quarter, reason in reason_map.items()
        }
    if actual_exclusions != FINMIND_KNOWN_NON_STANDALONE_EXCLUSIONS:
        raise ValueError("locked FinMind non-standalone exclusion map changed")

    stats = require_mapping(config, "statistics")
    if stats.get("lag_orientation") != "CAPEX(t) versus revenue(t+lag)":
        raise ValueError("locked lag orientation changed")
    lags = require_mapping(stats, "lags")
    if (
        int(lags.get("minimum")) != 0
        or int(lags.get("maximum")) != 8
        or list(lags.get("values") or []) != list(range(9))
    ):
        raise ValueError("lags must be exactly 0 through 8")
    raw_family = require_mapping(stats, "raw_confirmatory_family")
    detrended_family = require_mapping(stats, "detrended_confirmatory_family")
    if (
        list(raw_family.get("windows") or []) != ["full"]
        or int(raw_family.get("layers")) != 8
        or int(raw_family.get("lags_per_layer")) != 9
        or int(raw_family.get("family_size")) != 72
        or raw_family.get("correction") != "Holm"
    ):
        raise ValueError("raw confirmatory family must be full-only and exactly 72 cells")
    if (
        list(detrended_family.get("windows") or []) != ["full"]
        or int(detrended_family.get("layers")) != 8
        or int(detrended_family.get("lags_per_layer")) != 9
        or int(detrended_family.get("family_size")) != 72
        or detrended_family.get("correction") != "Holm"
        or str(detrended_family.get("break_quarter")) != "2020Q1"
        or list(detrended_family.get("terms") or [])
        != ["intercept", "linear_quarter_index", "post_level", "post_slope"]
        or detrended_family.get("exact_raw_supported_lag_required") is not True
    ):
        raise ValueError("detrended confirmatory family must be full-only and exactly 72 cells")
    split = require_mapping(stats, "split_diagnostics")
    if (
        list(split.get("windows") or []) != ["early", "late"]
        or int(split.get("bootstrap_draws")) != 0
        or split.get("correction_family") != "none"
        or split.get("exact_lag_match_required") is not True
    ):
        raise ValueError("split diagnostics must be early/late with zero draws")
    bootstrap = require_mapping(stats, "bootstrap")
    if (
        bootstrap.get("method") != "circular_stationary"
        or int(bootstrap.get("draws")) != 99_999
        or int(bootstrap.get("seed")) != 20_260_807
        or float(bootstrap.get("expected_block_length_quarters")) != 8.0
        or bootstrap.get("null_paths") != "independent_x_and_y"
        or bootstrap.get("confidence_interval_paths") != "paired"
    ):
        raise ValueError("locked bootstrap settings are invalid")
    minimum_n = require_mapping(stats, "minimum_n")
    if int(minimum_n.get("descriptive")) != 12 or int(minimum_n.get("inferential")) != 32:
        raise ValueError("locked sample-size thresholds are invalid")
    if float(stats.get("alpha")) != 0.05:
        raise ValueError("alpha must be 0.05")
    if int(stats.get("effective_sample_size_max_lag")) != 8:
        raise ValueError("effective sample size max lag must be eight")
    cycles = require_mapping(stats, "cycles")
    expected_cycle_settings = {
        "smoothing_quarters": 4,
        "extrema_radius": 2,
        "minimum_phase_quarters": 4,
        "minimum_cycle_quarters": 8,
        "minimum_for_nonexploratory_classification": 4,
    }
    if any(int(cycles.get(key)) != expected for key, expected in expected_cycle_settings.items()):
        raise ValueError("locked cycle settings changed")
    criteria = require_mapping(config, "criteria")
    if criteria.get("tsmc_control_layer") != "foundry":
        raise ValueError("TSMC control layer must be foundry")
    if int(criteria.get("minimum_supported_noncontrol_layers")) != 3:
        raise ValueError("minimum supported noncontrol layers must be three")
    boolean_criteria = (
        "all_must_pass",
        "exact_early_late_peak_match_to_full_supported_lag",
        "exact_lag_detrended_positive_holm_significant",
        "tsmc_control_must_have_no_positive_holm_significant_lag",
        "phase_2_forbidden_unless_all_pass",
    )
    if any(criteria.get(key) is not True for key in boolean_criteria):
        raise ValueError("all locked criteria booleans must remain true")

    changelog = config.get("changelog")
    if not isinstance(changelog, list) or not changelog:
        raise ValueError("config changelog is required")
    correction = mapping_value(changelog[-1], "changelog[-1]")
    if (
        str(correction.get("date")) != "2026-08-07"
        or correction.get("by") != "independent_review_correction"
        or correction.get("note") != INDEPENDENT_REVIEW_CHANGELOG_NOTE
    ):
        raise ValueError("independent-review correction changelog entry changed")


def require_mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    if key not in parent:
        raise ValueError(f"missing config section: {key}")
    return mapping_value(parent[key], key)


def mapping_value(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return value


def validate_sec_sources(value: Any, label: str) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    for index, source in enumerate(value):
        item = mapping_value(source, f"{label}[{index}]")
        cik = str(item.get("cik") or "")
        tags = item.get("tags")
        if not cik.isdigit() or len(cik) != 10:
            raise ValueError(f"{label}[{index}] requires a ten-digit CIK")
        if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) and tag for tag in tags):
            raise ValueError(f"{label}[{index}] requires ordered tags")
        if item.get("unit") != "USD" or not item.get("source_url"):
            raise ValueError(f"{label}[{index}] requires USD and a source URL")


def latest_completed_quarter(today: date | None = None) -> str:
    current = today or date.today()
    current_quarter = ((current.month - 1) // 3) + 1
    return core.shift_quarter(f"{current.year:04d}Q{current_quarter}", -1)


def quarter_bounds(quarter: str) -> tuple[str, str]:
    year, number = core.parse_quarter(quarter)
    month = (number - 1) * 3 + 1
    start = date(year, month, 1)
    after = date(year + 1, 1, 1) if number == 4 else date(year, month + 3, 1)
    return start.isoformat(), (after - timedelta(days=1)).isoformat()


def quarter_for_date(value: Any) -> str:
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"invalid source quarter date: {value!r}") from exc
    return f"{parsed.year:04d}Q{((parsed.month - 1) // 3) + 1}"


def analysis_role_for_quarter(quarter: str, output_start: str) -> str:
    core.parse_quarter(quarter)
    core.parse_quarter(output_start)
    return (
        "yoy_base_only"
        if core.quarter_index(quarter) < core.quarter_index(output_start)
        else "study"
    )


def select_audit_rows(
    rows: Sequence[Mapping[str, Any]],
    fetch_start: str,
    end_quarter: str,
    output_start: str,
) -> list[dict[str, Any]]:
    """Select reproducibility inputs and label pre-study YoY bases."""
    start_index = core.quarter_index(fetch_start)
    end_index = core.quarter_index(end_quarter)
    selected: list[dict[str, Any]] = []
    for supplied in rows:
        quarter = str(supplied.get("calendar_quarter"))
        quarter_index = core.quarter_index(quarter)
        if start_index <= quarter_index <= end_index:
            row = dict(supplied)
            row["analysis_role"] = analysis_role_for_quarter(
                quarter, output_start
            )
            selected.append(row)
    return selected


def finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def unique_present(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in (None, "") and value not in result:
            result.append(value)
    return result


def values_from(row: Mapping[str, Any], plural: str, singular: str) -> list[Any]:
    supplied = row.get(plural)
    values = list(supplied) if isinstance(supplied, list) else []
    if row.get(singular) not in (None, ""):
        values.append(row.get(singular))
    return unique_present(values)


def filing_dates_from(row: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    component_dates = row.get("component_filing_dates")
    if isinstance(component_dates, list):
        values.extend(component_dates)
    values.extend(values_from(row, "filing_dates", "filed"))
    return [str(value) for value in unique_present(values)]


def source_error(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return f"source_failure:{type(exc).__name__}:{message[:500]}"


def configured_values(sources: Any, key: str) -> list[Any]:
    result: list[Any] = []
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, Mapping):
            continue
        value = source.get(key)
        if isinstance(value, list):
            result.extend(value)
        elif value not in (None, ""):
            result.append(value)
    return unique_present(result)


def source_records_by_quarter(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        quarter = row.get("calendar_quarter")
        if not isinstance(quarter, str):
            raise ValueError("normalized SEC row is missing calendar_quarter")
        core.parse_quarter(quarter)
        result.setdefault(quarter, []).append(row)
    return result


def usable_component(
    candidates: Sequence[Mapping[str, Any]], component: str, failure: str | None
) -> tuple[Mapping[str, Any] | None, float | None, str | None]:
    if not candidates:
        return None, None, failure or f"missing_{component}"
    if len(candidates) != 1:
        return None, None, f"duplicate_{component}_quarter_rows"
    row = candidates[0]
    value = finite_number(row.get("value", row.get("val")))
    if value is None:
        return row, None, f"missing_or_nonfinite_{component}"
    if not filing_dates_from(row):
        return row, None, f"missing_filing_date_for_{component}"
    if not values_from(row, "source_urls", "source_url"):
        return row, None, f"missing_source_url_for_{component}"
    return row, abs(value), None


def fetch_capex_grid(
    config: Mapping[str, Any],
    start_quarter: str,
    end_quarter: str,
    refresh: bool,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    capex_config = require_mapping(config, "capex")
    companies = require_mapping(capex_config, "companies")
    fetched: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    failures: dict[str, str] = {}

    print("Fetching CAPEX sources (4 companies)...")
    for company in capex_config["frozen_companies"]:
        details = mapping_value(companies[company], f"capex company {company}")
        fetched[company] = {"ppe": [], "lease": []}
        try:
            fetched[company]["ppe"] = sources_sec.fetch_us_quarterly_metric(
                list(details["ppe_sources"]),
                start_quarter=start_quarter,
                refresh=refresh,
            )
        except SOURCE_FAILURES as exc:
            failures[f"{company}:ppe"] = source_error(exc)
            print(f"  {company} PPE insufficient: {failures[f'{company}:ppe']}")
        if company == "META":
            try:
                fetched[company]["lease"] = sources_sec.fetch_us_quarterly_metric(
                    list(details["finance_lease_sources"]),
                    start_quarter=start_quarter,
                    refresh=refresh,
                )
            except SOURCE_FAILURES as exc:
                failures[f"{company}:lease"] = source_error(exc)
                print(f"  META lease insufficient: {failures['META:lease']}")

    output: list[dict[str, Any]] = []
    for company in capex_config["frozen_companies"]:
        details = mapping_value(companies[company], f"capex company {company}")
        ppe_by_quarter = source_records_by_quarter(fetched[company]["ppe"])
        lease_by_quarter = source_records_by_quarter(fetched[company]["lease"])
        for quarter in core.quarter_range(start_quarter, end_quarter):
            if company == "AMZN" and core.quarter_index(
                quarter
            ) < core.quarter_index(str(details["first_standalone_quarter"])):
                ppe_row, ppe_value, ppe_reason = (
                    None,
                    None,
                    "before_first_standalone_amazon_productive_assets_value",
                )
            else:
                ppe_row, ppe_value, ppe_reason = usable_component(
                    ppe_by_quarter.get(quarter, []),
                    "ppe_capex",
                    failures.get(f"{company}:ppe"),
                )
            lease_row: Mapping[str, Any] | None = None
            lease_value: float | None = None
            lease_reason: str | None = None
            if company == "META":
                lease_row, lease_value, lease_reason = usable_component(
                    lease_by_quarter.get(quarter, []),
                    "finance_lease_principal",
                    failures.get("META:lease"),
                )
            component_rows = [row for row in (ppe_row, lease_row) if row is not None]
            filing_dates = unique_present(
                [date_value for row in component_rows for date_value in filing_dates_from(row)]
            )
            source_tags = unique_present(
                [
                    tag
                    for row in component_rows
                    for tag in values_from(row, "source_tags", "tag")
                ]
            )
            source_accessions = unique_present(
                [
                    accession
                    for row in component_rows
                    for accession in values_from(row, "source_accessions", "accn")
                ]
            )
            source_urls = unique_present(
                [
                    url
                    for row in component_rows
                    for url in values_from(row, "source_urls", "source_url")
                ]
            )
            if not source_tags:
                source_tags = configured_values(details.get("ppe_sources"), "tags")
                if company == "META":
                    source_tags.extend(
                        tag
                        for tag in configured_values(details.get("finance_lease_sources"), "tags")
                        if tag not in source_tags
                    )
            if not source_urls:
                source_urls = configured_values(details.get("ppe_sources"), "source_url")
                if company == "META":
                    source_urls.extend(
                        url
                        for url in configured_values(
                            details.get("finance_lease_sources"), "source_url"
                        )
                        if url not in source_urls
                    )

            complete = ppe_reason is None and (company != "META" or lease_reason is None)
            total = (
                ppe_value + lease_value
                if complete and company == "META" and ppe_value is not None and lease_value is not None
                else ppe_value
                if complete
                else None
            )
            reasons = unique_present(
                [
                    f"PPE:{ppe_reason}" if ppe_reason else None,
                    f"finance_lease:{lease_reason}"
                    if company == "META" and lease_reason
                    else None,
                ]
            )
            period_starts = unique_present(
                [row.get("period_start", row.get("start")) for row in component_rows]
            )
            period_ends = unique_present(
                [row.get("period_end", row.get("end")) for row in component_rows]
            )
            expected_start, expected_end = quarter_bounds(quarter)
            derivations = []
            if ppe_row is not None:
                derivations.append(f"PPE:{ppe_row.get('derivation') or 'unknown'}")
            if lease_row is not None:
                derivations.append(
                    f"finance_lease_principal:{lease_row.get('derivation') or 'unknown'}"
                )
            if company == "META":
                if complete:
                    notes = (
                        "Included financing-lease principal in total CAPEX; "
                        "both PPE and lease components are reported/derived without interpolation."
                    )
                else:
                    notes = (
                        "META total unavailable: financing-lease principal is required and a "
                        "missing lease value is not treated as zero; no interpolation."
                    )
            elif company == "AMZN":
                notes = (
                    f"{details['metric_label']}; the legacy ppe_capex_usd column "
                    "stores this broader gross productive-assets measure, not pure "
                    "PP&E. Quarters before 2017Q3 are unavailable. Excluded legacy "
                    f"tags: {','.join(str(tag) for tag in details['excluded_legacy_tags'])}. "
                    f"Reason: {details['excluded_legacy_reason']} No interpolation."
                )
            else:
                notes = "Total CAPEX equals PPE CAPEX; no interpolation."
            if company == "MSFT":
                notes += (
                    " Microsoft fiscal periods are assigned to the calendar quarter containing "
                    "the period midpoint."
                )
            if len(period_starts) > 1 or len(period_ends) > 1:
                notes += " Component period bounds differ; the displayed bounds span both components."
            ppe_tags = values_from(ppe_row or {}, "source_tags", "tag")
            ppe_urls = values_from(ppe_row or {}, "source_urls", "source_url")
            lease_tags = values_from(lease_row or {}, "source_tags", "tag")
            lease_urls = values_from(lease_row or {}, "source_urls", "source_url")
            if not ppe_tags:
                ppe_tags = configured_values(details.get("ppe_sources"), "tags")
            if not ppe_urls:
                ppe_urls = configured_values(details.get("ppe_sources"), "source_url")
            if company == "META" and not lease_tags:
                lease_tags = configured_values(
                    details.get("finance_lease_sources"), "tags"
                )
            if company == "META" and not lease_urls:
                lease_urls = configured_values(
                    details.get("finance_lease_sources"), "source_url"
                )
            output.append(
                {
                    "company": company,
                    "company_name": details.get("name"),
                    "calendar_quarter": quarter,
                    "period_start": min(str(value) for value in period_starts)
                    if period_starts
                    else expected_start,
                    "period_end": max(str(value) for value in period_ends)
                    if period_ends
                    else expected_end,
                    "currency": "USD",
                    "ppe_capex_usd": ppe_value,
                    "finance_lease_principal_usd": lease_value if company == "META" else None,
                    "total_capex_usd": total,
                    "status": "complete" if complete else "insufficient",
                    "missing_reason": ";".join(str(reason) for reason in reasons),
                    "derivation": ";".join(derivations) if derivations else "not_available",
                    "ppe_source_tags": ppe_tags,
                    "ppe_source_accessions": values_from(
                        ppe_row or {}, "source_accessions", "accn"
                    ),
                    "ppe_filing_dates": filing_dates_from(ppe_row or {}),
                    "ppe_source_urls": ppe_urls,
                    "finance_lease_source_tags": lease_tags,
                    "finance_lease_source_accessions": values_from(
                        lease_row or {}, "source_accessions", "accn"
                    ),
                    "finance_lease_filing_dates": filing_dates_from(lease_row or {}),
                    "finance_lease_source_urls": lease_urls,
                    "source_tags": source_tags,
                    "source_accessions": source_accessions,
                    "filing_dates": filing_dates,
                    "filed": max(filing_dates) if filing_dates else None,
                    "source_urls": source_urls,
                    "notes": notes,
                }
            )
    return output, failures


def normalize_sec_revenue_row(
    member: str, row: Mapping[str, Any], source_name: str
) -> dict[str, Any]:
    value = finite_number(row.get("value", row.get("val")))
    filing_dates = filing_dates_from(row)
    source_urls = values_from(row, "source_urls", "source_url")
    reasons = []
    if value is None:
        reasons.append("missing_or_nonfinite_revenue")
    if not filing_dates:
        reasons.append("missing_filing_date")
    if not source_urls:
        reasons.append("missing_source_url")
    return {
        "member": member,
        "calendar_quarter": str(row["calendar_quarter"]),
        "period_start": row.get("period_start", row.get("start")),
        "period_end": row.get("period_end", row.get("end")),
        "value": value,
        "excluded_source_value": None,
        "currency": str(row.get("unit") or "USD"),
        "status": "complete" if not reasons else "insufficient",
        "missing_reason": ";".join(reasons),
        "source": source_name,
        "source_tags": values_from(row, "source_tags", "tag"),
        "source_accessions": values_from(row, "source_accessions", "accn"),
        "filing_dates": filing_dates,
        "filed": max(filing_dates) if filing_dates else None,
        "filing_metadata_status": "SEC_filing_date" if filing_dates else "missing",
        "source_urls": source_urls,
        "derivation": row.get("derivation"),
        "notes": "Standalone quarter from SEC filing facts; no interpolation.",
    }


def taiwan_statutory_filing_date(quarter: str) -> str:
    year, number = core.parse_quarter(quarter)
    if number == 1:
        return date(year, 5, 15).isoformat()
    if number == 2:
        return date(year, 8, 14).isoformat()
    if number == 3:
        return date(year, 11, 14).isoformat()
    return date(year + 1, 3, 31).isoformat()


def normalize_finmind_revenue_row(
    member: str,
    row: Mapping[str, Any],
    source_url: str,
    exclusions: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    quarter = quarter_for_date(row.get("date"))
    start, end = quarter_bounds(quarter)
    source_value = finite_number(row.get("revenue"))
    exclusion_map = (
        FINMIND_KNOWN_NON_STANDALONE_EXCLUSIONS
        if exclusions is None
        else exclusions
    )
    exclusion_reason = (exclusion_map.get(member) or {}).get(quarter)
    excluded = exclusion_reason is not None
    value = None if excluded else source_value
    filed = taiwan_statutory_filing_date(quarter)
    if excluded:
        status = "insufficient"
        missing_reason = "known_non_standalone_finmind_revenue"
        notes = (
            f"Excluded {member} {quarter}: {exclusion_reason}. The FinMind raw "
            "value is retained in excluded_source_value; no replacement, "
            "interpolation, or reconstruction is used. Filing date is the "
            "conservative statutory deadline proxy."
        )
    else:
        status = "complete" if value is not None else "insufficient"
        missing_reason = "" if value is not None else "missing_or_nonfinite_revenue"
        notes = (
            "FinMind source row; filing date is the conservative statutory deadline "
            "because fetch_financials does not expose an accession or actual filing timestamp."
        )
    return {
        "member": member,
        "calendar_quarter": quarter,
        "period_start": start,
        "period_end": end,
        "value": value,
        "excluded_source_value": source_value if excluded else None,
        "currency": "TWD",
        "status": status,
        "missing_reason": missing_reason,
        "source": "FinMind taiwan_stock_financial_statement",
        "source_tags": ["Revenue"],
        "source_accessions": [],
        "filing_dates": [filed],
        "filed": filed,
        "filing_metadata_status": "statutory_deadline_proxy",
        "source_urls": [source_url],
        "derivation": "fetch_financials quarterly revenue",
        "notes": notes,
    }


def missing_revenue_row(
    member: str,
    quarter: str,
    currency: str,
    source: str,
    source_urls: Sequence[str],
    source_tags: Sequence[str],
    reason: str,
) -> dict[str, Any]:
    start, end = quarter_bounds(quarter)
    return {
        "member": member,
        "calendar_quarter": quarter,
        "period_start": start,
        "period_end": end,
        "value": None,
        "excluded_source_value": None,
        "currency": currency,
        "status": "insufficient",
        "missing_reason": reason,
        "source": source,
        "source_tags": list(source_tags),
        "source_accessions": [],
        "filing_dates": [],
        "filed": None,
        "filing_metadata_status": "missing",
        "source_urls": list(source_urls),
        "derivation": "not_available",
        "notes": "No value was imputed or interpolated.",
    }


def complete_revenue_member_grid(
    member: str,
    rows: Sequence[Mapping[str, Any]],
    start_quarter: str,
    end_quarter: str,
    *,
    currency: str,
    source: str,
    source_urls: Sequence[str],
    source_tags: Sequence[str],
    failure: str | None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        quarter = row.get("calendar_quarter")
        if not isinstance(quarter, str):
            raise ValueError(f"revenue row for {member} has no calendar quarter")
        grouped.setdefault(quarter, []).append(row)
    result: list[dict[str, Any]] = []
    for quarter in core.quarter_range(start_quarter, end_quarter):
        candidates = grouped.get(quarter, [])
        if len(candidates) == 1:
            result.append(dict(candidates[0]))
        else:
            reason = (
                "duplicate_member_quarter_source_rows"
                if len(candidates) > 1
                else failure or "no_reported_quarter"
            )
            result.append(
                missing_revenue_row(
                    member,
                    quarter,
                    currency,
                    source,
                    source_urls,
                    source_tags,
                    reason,
                )
            )
    return result


def fetch_revenue_grid(
    config: Mapping[str, Any],
    start_quarter: str,
    end_quarter: str,
    refresh: bool,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], dict[str, str]]:
    revenue = require_mapping(config, "revenue")
    sec_issuers = require_mapping(revenue, "sec_issuers")
    asml_config = require_mapping(revenue, "asml")["ASML"]
    taiwan = require_mapping(revenue, "taiwan")
    finmind_exclusions = require_mapping(
        taiwan, "known_non_standalone_revenue_exclusions"
    )
    failures: dict[str, str] = {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    source_descriptions: dict[str, str] = {}

    print("Fetching revenue sources (16 fixed members)...")
    for member, supplied in sec_issuers.items():
        details = mapping_value(supplied, f"SEC revenue {member}")
        ciks = configured_values(details["sources"], "cik")
        source_descriptions[member] = (
            "SEC companyfacts CIK " + ",".join(str(cik) for cik in ciks)
        )
        fetched: list[Mapping[str, Any]] = []
        try:
            fetched = sources_sec.fetch_us_quarterly_metric(
                list(details["sources"]),
                start_quarter=start_quarter,
                refresh=refresh,
            )
        except SOURCE_FAILURES as exc:
            failures[member] = source_error(exc)
            print(f"  {member} insufficient: {failures[member]}")
        rows = [normalize_sec_revenue_row(member, row, "SEC companyfacts") for row in fetched]
        normalized[member] = complete_revenue_member_grid(
            member,
            rows,
            start_quarter,
            end_quarter,
            currency=str(details["currency"]),
            source="SEC companyfacts",
            source_urls=[str(value) for value in configured_values(details["sources"], "source_url")],
            source_tags=[str(value) for value in configured_values(details["sources"], "tags")],
            failure=failures.get(member),
        )

    source_descriptions["ASML"] = (
        f"SEC 6-K U.S. GAAP exhibits CIK {asml_config['cik']}"
    )
    asml_fetched: list[Mapping[str, Any]] = []
    try:
        asml_fetched = sources_sec.fetch_asml_quarterly_revenue(
            start_quarter=start_quarter,
            refresh=refresh,
        )
    except SOURCE_FAILURES as exc:
        failures["ASML"] = source_error(exc)
        print(f"  ASML insufficient: {failures['ASML']}")
    asml_rows = [
        normalize_sec_revenue_row("ASML", row, "SEC 6-K U.S. GAAP exhibit")
        for row in asml_fetched
    ]
    normalized["ASML"] = complete_revenue_member_grid(
        "ASML",
        asml_rows,
        start_quarter,
        end_quarter,
        currency="EUR",
        source="SEC 6-K U.S. GAAP exhibit",
        source_urls=[str(value) for value in asml_config.get("source_urls") or []],
        source_tags=[str(value) for value in asml_config.get("tags") or []],
        failure=failures.get("ASML"),
    )

    source_url = str(taiwan["source_url"])
    start_date = quarter_bounds(start_quarter)[0]
    for member in taiwan["members"]:
        member_id = str(member)
        source_descriptions[member_id] = (
            "FinMind taiwan_stock_financial_statement"
        )
        fetched_finmind: list[Mapping[str, Any]] = []
        try:
            fetched_finmind = sources_finmind.fetch_financials(
                member_id,
                start_date=start_date,
            )
        except SOURCE_FAILURES as exc:
            failures[member_id] = source_error(exc)
            print(f"  {member_id} insufficient: {failures[member_id]}")
        finmind_rows = [
            normalize_finmind_revenue_row(
                member_id, row, source_url, finmind_exclusions
            )
            for row in fetched_finmind
        ]
        normalized[member_id] = complete_revenue_member_grid(
            member_id,
            finmind_rows,
            start_quarter,
            end_quarter,
            currency="TWD",
            source="FinMind taiwan_stock_financial_statement",
            source_urls=[source_url],
            source_tags=["Revenue"],
            failure=failures.get(member_id),
        )
    return normalized, failures, source_descriptions


def capex_analysis_endpoint(
    rows: Sequence[Mapping[str, Any]],
    companies: Sequence[str],
    output_start: str,
    latest_completed: str,
) -> str | None:
    complete = {
        (str(row.get("company")), str(row.get("calendar_quarter")))
        for row in rows
        if row.get("status") == "complete" and row.get("total_capex_usd") is not None
    }
    eligible = [
        quarter
        for quarter in core.quarter_range(output_start, latest_completed)
        if all((str(company), quarter) in complete for company in companies)
    ]
    return max(eligible, key=core.quarter_index) if eligible else None


def build_capex_aggregate(
    rows: Sequence[Mapping[str, Any]],
    companies: Sequence[str],
    start_quarter: str,
    end_quarter: str,
) -> dict[str, Any]:
    member_rows = [
        {
            "member": row["company"],
            "calendar_quarter": row["calendar_quarter"],
            "value": row["total_capex_usd"],
            "currency": "USD",
            "status": row["status"],
            "missing_reason": row["missing_reason"],
        }
        for row in rows
    ]
    return core.build_four_company_capex_aggregate(
        member_rows,
        list(companies),
        start_quarter=start_quarter,
        end_quarter=end_quarter,
    )


def build_layer_aggregates(
    config: Mapping[str, Any],
    revenue_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    start_quarter: str,
    end_quarter: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    fx = require_mapping(config, "fixed_fx")
    twd_per_usd = float(require_mapping(fx, "twd_per_usd")["value"])
    usd_per_eur = float(require_mapping(fx, "usd_per_eur")["value"])
    aggregates: dict[str, dict[str, Any]] = {}
    audit_rows: list[dict[str, Any]] = []

    for supplied_layer in config["layers"]:
        layer = mapping_value(supplied_layer, "layer")
        layer_id = str(layer["id"])
        members = [str(member) for member in layer["members"]]
        inputs = [dict(row) for member in members for row in revenue_rows[member]]
        aggregate = core.build_fixed_member_revenue_aggregate(
            inputs,
            members,
            twd_per_usd=twd_per_usd,
            usd_per_eur=usd_per_eur,
            start_quarter=start_quarter,
            end_quarter=end_quarter,
        )
        aggregates[layer_id] = aggregate
        source_lookup = {
            (str(row["member"]), str(row["calendar_quarter"])): row for row in inputs
        }
        panel_lookup = {
            (str(row["member"]), str(row["calendar_quarter"])): row
            for row in aggregate["panel"]
        }
        level_lookup = {row["calendar_quarter"]: row for row in aggregate["levels"]}
        yoy_lookup = {row["calendar_quarter"]: row for row in aggregate["yoy"]}

        for quarter in core.quarter_range(start_quarter, end_quarter):
            for member in members:
                source_row = source_lookup[(member, quarter)]
                panel_row = panel_lookup[(member, quarter)]
                audit_rows.append(
                    revenue_audit_base(layer, "member", member, quarter)
                    | {
                        "period_start": source_row.get("period_start"),
                        "period_end": source_row.get("period_end"),
                        "original_currency": source_row.get("currency"),
                        "original_value": source_row.get("value"),
                        "excluded_source_value": source_row.get(
                            "excluded_source_value"
                        ),
                        "fixed_twd_per_usd": twd_per_usd,
                        "fixed_usd_per_eur": usd_per_eur,
                        "fixed_usd_value": panel_row.get("value_usd"),
                        "status": "complete" if panel_row.get("usable") else "insufficient",
                        "missing_reason": panel_row.get("reason") or source_row.get("missing_reason"),
                        "source": source_row.get("source"),
                        "source_tags": source_row.get("source_tags"),
                        "source_accessions": source_row.get("source_accessions"),
                        "filing_dates": source_row.get("filing_dates"),
                        "filed": source_row.get("filed"),
                        "filing_metadata_status": source_row.get("filing_metadata_status"),
                        "source_urls": source_row.get("source_urls"),
                        "derivation": source_row.get("derivation"),
                        "notes": source_row.get("notes"),
                    }
                )
            level = level_lookup[quarter]
            audit_rows.append(
                revenue_audit_base(layer, "layer_aggregate", "", quarter)
                | {
                    "fixed_twd_per_usd": twd_per_usd,
                    "fixed_usd_per_eur": usd_per_eur,
                    "layer_fixed_usd_total": level.get("value_usd"),
                    "status": level.get("status"),
                    "missing_reason": "" if level.get("complete") else "missing_layer_member",
                    "missing_members": level.get("missing_members"),
                    "source": "capex_transmission.build_fixed_member_revenue_aggregate",
                    "derivation": "literal sum of every fixed member after fixed-FX conversion",
                    "notes": "Aggregate is present only when every frozen member is present.",
                }
            )
            yoy = yoy_lookup[quarter]
            audit_rows.append(
                revenue_audit_base(layer, "layer_yoy", "", quarter)
                | {
                    "base_quarter": yoy.get("base_quarter"),
                    "fixed_twd_per_usd": twd_per_usd,
                    "fixed_usd_per_eur": usd_per_eur,
                    "layer_yoy_pct": yoy.get("yoy"),
                    "status": yoy.get("status"),
                    "missing_reason": "" if yoy.get("usable") else yoy.get("status"),
                    "missing_members": yoy.get("missing_member_reasons"),
                    "source": "capex_transmission.yoy_from_complete_levels",
                    "derivation": "100 * (complete level(t) / complete level(t-4) - 1)",
                    "notes": "YoY is blank unless both fixed-member levels are complete.",
                }
            )
    return aggregates, audit_rows


def revenue_audit_base(
    layer: Mapping[str, Any], row_type: str, member: str, quarter: str
) -> dict[str, Any]:
    return {
        field: "" for field in REVENUE_AUDIT_FIELDS
    } | {
        "row_type": row_type,
        "layer_id": str(layer["id"]),
        "layer_name": str(layer["name"]),
        "is_control": bool(layer.get("control")),
        "member": member,
        "calendar_quarter": quarter,
    }


def merge_full_and_split_summaries(
    raw_full: Mapping[str, Any], split_grid: Mapping[str, Any]
) -> dict[str, Any]:
    split_by_layer = {
        str(layer["layer"]): layer for layer in split_grid.get("layers") or []
    }
    merged_layers = []
    for raw_layer in raw_full.get("layers") or []:
        layer_id = str(raw_layer["layer"])
        if layer_id not in split_by_layer:
            raise ValueError(f"split diagnostics missing layer {layer_id}")
        merged = dict(raw_layer)
        merged["windows"] = [
            *[dict(window) for window in raw_layer.get("windows") or []],
            *[dict(window) for window in split_by_layer[layer_id].get("windows") or []],
        ]
        merged_layers.append(merged)
    result = dict(raw_full)
    result["layers"] = merged_layers
    return result


def run_statistics(
    config: Mapping[str, Any],
    capex_yoy: Sequence[Mapping[str, Any]],
    layer_aggregates: Mapping[str, Mapping[str, Any]],
    endpoint: str,
    draws: int,
) -> dict[str, Any]:
    periods = require_mapping(config, "periods")
    stats = require_mapping(config, "statistics")
    bootstrap = require_mapping(stats, "bootstrap")
    criteria = require_mapping(config, "criteria")
    output_start = str(periods["output_start"])
    layers = [
        {
            "id": str(layer["id"]),
            "series": layer_aggregates[str(layer["id"])]["yoy"],
            "is_control": bool(layer.get("control")),
        }
        for layer in config["layers"]
    ]
    common = {
        "max_lag": 8,
        "expected_block_length": float(bootstrap["expected_block_length_quarters"]),
        "seed": int(bootstrap["seed"]),
        "alpha": float(stats["alpha"]),
    }

    print(f"Running raw full 72-cell family ({draws:,} bootstrap draws)...")
    raw = core.full_lag_grid(
        capex_yoy,
        layers,
        {"full": (output_start, endpoint)},
        draws=draws,
        detrend=False,
        **common,
    )
    if raw["family_size"] != 72:
        raise AssertionError(f"raw confirmatory family is {raw['family_size']}, expected 72")

    print("Running early/late descriptive diagnostics (draws=0; outside Holm family)...")
    split_grid = core.full_lag_grid(
        capex_yoy,
        layers,
        {
            "early": (str(periods["early"]["start"]), str(periods["early"]["end"])),
            "late": (str(periods["late"]["start"]), endpoint),
        },
        draws=0,
        detrend=False,
        **common,
    )
    merged = merge_full_and_split_summaries(raw, split_grid)
    split_consistency = core.assess_split_consistency(merged)

    print(f"Running detrended full 72-cell family ({draws:,} bootstrap draws)...")
    detrended = core.full_lag_grid(
        capex_yoy,
        layers,
        {"full": (output_start, endpoint)},
        draws=draws,
        detrend=True,
        break_quarter=str(stats["detrended_confirmatory_family"]["break_quarter"]),
        **common,
    )
    if detrended["family_size"] != 72:
        raise AssertionError(
            f"detrended confirmatory family is {detrended['family_size']}, expected 72"
        )
    trend = core.assess_trend_robustness(
        raw,
        detrended,
        full_window="full",
        alpha=float(stats["alpha"]),
    )
    decision = core.transmission_decision(
        raw,
        detrended,
        split_consistency=split_consistency,
        trend_robustness=trend,
        control_layer=str(criteria["tsmc_control_layer"]),
        min_supported_layers=int(criteria["minimum_supported_noncontrol_layers"]),
        alpha=float(stats["alpha"]),
    )
    cycle_settings = require_mapping(stats, "cycles")
    cycles = core.effective_cycle_diagnostics(
        capex_yoy,
        start_quarter=output_start,
        end_quarter=endpoint,
        smoothing_quarters=int(cycle_settings["smoothing_quarters"]),
        extrema_radius=int(cycle_settings["extrema_radius"]),
        min_phase_quarters=int(cycle_settings["minimum_phase_quarters"]),
        min_cycle_quarters=int(cycle_settings["minimum_cycle_quarters"]),
    )
    return {
        "raw": raw,
        "split_grid": split_grid,
        "split_consistency": split_consistency,
        "detrended": detrended,
        "trend_robustness": trend,
        "decision": decision,
        "cycles": cycles,
    }


def lag_csv_rows(
    config: Mapping[str, Any], results: Mapping[str, Any]
) -> list[dict[str, Any]]:
    layer_names = {str(layer["id"]): str(layer["name"]) for layer in config["layers"]}
    rows: list[dict[str, Any]] = []
    modes = [
        ("raw_full", results["raw"], False),
        ("split_descriptive", results["split_grid"], True),
        ("detrended_full", results["detrended"], False),
    ]
    for mode, grid, descriptive_only in modes:
        for cell in grid["cells"]:
            pairs = cell.get("pairs") or []
            ess_detail = cell.get("effective_sample_size") or {}
            status = (
                "descriptive_only"
                if descriptive_only and cell.get("n", 0) >= core.MIN_DESCRIPTIVE_N and cell.get("r") is not None
                else cell.get("status")
                if descriptive_only
                else cell.get("support_status")
            )
            rows.append(
                {
                    "mode": mode,
                    "window": cell.get("window"),
                    "layer": cell.get("layer"),
                    "layer_name": layer_names[str(cell.get("layer"))],
                    "lag": cell.get("lag"),
                    "n": cell.get("n"),
                    "segment_count": cell.get("segment_count"),
                    "segment_lengths": cell.get("segment_lengths"),
                    "segment_boundaries": cell.get("segment_boundaries"),
                    "r": cell.get("r"),
                    "pearson_p": cell.get("pearson_p"),
                    "pearson_p_status": cell.get("pearson_p_status"),
                    "bootstrap_p": "" if descriptive_only else cell.get("bootstrap_p"),
                    "bootstrap_ci": "" if descriptive_only else cell.get("bootstrap_ci_95"),
                    "holm_p": "" if descriptive_only else cell.get("holm_adjusted_p"),
                    "inferential_eligible": bool(cell.get("inferential_eligible")),
                    "support_status": status,
                    "ess": ess_detail.get("effective_n"),
                    "ess_status": ess_detail.get("status"),
                    "paired_capex_range": pair_range(pairs, "capex_quarter"),
                    "paired_revenue_range": pair_range(pairs, "revenue_quarter"),
                }
            )
    expected = 72 + 144 + 72
    if len(rows) != expected:
        raise AssertionError(f"lag output has {len(rows)} rows, expected {expected}")
    return rows


def pair_range(pairs: Sequence[Mapping[str, Any]], key: str) -> str:
    if not pairs:
        return ""
    return f"{pairs[0][key]}..{pairs[-1][key]}"


def write_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        for supplied in rows:
            writer.writerow({field: csv_value(supplied.get(field)) for field in fields})


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def coverage_record(
    domain: str,
    member: str,
    name: str,
    source: str,
    currency: str,
    rows: Sequence[Mapping[str, Any]],
    start_quarter: str,
    end_quarter: str,
) -> dict[str, Any]:
    expected = core.quarter_range(start_quarter, end_quarter)
    by_quarter = {
        str(row["calendar_quarter"]): row
        for row in rows
        if core.quarter_index(start_quarter)
        <= core.quarter_index(str(row["calendar_quarter"]))
        <= core.quarter_index(end_quarter)
    }
    usable = [
        quarter
        for quarter in expected
        if quarter in by_quarter
        and by_quarter[quarter].get("status") == "complete"
        and finite_number(
            by_quarter[quarter].get(
                "total_capex_usd", by_quarter[quarter].get("value")
            )
        )
        is not None
    ]
    missing = [quarter for quarter in expected if quarter not in usable]
    return {
        "domain": domain,
        "member": member,
        "name": name,
        "source": source,
        "currency": currency,
        "coverage_window": f"{start_quarter}..{end_quarter}",
        "first": usable[0] if usable else None,
        "last": usable[-1] if usable else None,
        "usable_count": len(usable),
        "expected_count": len(expected),
        "missing": missing,
    }


def build_coverage(
    config: Mapping[str, Any],
    capex_rows: Sequence[Mapping[str, Any]],
    revenue_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    source_descriptions: Mapping[str, str],
    output_start: str,
    latest_completed: str,
    endpoint: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    companies = config["capex"]["companies"]
    for company in config["capex"]["frozen_companies"]:
        details = companies[company]
        ciks = configured_values(details["ppe_sources"], "cik")
        result.append(
            coverage_record(
                "CAPEX",
                company,
                str(details["name"]),
                f"SEC companyfacts CIK {','.join(str(cik) for cik in ciks)}",
                "USD",
                [row for row in capex_rows if row["company"] == company],
                output_start,
                latest_completed,
            )
        )
    member_details = revenue_member_details(config)
    for member in ordered_revenue_members(config):
        details = member_details[member]
        result.append(
            coverage_record(
                "Revenue",
                member,
                details["name"],
                source_descriptions[member],
                details["currency"],
                revenue_rows[member],
                output_start,
                endpoint,
            )
        )
    return result


def ordered_revenue_members(config: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for layer in config["layers"]:
        for supplied in layer["members"]:
            member = str(supplied)
            if member not in result:
                result.append(member)
    return result


def revenue_member_details(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    revenue = config["revenue"]
    details: dict[str, dict[str, str]] = {}
    for member, item in revenue["sec_issuers"].items():
        details[str(member)] = {"name": str(item["name"]), "currency": str(item["currency"])}
    details["ASML"] = {
        "name": str(revenue["asml"]["ASML"]["name"]),
        "currency": "EUR",
    }
    for member, item in revenue["taiwan"]["members"].items():
        details[str(member)] = {"name": str(item["name"]), "currency": "TWD"}
    return details


def grid_window_summary(
    grid: Mapping[str, Any], layer_id: str, window: str
) -> Mapping[str, Any] | None:
    for layer in grid.get("layers") or []:
        if str(layer.get("layer")) != layer_id:
            continue
        for summary in layer.get("windows") or []:
            if summary.get("window") == window:
                return summary
    return None


def grid_cell(
    grid: Mapping[str, Any], layer_id: str, window: str, lag: Any
) -> Mapping[str, Any] | None:
    if lag is None:
        return None
    return next(
        (
            cell
            for cell in grid.get("cells") or []
            if str(cell.get("layer")) == layer_id
            and cell.get("window") == window
            and cell.get("lag") == lag
        ),
        None,
    )


def cell_ess(cell: Mapping[str, Any] | None) -> Any:
    detail = (cell or {}).get("effective_sample_size") or {}
    return detail.get("effective_n")


def fmt(value: Any, digits: int = 3) -> str:
    number = finite_number(value)
    if number is None:
        return "NA"
    if number == 0:
        return "0"
    if abs(number) < 0.001:
        return f"{number:.2e}"
    return f"{number:.{digits}f}"


def fmt_lag(value: Any) -> str:
    return "NA" if value is None else str(value)


def fmt_n_ess(cell: Mapping[str, Any] | None) -> str:
    if not cell:
        return "NA/NA"
    return f"{cell.get('n', 'NA')}/{fmt(cell_ess(cell), 1)}"


def pass_fail(value: Any) -> str:
    return "PASS" if bool(value) else "FAIL"


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(
    config: Mapping[str, Any],
    *,
    generated_at: str,
    latest_completed: str,
    endpoint: str,
    draws: int,
    debug: bool,
    refresh: bool,
    coverage: Sequence[Mapping[str, Any]],
    capex_rows: Sequence[Mapping[str, Any]],
    results: Mapping[str, Any],
) -> str:
    periods = config["periods"]
    stats = config["statistics"]
    raw = results["raw"]
    split_grid = results["split_grid"]
    detrended = results["detrended"]
    split_consistency = results["split_consistency"]
    trend = results["trend_robustness"]
    decision = results["decision"]
    cycles = results["cycles"]
    labels = {str(layer["id"]): str(layer["name"]) for layer in config["layers"]}
    lines: list[str] = []
    add = lines.append

    add("# 雲端 CAPEX 對半導體供應鏈營收的傳導研究")
    add("")
    add(f"- 產生時間（UTC）：{generated_at}")
    add(f"- 最新已完成日曆季：{latest_completed}；分析終點：{endpoint}")
    add(f"- 研究期間：{periods['output_start']} 至 {endpoint}；抓取基期自 {periods['fetch_start']}")
    add("- 範圍：獨立研究管線，不是交易策略，不估算報酬、進出場或可交易性。")
    add(
        "- 事前登記：使用專案目錄 `capex_transmission_config.yaml` 的 2026-08-07 "
        "鎖定規格及同日獨立審查修正；不使用公司 guidance 資料。"
    )
    add(f"- 本次設定檔 SHA-256：`{hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()}`。")
    sec_identity = sources_sec.sec_request_identity()
    add(
        f"- SEC Fair Access User-Agent：來源 `{sec_identity['source']}`；"
        "可用 `.env` 的 `SEC_USER_AGENT` 設定直接聯絡資訊。"
    )
    if debug:
        add(
            f"- 執行標籤：**NON-PREREGISTERED/DEBUG**，`--draws {draws}` 覆寫事前登記的 99,999 次；推論數字不可視為事前登記正式結果。"
        )
    else:
        add(f"- 執行標籤：preregistered，stationary bootstrap {draws:,} 次。")
    if refresh:
        add("- `--refresh`：SEC 來源強制更新；FinMind 的既有 API 無 refresh 參數，沿用其模組快取政策。")

    add("")
    add("## 方法")
    add("")
    add(
        "固定四家雲端公司 CAPEX 先以核心 `build_four_company_capex_aggregate` 加總；只有 t 與 t-4 四家公司都完整時才計算 YoY。"
    )
    add(
        "各營收層固定成員，以 2014Q1 的固定匯率逐季換算後作字面加總；任一成員缺值，該層當季及需要該水準的 YoY 都留白。"
    )
    add(
        "落後方向固定為 **CAPEX(t) 對 revenue(t+lag)**，lag=0..8；正 lag 表示營收晚於 CAPEX。"
    )
    add(
        "原始確認族只含 full 視窗的 8 層 x 9 lag = 72 格，使用 circular stationary bootstrap 與 72 格 Holm 校正。"
    )
    add(
        "early/late 是另一個 draws=0 的純描述執行，bootstrap/Holm 欄位刻意留白，沒有加入或改動原始 72 格族。"
    )
    add(
        "去趨勢確認族另為 72 格：分別將 CAPEX YoY 與營收 YoY 對截距、時間、2020Q1 後水準及斜率殘差化，再獨立 bootstrap/Holm。"
    )
    add(
        f"門檻：描述 n>={stats['minimum_n']['descriptive']}、推論 n>={stats['minimum_n']['inferential']}、alpha={stats['alpha']}、期望區塊長度 {stats['bootstrap']['expected_block_length_quarters']} 季。"
    )
    add(
        "缺季會把每一格配對切成連續日曆季度區塊；stationary bootstrap "
        "只在各區塊內循環抽樣，絕不跨越缺口。各格區塊數、長度及邊界均寫入 lag CSV。"
    )

    add("")
    add("## 資料涵蓋")
    add("")
    add("|類別|成員|名稱|來源|幣別|檢查區間|首筆|末筆|可用/應有|缺季|")
    add("|---|---|---|---|---|---|---|---|---:|---|")
    for row in coverage:
        missing = "無" if not row["missing"] else ",".join(row["missing"])
        add(
            "|{domain}|{member}|{name}|{source}|{currency}|{window}|{first}|{last}|{usable}/{expected}|{missing}|".format(
                domain=md_escape(row["domain"]),
                member=md_escape(row["member"]),
                name=md_escape(row["name"]),
                source=md_escape(row["source"]),
                currency=md_escape(row["currency"]),
                window=md_escape(row["coverage_window"]),
                first=row["first"] or "NA",
                last=row["last"] or "NA",
                usable=row["usable_count"],
                expected=row["expected_count"],
                missing=missing,
            )
        )
    add("")
    add("台灣名單已更正：原要求中的錯誤代號 3416 未使用，廠務層採用 **6196 帆宣**。")
    add(
        "`capex_history.csv` 與 `capex_transmission_revenue.csv` 都明列 "
        "2013Q1-Q4，`analysis_role=yoy_base_only`；2013 僅作為可能的 t-4 基期，"
        "只有基期與當期都完整時才重現 2014 YoY。涵蓋率、報告與主要研究期間仍自 2014Q1 起。"
    )
    finmind_exclusions = config["revenue"]["taiwan"][
        "known_non_standalone_revenue_exclusions"
    ]
    exclusion_text = "；".join(
        f"{member} {quarter}（{reason}）"
        for member, quarter_reasons in finmind_exclusions.items()
        for quarter, reason in quarter_reasons.items()
    )
    add(
        "已知 FinMind 非單季營收排除："
        + exclusion_text
        + "；原值只保留於 `excluded_source_value`，分析值留白且不替換、不插值。"
    )

    meta_gaps = [
        str(row["calendar_quarter"])
        for row in capex_rows
        if row["company"] == "META"
        and row.get("analysis_role") == "study"
        and row["status"] != "complete"
    ]
    amazon_config = config["capex"]["companies"]["AMZN"]
    add("")
    add("## CAPEX 定義與稽核")
    add("")
    add("- MSFT 與 GOOGL 使用 PPE 現金 CAPEX；META total = PPE + financing-lease principal，且兩者都存在才成立。")
    add(
        "- AMZN 只使用 `PaymentsToAcquireProductiveAssets`：Amazon gross "
        "productive-assets cash purchases，範圍包含 PP&E、internal-use software "
        "及其他 intangibles；不是純 PPE，也不與其他公司宣稱範圍完全相同。"
    )
    add(
        "- AMZN 2017Q3 前沒有該 tag 的 standalone 值，全部維持 insufficient；"
        "排除 "
        + "、".join(
            f"`{tag}`" for tag in amazon_config["excluded_legacy_tags"]
        )
        + f"。理由：{amazon_config['excluded_legacy_reason']}"
    )
    add("- META 缺少 lease principal 時絕不當作 0。META 缺口：" + (",".join(meta_gaps) if meta_gaps else "無"))
    add("- META 完整列的 CSV notes 明載 included financing-lease principal；各成分的 tag、accession、來源網址及 filing date 都保留。")
    add("- 所有 SEC 非日曆財年公司（包括 MSFT 與供應鏈美股）：依報告期間中點所落的日曆季轉換，不把 fiscal quarter 名稱或期末月份直接當 calendar quarter。")
    add("- standalone quarter 可為直接季值，或同一 CIK、同一設定 metric 內的相鄰 YTD、FY 算術差額；不插值、不以前後季補值。")
    add(
        "- Vintage 規則先選最早有效 filing date（衍生列為各成分 filing date 的最大值），"
        "再依設定 source/tag priority，最後才以直接季值勝過衍生值作同日 tie-break。"
        "較晚 comparative disclosure 只能填補完全沒有更早 standalone 候選的歷史季度；"
        "所有延遲 filing date 仍完整揭露。這是 historical actual research，"
        "不是 point-in-time trading，也不把延遲揭露當 contemporaneous signal。"
    )
    add(
        f"- 固定匯率：TWD/USD={config['fixed_fx']['twd_per_usd']['value']}；USD/EUR={config['fixed_fx']['usd_per_eur']['value']}，均為 2014Q1 FRED 61 筆非缺失日值平均。"
    )

    add("")
    add("## Full 最佳落後期")
    add("")
    add("`n/ESS` 同時列出季度配對數與 lag-8 自相關診斷的有效樣本數；ESS 不是新的顯著性檢定。")
    add("")
    add("|層級|描述峰值 lag|支持 lag|峰值 r|峰值一般 p|峰值 block-bootstrap p|峰值 Holm p|峰值 n/ESS|支持 r|支持一般 p|支持 bootstrap p|支持 Holm p|支持 n/ESS|狀態|")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for layer in config["layers"]:
        layer_id = str(layer["id"])
        summary = grid_window_summary(raw, layer_id, "full") or {}
        peak = grid_cell(raw, layer_id, "full", summary.get("descriptive_peak_lag"))
        supported = grid_cell(raw, layer_id, "full", summary.get("best_supported_lag"))
        add(
            f"|{layer_id} {labels[layer_id]}|{fmt_lag((peak or {}).get('lag'))}|{fmt_lag((supported or {}).get('lag'))}|"
            f"{fmt((peak or {}).get('r'))}|{fmt((peak or {}).get('pearson_p'))}|{fmt((peak or {}).get('bootstrap_p'))}|"
            f"{fmt((peak or {}).get('holm_adjusted_p'))}|{fmt_n_ess(peak)}|{fmt((supported or {}).get('r'))}|"
            f"{fmt((supported or {}).get('pearson_p'))}|{fmt((supported or {}).get('bootstrap_p'))}|"
            f"{fmt((supported or {}).get('holm_adjusted_p'))}|{fmt_n_ess(supported)}|"
            f"{summary.get('status', 'not_assessable')}|"
        )

    split_by_layer = {
        str(row["layer"]): row for row in split_consistency.get("layers") or []
    }
    add("")
    add("## 分段一致性")
    add("")
    add("必須 early 與 late 的正相關描述峰值都**精確等於** full 的支持 lag；不接受相鄰 lag 替代。")
    add("")
    add("|層級|full 支持 lag|early 峰值 lag|early n/ESS|early r|late 峰值 lag|late n/ESS|late r|判定|")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for layer in config["layers"]:
        layer_id = str(layer["id"])
        assessment = split_by_layer[layer_id]
        early_cell = grid_cell(
            split_grid, layer_id, "early", assessment.get("early_descriptive_peak_lag")
        )
        late_cell = grid_cell(
            split_grid, layer_id, "late", assessment.get("late_descriptive_peak_lag")
        )
        add(
            f"|{layer_id} {labels[layer_id]}|{fmt_lag(assessment.get('full_supported_lag'))}|"
            f"{fmt_lag(assessment.get('early_descriptive_peak_lag'))}|{fmt_n_ess(early_cell)}|{fmt(assessment.get('early_r'))}|"
            f"{fmt_lag(assessment.get('late_descriptive_peak_lag'))}|{fmt_n_ess(late_cell)}|{fmt(assessment.get('late_r'))}|"
            f"{assessment.get('status', 'not_assessable')}|"
        )

    trend_by_layer = {str(row["layer"]): row for row in trend.get("layers") or []}
    add("")
    add("## 去趨勢比較")
    add("")
    add("|層級|raw 描述峰值 lag/r|detrended 描述峰值 lag/r|detrended 峰值 n/ESS|raw 支持 exact lag|該 lag detrended r|該 lag detrended Holm p|該 lag n/ESS|exact-lag 穩健性|")
    add("|---|---|---|---:|---:|---:|---:|---:|---|")
    for layer in config["layers"]:
        layer_id = str(layer["id"])
        raw_summary = grid_window_summary(raw, layer_id, "full") or {}
        det_summary = grid_window_summary(detrended, layer_id, "full") or {}
        raw_peak = grid_cell(raw, layer_id, "full", raw_summary.get("descriptive_peak_lag"))
        det_peak = grid_cell(
            detrended, layer_id, "full", det_summary.get("descriptive_peak_lag")
        )
        assessment = trend_by_layer[layer_id]
        exact_cell = grid_cell(
            detrended, layer_id, "full", assessment.get("raw_supported_lag")
        )
        add(
            f"|{layer_id} {labels[layer_id]}|{fmt_lag((raw_peak or {}).get('lag'))}/{fmt((raw_peak or {}).get('r'))}|"
            f"{fmt_lag((det_peak or {}).get('lag'))}/{fmt((det_peak or {}).get('r'))}|{fmt_n_ess(det_peak)}|"
            f"{fmt_lag(assessment.get('raw_supported_lag'))}|{fmt(assessment.get('detrended_r'))}|"
            f"{fmt(assessment.get('detrended_holm_adjusted_p'))}|{fmt_n_ess(exact_cell)}|"
            f"{assessment.get('status', 'not_assessable')}|"
        )

    add("")
    add("## 有效循環數")
    add("")
    boundaries = [
        f"{cycle['start_trough']} -> {cycle['peak']} -> {cycle['end_trough']}"
        for cycle in cycles.get("cycles") or []
    ]
    add(f"- 完整、交替的 trough-to-trough 有效獨立循環數：**{cycles.get('cycle_count', 0)}**。")
    add("- 循環邊界：" + ("；".join(boundaries) if boundaries else "未辨識出完整循環"))
    episodes = stats["cycles"].get("design_period_episodes") or []
    add(
        "- 研究設計事前辨識的約三段景氣事件："
        + ("；".join(str(item) for item in episodes) if episodes else "未列示")
        + "。這是定性事件數，不等於符合 trough-to-trough 規則的有效獨立循環。"
    )
    add("- 警告：季度 n 是重疊 YoY 的觀測筆數，不是獨立循環數；不能把 n 季解讀為 n 個獨立景氣實驗。")
    if int(cycles.get("cycle_count", 0)) < int(stats["cycles"]["minimum_for_nonexploratory_classification"]):
        add("- 因少於四個完整循環，本研究證據明確分類為 **exploratory（探索性）**，即使其他統計條件通過亦同。")
    else:
        add("- 循環數達事前登記的四循環非探索性門檻；這仍不代表因果或可交易性。")

    control_summary = grid_window_summary(raw, "foundry", "full") or {}
    control_peak = grid_cell(
        raw, "foundry", "full", control_summary.get("descriptive_peak_lag")
    )
    control_cells = [
        cell
        for cell in raw["cells"]
        if cell.get("layer") == "foundry" and cell.get("window") == "full"
    ]
    control_hits = [
        cell["lag"]
        for cell in control_cells
        if cell.get("supports_transmission")
    ]
    control_criterion = decision["criteria"]["tsmc_control_no_positive_significant_lag"]
    add("")
    add("## 台積電對照")
    add("")
    add(
        f"foundry（晶圓代工/台積電對照）的描述峰值 lag={fmt_lag(control_summary.get('descriptive_peak_lag'))}，"
        f"r={fmt((control_peak or {}).get('r'))}、一般 p={fmt((control_peak or {}).get('pearson_p'))}、"
        f"block-bootstrap p={fmt((control_peak or {}).get('bootstrap_p'))}、Holm p={fmt((control_peak or {}).get('holm_adjusted_p'))}；"
        f"正向 Holm 顯著 lag={','.join(str(value) for value in control_hits) if control_hits else '無'}；"
        f"對照標準 {pass_fail(control_criterion['pass'])}。"
    )
    if not control_criterion.get("assessable"):
        add("對照不可評估，因此不能排除結果只是廣泛半導體景氣或 market beta。")
    elif control_hits:
        add("台積電對照也呈正向顯著，支持『廣泛半導體景氣/beta』替代解釋，而非供應鏈特定傳導。")
    else:
        add(
            "台積電對照未出現正向 Holm 顯著 lag，形式上通過事前控制門檻；"
            "但未校正相關仍強，不能排除廣泛半導體景氣/beta，也不建立因果。"
        )

    criterion_labels = {
        "at_least_three_noncontrol_layers_supported": "至少三個非對照層在 full 有正向 Holm 支持",
        "exact_split_consistency": "所有受支持非對照層的 early/late 峰值精確一致",
        "exact_lag_detrended_significance": "所有受支持非對照層在 raw exact lag 去趨勢後仍顯著",
        "tsmc_control_no_positive_significant_lag": "台積電對照沒有正向 Holm 顯著 lag",
    }
    add("")
    add("## 四項標準與結論")
    add("")
    add("|標準|可評估|結果|細節|")
    add("|---|---|---|---|")
    for key, criterion in decision["criteria"].items():
        details = {
            item_key: item_value
            for item_key, item_value in criterion.items()
            if item_key not in {"pass", "assessable"}
        }
        add(
            f"|{criterion_labels[key]}|{pass_fail(criterion.get('assessable'))}|{pass_fail(criterion.get('pass'))}|"
            f"{md_escape(json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(',', ':')))}|"
        )
    add("")
    add(f"核心 decision helper 的最終結論（原樣）：**{decision['conclusion']}**")
    add("此為事前登記的失敗分類用語；本次實證意義是樣本不足、未能證明穩定傳導，不是統計上證明關係必然不存在或必然不穩定。")
    add("")
    add("**第二階段限制：除非上述四項標準全部 PASS，否則禁止進入 phase 2。**")
    if not decision["pass"] and decision["conclusion"] != core.FAIL_CONCLUSION:
        raise AssertionError("failed decision did not return the locked failure conclusion")

    add("")
    add("## 限制")
    add("")
    add("- 研究期間事前辨識約三段景氣事件／regime，但機械規則辨識的完整獨立循環為上表數值；重疊 YoY、共同週期與低有效循環數會產生 pseudo-correlation。")
    add("- 共同趨勢即使經殘差化仍不等於因果；相關與領先落後不能證明 CAPEX 導致特定供應商營收。")
    add("- 公開 CAPEX guidance 會立即反映在價格，本研究刻意不用 guidance；即使財報相關存在，也完全不代表可交易性。")
    add("- 多角化公司的總營收不是純 AI 曝險，層級加總混合了非雲端、非 AI 與不同產品週期。")
    add("- 缺值不插補，會改變不同 lag 的配對樣本；TWD 與 EUR 全期使用固定 2014Q1 FX，忽略之後匯率變動。")
    add("- AMZN productive-assets 購買包含 PP&E、內部使用軟體及其他無形資產，與其他公司的 PPE 定義不同；2017Q3 前歷史因排除舊 net/legacy tags 而不可用。")
    add("- 延遲 comparative facts 只可填補原本完全缺少候選的歷史季度，並非當時可交易訊號；本研究不主張 point-in-time trading。")
    add("- 72 格 Holm 校正處理多重檢定；gap-aware stationary block bootstrap 只在連續季度區塊內抽樣。兩者只能降低、不能消除自相關與模型選擇風險。")
    return "\n".join(lines) + "\n"


def resolve_output_dir(value: str) -> Path:
    supplied = Path(value).expanduser()
    return supplied if supplied.is_absolute() else ROOT / supplied


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    output_dir = resolve_output_dir(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    periods = config["periods"]
    fetch_start = str(periods["fetch_start"])
    output_start = str(periods["output_start"])
    latest_completed = latest_completed_quarter()
    if core.quarter_index(latest_completed) < core.quarter_index(output_start):
        raise RuntimeError("latest completed quarter predates the registered study")
    configured_draws = int(config["statistics"]["bootstrap"]["draws"])
    draws = configured_draws if args.draws is None else args.draws
    debug = draws != configured_draws
    if debug:
        print(
            f"NON-PREREGISTERED/DEBUG: --draws {draws:,} overrides registered {configured_draws:,}."
        )
    if args.refresh:
        print("Refresh requested: SEC refresh enabled; FinMind follows its module cache API.")

    capex_all, capex_failures = fetch_capex_grid(
        config,
        fetch_start,
        latest_completed,
        args.refresh,
    )
    capex_history = select_audit_rows(
        capex_all, fetch_start, latest_completed, output_start
    )
    capex_path = output_dir / "capex_history.csv"
    write_csv(capex_path, CAPEX_HISTORY_FIELDS, capex_history)
    companies = [str(value) for value in config["capex"]["frozen_companies"]]
    endpoint = capex_analysis_endpoint(
        capex_all,
        companies,
        output_start,
        latest_completed,
    )
    study_capex_history = [
        row for row in capex_history if row["analysis_role"] == "study"
    ]
    complete_capex = sum(
        row["status"] == "complete" for row in study_capex_history
    )
    print(
        f"CAPEX coverage: {complete_capex}/{len(study_capex_history)} "
        "study company-quarters complete; "
        f"analysis endpoint={endpoint or 'unavailable'}."
    )
    if endpoint is None:
        raise RuntimeError(
            f"no quarter from {output_start} through {latest_completed} has all four CAPEX totals; "
            f"audit written to {capex_path}"
        )
    if core.quarter_index(endpoint) < core.quarter_index(str(periods["late"]["start"])):
        raise RuntimeError(
            f"analysis endpoint {endpoint} predates the registered late window; audit written to {capex_path}"
        )

    revenue_all, revenue_failures, source_descriptions = fetch_revenue_grid(
        config,
        fetch_start,
        latest_completed,
        args.refresh,
    )
    revenue_complete = sum(
        row["status"] == "complete"
        for member_rows in revenue_all.values()
        for row in member_rows
        if core.quarter_index(row["calendar_quarter"]) >= core.quarter_index(output_start)
        and core.quarter_index(row["calendar_quarter"]) <= core.quarter_index(endpoint)
    )
    revenue_expected = len(revenue_all) * len(core.quarter_range(output_start, endpoint))
    print(f"Revenue coverage: {revenue_complete}/{revenue_expected} member-quarters complete.")

    capex_aggregate = build_capex_aggregate(
        capex_all,
        companies,
        fetch_start,
        latest_completed,
    )
    layer_aggregates, revenue_audit_all = build_layer_aggregates(
        config,
        revenue_all,
        fetch_start,
        endpoint,
    )
    revenue_audit = select_audit_rows(
        revenue_audit_all, fetch_start, endpoint, output_start
    )
    revenue_path = output_dir / "capex_transmission_revenue.csv"
    write_csv(revenue_path, REVENUE_AUDIT_FIELDS, revenue_audit)

    results = run_statistics(
        config,
        capex_aggregate["yoy"],
        layer_aggregates,
        endpoint,
        draws,
    )
    lag_rows = lag_csv_rows(config, results)
    lag_path = output_dir / "capex_transmission_lags.csv"
    write_csv(lag_path, LAG_FIELDS, lag_rows)

    coverage = build_coverage(
        config,
        capex_all,
        revenue_all,
        source_descriptions,
        output_start,
        latest_completed,
        endpoint,
    )
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report = render_report(
        config,
        generated_at=generated_at,
        latest_completed=latest_completed,
        endpoint=endpoint,
        draws=draws,
        debug=debug,
        refresh=args.refresh,
        coverage=coverage,
        capex_rows=capex_history,
        results=results,
    )
    report_path = output_dir / "capex_transmission.md"
    report_path.write_text(report, encoding="utf-8", newline="\n")

    if capex_failures or revenue_failures:
        print(
            f"Explicit source failures: CAPEX={len(capex_failures)}, revenue={len(revenue_failures)}; "
            "affected rows remain insufficient."
        )
    for path in (capex_path, revenue_path, lag_path, report_path):
        print(f"Output: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
