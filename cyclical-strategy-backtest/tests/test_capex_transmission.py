from __future__ import annotations

import copy
import math
from typing import Any

import pytest

import capex_transmission as ct


def member_row(
    member: str,
    quarter: str,
    value: float,
    currency: str = "USD",
) -> dict[str, object]:
    return {
        "member": member,
        "calendar_quarter": quarter,
        "value": value,
        "currency": currency,
        "status": "reported",
    }


def test_fixed_member_levels_use_fixed_fx_literal_sum_and_t_minus_four_yoy() -> None:
    rows = [
        member_row("US", "2020Q1", 10),
        member_row("TW", "2020Q1", 300, "TWD"),
        member_row("EU", "2020Q1", 10, "EUR"),
        member_row("US", "2021Q1", 20),
        member_row("TW", "2021Q1", 600, "TWD"),
        member_row("EU", "2021Q1", 20, "EUR"),
        member_row("NEW", "2021Q1", 10_000),
    ]

    result = ct.build_fixed_member_revenue_aggregate(
        rows,
        ["US", "TW", "EU"],
        fixed_rates={"TWD_per_USD": 30, "USD_per_EUR": 1.2},
        start_quarter="2020Q1",
        end_quarter="2021Q1",
    )
    levels = {row["calendar_quarter"]: row for row in result["levels"]}
    panel = {
        (row["calendar_quarter"], row["member"]): row for row in result["panel"]
    }
    yoy = {row["calendar_quarter"]: row for row in result["yoy"]}

    assert panel[("2020Q1", "TW")]["value_usd"] == pytest.approx(10)
    assert panel[("2020Q1", "EU")]["value_usd"] == pytest.approx(12)
    assert levels["2020Q1"]["value_usd"] == pytest.approx(10 + 10 + 12)
    assert levels["2021Q1"]["value_usd"] == pytest.approx(20 + 20 + 24)
    assert yoy["2020Q1"]["yoy"] is None
    assert yoy["2020Q1"]["status"] == "base_quarter_outside_panel"
    assert yoy["2021Q1"]["base_quarter"] == "2020Q1"
    assert yoy["2021Q1"]["yoy"] == pytest.approx(100)
    assert result["fixed_rates"] == {
        "twd_per_usd": 30.0,
        "usd_per_eur": 1.2,
        "rate_period": "2014Q1",
    }
    assert result["composition_changed"] is False
    assert result["frozen_members"] == ["US", "TW", "EU"]
    assert result["ignored_nonmembers"] == ["NEW"]
    assert {row["member"] for row in result["panel"]} == {"US", "TW", "EU"}


def test_missing_member_or_incomplete_base_suppresses_level_and_yoy() -> None:
    rows = [
        member_row("A", "2020Q1", 10),
        member_row("A", "2021Q1", 20),
        member_row("B", "2021Q1", 20),
    ]
    result = ct.build_fixed_member_panel(
        rows,
        ["A", "B"],
        start_quarter="2020Q1",
        end_quarter="2021Q1",
    )
    levels = {row["calendar_quarter"]: row for row in result["levels"]}
    yoy = {row["calendar_quarter"]: row for row in result["yoy"]}

    assert levels["2020Q1"]["value_usd"] is None
    assert levels["2020Q1"]["member_reasons"] == {"B": "missing_row"}
    assert levels["2021Q1"]["complete"] is True
    assert yoy["2021Q1"]["yoy"] is None
    assert yoy["2021Q1"]["status"] == "base_incomplete"
    assert yoy["2021Q1"]["missing_member_reasons"]["base"] == {
        "B": "missing_row"
    }


def test_duplicate_member_quarter_rows_are_invalid_not_selected() -> None:
    result = ct.build_fixed_member_panel(
        [
            member_row("A", "2020Q1", 10),
            member_row("A", "2020Q1", 11),
            member_row("B", "2020Q1", 20),
        ],
        ["A", "B"],
        start_quarter="2020Q1",
        end_quarter="2020Q1",
    )

    assert result["levels"][0]["value_usd"] is None
    duplicate = next(row for row in result["panel"] if row["member"] == "A")
    assert duplicate["usable"] is False
    assert duplicate["value_usd"] is None
    assert duplicate["reason"] == "duplicate_member_quarter_rows"


def test_lag_pairs_capex_t_to_revenue_t_plus_lag_without_crossing_window() -> None:
    capex = {
        "2019Q4": -10,
        "2020Q1": 1,
        "2020Q2": 2,
        "2020Q3": 3,
    }
    revenue = {
        "2020Q1": -10,
        "2020Q2": 1,
        "2020Q3": 2,
        "2020Q4": 3,
    }

    result = ct.lagged_correlation(capex, revenue, 1, "2020Q1", "2020Q3")

    assert [(row["capex_quarter"], row["revenue_quarter"]) for row in result["pairs"]] == [
        ("2020Q1", "2020Q2"),
        ("2020Q2", "2020Q3"),
    ]
    assert [(row["x"], row["y"]) for row in result["pairs"]] == [(1, 1), (2, 2)]
    assert result["r"] == pytest.approx(1)


@pytest.mark.parametrize(
    ("n", "status", "eligible"),
    [
        (11, "insufficient_n", False),
        (12, "descriptive_only", False),
        (31, "descriptive_only", False),
        (32, "inferential_eligible", True),
    ],
)
def test_lagged_correlation_n_thresholds(n: int, status: str, eligible: bool) -> None:
    start = "2010Q1"
    end = ct.shift_quarter(start, n - 1)
    quarters = ct.quarter_range(start, end)
    capex = {quarter: float(index) for index, quarter in enumerate(quarters)}
    revenue = {quarter: float(index * 2 + 1) for index, quarter in enumerate(quarters)}

    result = ct.lagged_correlation(capex, revenue, 0, start, end)

    assert result["n"] == n
    assert result["status"] == status
    assert result["inferential_eligible"] is eligible


def test_lagged_correlation_rejects_zero_variance() -> None:
    quarters = ct.quarter_range("2010Q1", "2017Q4")
    result = ct.lagged_correlation(
        {quarter: 1.0 for quarter in quarters},
        {quarter: float(index) for index, quarter in enumerate(quarters)},
        0,
        quarters[0],
        quarters[-1],
    )

    assert result["n"] == 32
    assert result["r"] is None
    assert result["status"] == "zero_variance"
    assert result["inferential_eligible"] is False
    assert result["effective_sample_size"]["status"] == "zero_variance"


def test_holm_uses_fixed_family_and_assigns_ineligible_or_missing_p_one() -> None:
    hypotheses = [
        {"bootstrap_p": 0.01, "inferential_eligible": True},
        {"bootstrap_p": 0.001, "inferential_eligible": False},
        {"bootstrap_p": 0.03, "inferential_eligible": True},
        {"bootstrap_p": None, "inferential_eligible": True},
    ]

    corrected = ct.holm_correct_family(hypotheses)

    assert [row["holm_input_p"] for row in corrected] == [0.01, 1.0, 0.03, 1.0]
    assert [row["holm_adjusted_p"] for row in corrected] == pytest.approx(
        [0.04, 1.0, 0.09, 1.0]
    )


def test_support_is_positive_only_and_equal_correlations_choose_shorter_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deterministic_bootstrap(cells: list[dict[str, Any]], **kwargs: Any) -> None:
        del kwargs
        for cell in cells:
            cell.update(
                {
                    "bootstrap_p": 0.001,
                    "bootstrap_ci_95": [-1.0, 1.0],
                    "bootstrap_draws": 1,
                    "bootstrap_valid_draws": 1,
                    "bootstrap_status": "ok",
                }
            )

    monkeypatch.setattr(ct, "_attach_bootstrap_to_cells", deterministic_bootstrap)
    quarters = ct.quarter_range("2010Q1", "2019Q4")
    alternating = {
        quarter: float(1 if index % 2 else -1)
        for index, quarter in enumerate(quarters)
    }

    grid = ct.full_lag_grid(
        alternating,
        {"Layer": alternating},
        {"full": (quarters[0], quarters[-1])},
        max_lag=2,
        draws=1,
    )
    by_lag = {cell["lag"]: cell for cell in grid["cells"]}

    assert by_lag[0]["r"] == by_lag[2]["r"] == 1.0
    assert by_lag[0]["supports_transmission"] is True
    assert by_lag[2]["supports_transmission"] is True
    assert by_lag[1]["r"] == -1.0
    assert by_lag[1]["holm_adjusted_p"] < 0.05
    assert by_lag[1]["supports_transmission"] is False
    assert by_lag[1]["support_status"] == "negative_or_zero"
    assert grid["layers"][0]["windows"][0]["best_supported_lag"] == 0


def test_stationary_bootstrap_paths_and_results_are_deterministic_and_blocked() -> None:
    paths = ct.stationary_bootstrap_paths(
        16, draws=31, expected_block_length=3, seed=73
    )
    repeated = ct.stationary_bootstrap_paths(
        16, draws=31, expected_block_length=3, seed=73
    )

    assert paths == repeated
    assert paths["x_paths"] != paths["y_paths"]
    assert paths["independent_null_paths"] is True
    assert paths["circular"] is True
    assert any(len(set(path)) < 16 for path in paths["x_paths"])
    assert any(
        right == (left + 1) % 16
        for path in paths["x_paths"]
        for left, right in zip(path, path[1:])
    )

    xs = [math.sin(index / 2) + index / 100 for index in range(16)]
    ys = [0.7 * value + 0.2 * math.cos(index) for index, value in enumerate(xs)]
    first = ct.stationary_bootstrap_test(xs, ys, draws=31, paths=paths)
    second = ct.stationary_bootstrap_test(xs, ys, draws=31, paths=paths)
    assert first == second
    assert first["status"] == "descriptive_only"
    assert first["valid_null_draws"] > 0
    assert 0 < first["p_value"] <= 1


def test_calendar_gap_produces_segments_and_paths_never_cross_them() -> None:
    capex_quarters = ct.quarter_range("2010Q1", "2011Q1") + ct.quarter_range(
        "2011Q4", "2013Q2"
    )
    capex = {
        quarter: math.sin(index / 2) + index / 10
        for index, quarter in enumerate(capex_quarters)
    }
    revenue = {
        ct.shift_quarter(quarter, 1): value * 0.8 + index / 20
        for index, (quarter, value) in enumerate(capex.items())
    }

    cell = ct.lagged_correlation(
        capex, revenue, 1, "2010Q1", ct.shift_quarter(capex_quarters[-1], 1)
    )

    assert cell["segment_lengths"] == [5, 7]
    assert cell["segment_count"] == 2
    assert cell["segment_boundaries"] == [
        {
            "start_capex_quarter": "2010Q1",
            "end_capex_quarter": "2011Q1",
            "start_revenue_quarter": "2010Q2",
            "end_revenue_quarter": "2011Q2",
        },
        {
            "start_capex_quarter": "2011Q4",
            "end_capex_quarter": "2013Q2",
            "start_revenue_quarter": "2012Q1",
            "end_revenue_quarter": "2013Q3",
        },
    ]

    paths = ct.stationary_bootstrap_paths(
        cell["n"], draws=31, seed=73, segment_lengths=cell["segment_lengths"]
    )
    assert paths["segment_lengths"] == [5, 7]
    assert paths["segment_count"] == 2
    assert paths["expected_block_length"] == ct.DEFAULT_BLOCK_LENGTH == 8.0
    for path_name in ("x_paths", "y_paths", "paired_paths"):
        for path in paths[path_name]:
            assert all(0 <= position < 5 for position in path[:5])
            assert all(5 <= position < 12 for position in path[5:])

    invalid = copy.deepcopy(paths)
    invalid["x_paths"][0][0] = 5
    with pytest.raises(ValueError, match="crosses a segment boundary"):
        ct.stationary_bootstrap_test(
            [row["x"] for row in cell["pairs"]],
            [row["y"] for row in cell["pairs"]],
            draws=1,
            paths=invalid,
            segment_lengths=cell["segment_lengths"],
        )


def test_gapped_and_compressed_bootstraps_differ_with_fixed_seed() -> None:
    xs = [math.sin(index / 2) + index / 100 for index in range(16)]
    ys = [0.7 * value + 0.2 * math.cos(index) for index, value in enumerate(xs)]

    compressed = ct.stationary_bootstrap_test(xs, ys, draws=127, seed=73)
    gapped = ct.stationary_bootstrap_test(
        xs, ys, draws=127, seed=73, segment_lengths=[8, 8]
    )

    assert compressed["segment_lengths"] == [16]
    assert gapped["segment_lengths"] == [8, 8]
    assert (compressed["p_value"], compressed["ci_95"]) != (
        gapped["p_value"],
        gapped["ci_95"],
    )


def test_equal_n_different_gap_shapes_use_separate_precomputed_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarters = ct.quarter_range("2010Q1", "2013Q2")
    capex = {
        quarter: math.sin(index / 2) + index / 10
        for index, quarter in enumerate(quarters)
    }
    layer_a = {
        quarter: value for index, (quarter, value) in enumerate(capex.items())
        if index not in (6, 7)
    }
    layer_b = {
        quarter: value for index, (quarter, value) in enumerate(capex.items())
        if index not in (4, 5)
    }
    precomputed = ct.precompute_stationary_bootstrap_paths(
        [[6, 6], [4, 8]], draws=7, seed=73
    )
    calls: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    original = ct._bootstrap_many

    def recording_bootstrap(
        datasets: list[tuple[Any, Any, float, tuple[int, ...]]], **kwargs: Any
    ) -> list[dict[str, Any]]:
        dataset_shape = tuple(datasets[0][3])
        path_shape = tuple(kwargs["paths"]["segment_lengths"])
        calls.append((dataset_shape, path_shape))
        return original(datasets, **kwargs)

    monkeypatch.setattr(ct, "_bootstrap_many", recording_bootstrap)
    grid = ct.full_lag_grid(
        capex,
        {"A": layer_a, "B": layer_b},
        {"full": (quarters[0], quarters[-1])},
        max_lag=0,
        draws=7,
        seed=73,
        precomputed_paths=precomputed,
    )

    assert {
        cell["layer"]: cell["segment_lengths"] for cell in grid["cells"]
    } == {"A": [6, 6], "B": [4, 8]}
    assert set(calls) == {((6, 6), (6, 6)), ((4, 8), (4, 8))}
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("n", "revenue_sign", "summary_status", "support_status"),
    [
        (32, 1, "not_significant", "not_significant"),
        (31, 1, "descriptive_only", "ineligible"),
        (11, 1, "not_assessable", "ineligible"),
        (20, -1, "not_assessable", "ineligible"),
    ],
)
def test_grid_summary_and_support_statuses(
    n: int,
    revenue_sign: int,
    summary_status: str,
    support_status: str,
) -> None:
    quarters = ct.quarter_range("2010Q1", ct.shift_quarter("2010Q1", n - 1))
    capex = {quarter: float(index) for index, quarter in enumerate(quarters)}
    revenue = {
        quarter: float(revenue_sign * index)
        for index, quarter in enumerate(quarters)
    }

    grid = ct.full_lag_grid(
        capex,
        {"Layer": revenue},
        {"full": (quarters[0], quarters[-1])},
        max_lag=0,
        draws=0,
    )

    assert grid["layers"][0]["windows"][0]["status"] == summary_status
    assert grid["cells"][0]["support_status"] == support_status


def grid_summary(
    *,
    full_lag: int = 2,
    early_lag: int = 2,
    late_lag: int = 2,
) -> dict[str, object]:
    return {
        "layers": [
            {
                "layer": "Layer",
                "windows": [
                    {"window": "full", "best_supported_lag": full_lag},
                    {
                        "window": "early",
                        "descriptive_peak": {"lag": early_lag, "n": 16, "r": 0.5},
                    },
                    {
                        "window": "late",
                        "descriptive_peak": {"lag": late_lag, "n": 16, "r": 0.4},
                    },
                ],
            }
        ]
    }


def test_split_consistency_requires_the_exact_full_supported_lag() -> None:
    exact = ct.assess_split_consistency(grid_summary())
    mismatch = ct.assess_split_consistency(grid_summary(late_lag=3))

    assert exact["all_pass"] is True
    assert exact["layers"][0]["pass"] is True
    assert mismatch["all_pass"] is False
    assert mismatch["layers"][0]["status"] == "fail"


def test_detrended_robustness_tests_the_raw_exact_lag_without_substitution() -> None:
    raw = grid_summary()
    detrended = {
        "cells": [
            {
                "layer": "Layer",
                "window": "full",
                "lag": 1,
                "n": 40,
                "r": 0.9,
                "holm_adjusted_p": 0.001,
                "inferential_eligible": True,
            },
            {
                "layer": "Layer",
                "window": "full",
                "lag": 2,
                "n": 40,
                "r": 0.3,
                "holm_adjusted_p": 0.04,
                "inferential_eligible": True,
            },
        ]
    }

    robust = ct.assess_trend_robustness(raw, detrended)
    assert robust["all_pass"] is True
    assert robust["layers"][0]["raw_supported_lag"] == 2

    detrended["cells"][1]["holm_adjusted_p"] = 0.05
    not_robust = ct.assess_trend_robustness(raw, detrended)
    assert not_robust["all_pass"] is False
    assert not_robust["layers"][0]["detrended_holm_adjusted_p"] == 0.05


def test_cycle_diagnostic_counts_complete_alternating_trough_cycles() -> None:
    quarters = ct.quarter_range("2010Q1", "2022Q4")
    capex = {
        quarter: math.sin(2 * math.pi * index / 12)
        for index, quarter in enumerate(quarters)
    }

    diagnostic = ct.effective_cycle_diagnostics(capex)

    assert diagnostic["status"] == "ok"
    assert diagnostic["cycle_count"] == 3
    assert [cycle["length_quarters"] for cycle in diagnostic["cycles"]] == [12, 12, 12]
    assert diagnostic["cycle_boundaries"] == [
        {"start_trough": "2012Q4", "end_trough": "2015Q4"},
        {"start_trough": "2015Q4", "end_trough": "2018Q4"},
        {"start_trough": "2018Q4", "end_trough": "2021Q4"},
    ]


def test_cycle_extrema_and_cycles_are_segment_local() -> None:
    first = ct.quarter_range("2000Q1", "2006Q4")
    second = ct.quarter_range("2012Q1", "2018Q4")
    capex = {
        quarter: math.sin(2 * math.pi * index / 12)
        for index, quarter in enumerate(first)
    }
    capex.update(
        {
            quarter: 2 * math.sin(2 * math.pi * index / 12)
            for index, quarter in enumerate(second)
        }
    )

    diagnostic = ct.effective_cycle_diagnostics(capex)

    assert diagnostic["segment_count"] == 2
    assert diagnostic["segment_boundaries"] == [
        {
            "segment": 1,
            "start_quarter": "2000Q4",
            "end_quarter": "2006Q4",
            "length_quarters": 25,
        },
        {
            "segment": 2,
            "start_quarter": "2012Q4",
            "end_quarter": "2018Q4",
            "length_quarters": 25,
        },
    ]
    assert [
        (extremum["quarter"], extremum["segment"])
        for extremum in diagnostic["extrema"]
        if extremum["type"] == "trough"
    ] == [
        ("2002Q4", 1),
        ("2005Q4", 1),
        ("2014Q4", 2),
        ("2017Q4", 2),
    ]
    assert [
        (cycle["start_trough"], cycle["end_trough"], cycle["segment"])
        for cycle in diagnostic["cycles"]
    ] == [
        ("2002Q4", "2005Q4", 1),
        ("2014Q4", "2017Q4", 2),
    ]


def decision_grids() -> tuple[dict[str, object], dict[str, object]]:
    layers = []
    detrended_cells = []
    for name in ("Foundry", "Memory", "Equipment"):
        layers.append(
            {
                "layer": name,
                "is_control": False,
                "windows": [
                    {"window": "full", "best_supported_lag": 2},
                    {
                        "window": "early",
                        "descriptive_peak": {"lag": 2, "n": 16, "r": 0.4},
                    },
                    {
                        "window": "late",
                        "descriptive_peak": {"lag": 2, "n": 16, "r": 0.3},
                    },
                ],
            }
        )
        detrended_cells.append(
            {
                "layer": name,
                "window": "full",
                "lag": 2,
                "n": 40,
                "r": 0.25,
                "holm_adjusted_p": 0.04,
                "inferential_eligible": True,
            }
        )
    layers.append(
        {
            "layer": "TSMC",
            "is_control": True,
            "windows": [
                {"window": "full", "best_supported_lag": None},
                {"window": "early", "descriptive_peak": None},
                {"window": "late", "descriptive_peak": None},
            ],
        }
    )
    raw = {
        "layers": layers,
        "cells": [
            {
                "layer": "TSMC",
                "window": "full",
                "lag": 0,
                "n": 40,
                "r": -0.2,
                "bootstrap_p": 0.3,
                "holm_adjusted_p": 0.3,
                "inferential_eligible": True,
            },
            {
                "layer": "TSMC",
                "window": "full",
                "lag": 8,
                "n": 29,
                "r": 0.7,
                "bootstrap_p": 0.01,
                "holm_adjusted_p": 1.0,
                "inferential_eligible": False,
            },
        ],
    }
    return raw, {"cells": detrended_cells}


def test_four_criteria_decision_and_positive_significant_tsmc_control_failure() -> None:
    raw, detrended = decision_grids()
    passing = ct.transmission_decision(raw, detrended)

    assert passing["pass"] is True
    assert all(criterion["pass"] for criterion in passing["criteria"].values())

    failed_raw = copy.deepcopy(raw)
    control = failed_raw["cells"][0]
    control["r"] = 0.4
    control["bootstrap_p"] = 0.01
    control["holm_adjusted_p"] = 0.01
    failed = ct.transmission_decision(failed_raw, detrended)

    assert failed["pass"] is False
    assert failed["proceed_to_strategy"] is False
    assert failed["criteria"]["tsmc_control_no_positive_significant_lag"] == {
        "pass": False,
        "assessable": True,
        "control_layer": "TSMC",
        "positive_significant_lags": [0],
    }
    assert all(
        failed["criteria"][name]["pass"]
        for name in (
            "at_least_three_noncontrol_layers_supported",
            "exact_split_consistency",
            "exact_lag_detrended_significance",
        )
    )
    assert failed["conclusion"] == "傳導關係不穩定,不進入策略設計階段"
    assert failed["conclusion"] == ct.FAIL_CONCLUSION


def test_tsmc_control_is_not_assessable_when_every_lag_is_ineligible() -> None:
    raw, detrended = decision_grids()
    for cell in raw["cells"]:
        cell["inferential_eligible"] = False

    result = ct.transmission_decision(raw, detrended)

    assert result["criteria"]["tsmc_control_no_positive_significant_lag"] == {
        "pass": False,
        "assessable": False,
        "control_layer": "TSMC",
        "positive_significant_lags": [],
    }


def test_supported_layer_count_is_not_assessable_without_eligible_cells() -> None:
    raw, detrended = decision_grids()
    for cell in raw["cells"]:
        cell["inferential_eligible"] = False
    for layer in raw["layers"]:
        if not layer.get("is_control"):
            layer["windows"][0]["best_supported_lag"] = None

    result = ct.transmission_decision(raw, detrended)

    assert result["criteria"]["at_least_three_noncontrol_layers_supported"] == {
        "pass": False,
        "assessable": False,
        "count": 0,
        "required": 3,
    }
