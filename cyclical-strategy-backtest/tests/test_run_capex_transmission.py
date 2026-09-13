from __future__ import annotations

import copy

import pytest

import run_capex_transmission as runner


def test_finmind_known_anomaly_is_unavailable_and_retains_raw_value() -> None:
    row = runner.normalize_finmind_revenue_row(
        "3711",
        {"date": "2018-06-30", "revenue": 123456.0},
        "https://api.finmindtrade.com/api/v4/data",
    )

    assert row["value"] is None
    assert row["excluded_source_value"] == 123456.0
    assert row["status"] == "insufficient"
    assert row["missing_reason"] == "known_non_standalone_finmind_revenue"
    assert "FinMind selected six-month cumulative value" in row["notes"]
    assert "no replacement, interpolation, or reconstruction" in row["notes"]
    assert "excluded_source_value" in runner.REVENUE_AUDIT_FIELDS


def test_finmind_nonexcluded_row_is_unchanged() -> None:
    row = runner.normalize_finmind_revenue_row(
        "3711",
        {"date": "2018-03-31", "revenue": 123456.0},
        "https://api.finmindtrade.com/api/v4/data",
    )

    assert row["value"] == 123456.0
    assert row["excluded_source_value"] is None
    assert row["status"] == "complete"
    assert row["missing_reason"] == ""


def test_audit_output_selection_includes_and_labels_2013_baseline() -> None:
    rows = [
        {"calendar_quarter": "2012Q4", "value": 0},
        {"calendar_quarter": "2013Q1", "value": 1},
        {"calendar_quarter": "2013Q4", "value": 2},
        {"calendar_quarter": "2014Q1", "value": 3},
        {"calendar_quarter": "2014Q2", "value": 4},
    ]

    selected = runner.select_audit_rows(
        rows, "2013Q1", "2014Q1", "2014Q1"
    )

    assert [row["calendar_quarter"] for row in selected] == [
        "2013Q1",
        "2013Q4",
        "2014Q1",
    ]
    assert [row["analysis_role"] for row in selected] == [
        "yoy_base_only",
        "yoy_base_only",
        "study",
    ]
    assert "analysis_role" in runner.CAPEX_HISTORY_FIELDS
    assert "analysis_role" in runner.REVENUE_AUDIT_FIELDS


def test_loaded_config_contains_exact_source_correction_locks() -> None:
    config = runner.load_config()
    amazon = config["capex"]["companies"]["AMZN"]

    assert amazon["ppe_sources"][0]["cik"] == "0001018724"
    assert amazon["ppe_sources"][0]["tags"] == [runner.AMAZON_CAPEX_TAG]
    assert amazon["excluded_legacy_tags"] == runner.AMAZON_EXCLUDED_LEGACY_TAGS
    assert (
        config["revenue"]["taiwan"][
            "known_non_standalone_revenue_exclusions"
        ]
        == runner.FINMIND_KNOWN_NON_STANDALONE_EXCLUSIONS
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda config: config["capex"]["companies"]["AMZN"][
                "ppe_sources"
            ][0]["tags"].append("PaymentsToAcquirePropertyPlantAndEquipment"),
            "AMZN",
        ),
        (
            lambda config: config["revenue"]["taiwan"][
                "known_non_standalone_revenue_exclusions"
            ]["6488"].pop("2014Q4"),
            "FinMind",
        ),
        (
            lambda config: config["statistics"][
                "detrended_confirmatory_family"
            ].update({"break_quarter": "2020Q2"}),
            "detrended",
        ),
        (
            lambda config: config["statistics"]["cycles"].update(
                {"minimum_cycle_quarters": 7}
            ),
            "cycle",
        ),
        (
            lambda config: config["statistics"]["bootstrap"].update(
                {"null_paths": "paired"}
            ),
            "bootstrap",
        ),
        (
            lambda config: config["criteria"].update(
                {"all_must_pass": False}
            ),
            "criteria",
        ),
    ],
)
def test_validate_config_rejects_inference_critical_lock_changes(
    mutation: object, message: str
) -> None:
    config = copy.deepcopy(runner.load_config())
    mutation(config)  # type: ignore[operator]

    with pytest.raises(ValueError, match=message):
        runner.validate_config(config)


def test_lag_csv_rows_exposes_gap_segments_and_blanks_split_inference() -> None:
    layers = [
        {"id": f"layer_{index}", "name": f"Layer {index}"}
        for index in range(8)
    ]
    config = {"layers": layers}
    base_cell = {
        "window": "full",
        "layer": "layer_0",
        "lag": 1,
        "n": 5,
        "segment_count": 2,
        "segment_lengths": [2, 3],
        "segment_boundaries": [{"start_capex_quarter": "2014Q1"}],
        "r": 0.5,
        "pearson_p": 0.1,
        "pearson_p_status": "ok",
        "bootstrap_p": 0.2,
        "bootstrap_ci_95": [0.1, 0.9],
        "holm_adjusted_p": 0.3,
        "inferential_eligible": False,
        "support_status": "ineligible",
        "status": "insufficient_n",
        "effective_sample_size": {"effective_n": 3, "status": "ok"},
        "pairs": [],
    }
    raw_cells = [
        dict(base_cell, layer=layer["id"], lag=lag)
        for layer in layers
        for lag in range(9)
    ]
    split_cells = [
        dict(base_cell, layer=layer["id"], window=window, lag=lag)
        for layer in layers
        for window in ("early", "late")
        for lag in range(9)
    ]
    results = {
        "raw": {"cells": raw_cells},
        "split_grid": {"cells": split_cells},
        "detrended": {"cells": [dict(row) for row in raw_cells]},
    }

    rows = runner.lag_csv_rows(config, results)

    assert len(rows) == 288
    assert rows[0]["segment_count"] == 2
    assert rows[0]["segment_lengths"] == [2, 3]
    assert rows[0]["segment_boundaries"] == [
        {"start_capex_quarter": "2014Q1"}
    ]
    split = next(row for row in rows if row["mode"] == "split_descriptive")
    assert split["bootstrap_p"] == ""
    assert split["bootstrap_ci"] == ""
    assert split["holm_p"] == ""
