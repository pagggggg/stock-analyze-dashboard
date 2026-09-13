from __future__ import annotations

import pytest

from sources_sec import (
    calendar_quarter_for_period,
    deduplicate_quarters,
    duration_facts_to_standalone_quarters,
    extract_duration_facts,
    parse_asml_gaap_quarterly_revenue,
)


TAG = "PaymentsToAcquirePropertyPlantAndEquipment"
SOURCE_URL = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"
)


def sec_entry(
    start: str,
    end: str,
    value: int,
    *,
    fp: str,
    form: str,
    filed: str,
    accession: str,
    fy: int = 2024,
) -> dict[str, object]:
    return {
        "start": start,
        "end": end,
        "val": value,
        "accn": accession,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
        "frame": None,
    }


def duration_fact(
    start: str,
    end: str,
    value: int,
    *,
    fp: str,
    form: str,
    filed: str,
    accession: str,
) -> dict[str, object]:
    return {
        **sec_entry(
            start,
            end,
            value,
            fp=fp,
            form=form,
            filed=filed,
            accession=accession,
        ),
        "unit": "USD",
        "taxonomy": "us-gaap",
        "tag": TAG,
        "cik": "0000789019",
        "source_url": SOURCE_URL,
    }


def test_microsoft_like_fiscal_year_derives_standalone_quarters_and_metadata() -> None:
    entries = [
        sec_entry(
            "2023-07-01",
            "2023-09-30",
            100,
            fp="Q1",
            form="10-Q",
            filed="2023-10-24",
            accession="q1-original",
        ),
        sec_entry(
            "2023-07-01",
            "2023-09-30",
            900,
            fp="Q1",
            form="10-Q/A",
            filed="2023-11-03",
            accession="q1-amended",
        ),
        sec_entry(
            "2023-07-01",
            "2023-09-30",
            800,
            fp="Q1",
            form="10-Q",
            filed="2024-10-30",
            accession="q1-later-comparative",
            fy=2025,
        ),
        sec_entry(
            "2023-07-01",
            "2023-12-31",
            250,
            fp="Q2",
            form="10-Q",
            filed="2024-01-30",
            accession="q2-original",
        ),
        sec_entry(
            "2023-07-01",
            "2023-12-31",
            999,
            fp="Q2",
            form="10-Q",
            filed="2025-01-29",
            accession="q2-later-comparative",
            fy=2025,
        ),
        sec_entry(
            "2023-07-01",
            "2024-03-31",
            450,
            fp="Q3",
            form="10-Q",
            filed="2024-04-25",
            accession="q3-original",
        ),
        sec_entry(
            "2023-07-01",
            "2024-06-30",
            700,
            fp="FY",
            form="10-K",
            filed="2024-07-30",
            accession="fy-original",
        ),
    ]
    companyfacts = {
        "facts": {"us-gaap": {TAG: {"units": {"USD": entries}}}}
    }

    facts = extract_duration_facts(
        companyfacts, "us-gaap", TAG, "USD", cik="789019"
    )
    expected_metadata = {
        "taxonomy": "us-gaap",
        "tag": TAG,
        "unit": "USD",
        "cik": "0000789019",
        "source_url": SOURCE_URL,
        "accn": "q1-original",
        "filed": "2023-10-24",
    }
    assert {key: facts[0][key] for key in expected_metadata} == expected_metadata

    quarters = duration_facts_to_standalone_quarters(facts)
    assert [row["calendar_quarter"] for row in quarters] == [
        "2023Q3",
        "2023Q4",
        "2024Q1",
        "2024Q2",
    ]
    assert [row["fiscal_quarter"] for row in quarters] == ["Q1", "Q2", "Q3", "Q4"]
    assert [row["value"] for row in quarters] == [100, 150, 200, 250]
    assert [row["derivation"] for row in quarters] == [
        "reported_quarter",
        "ytd_difference",
        "ytd_difference",
        "fy_difference",
    ]

    q1, q2, _, q4 = quarters
    assert (q1["filed"], q1["accn"]) == ("2023-10-24", "q1-original")
    assert q2["component_accessions"] == ["q2-original", "q1-original"]
    assert q2["component_filing_dates"] == ["2024-01-30", "2023-10-24"]
    assert [component["role"] for component in q2["components"]] == [
        "minuend",
        "subtrahend",
    ]
    assert q4["source_accessions"] == ["fy-original", "q3-original"]
    assert q4["source_tags"] == [TAG]
    assert q4["source_urls"] == [SOURCE_URL]
    assert all(row["tag"] == TAG and row["source_url"] == SOURCE_URL for row in quarters)

    used_accessions = {
        accession
        for row in quarters
        for accession in row["component_accessions"]
    }
    assert "q1-amended" not in used_accessions
    assert "q1-later-comparative" not in used_accessions
    assert "q2-later-comparative" not in used_accessions


@pytest.mark.parametrize(
    ("start", "q3_end", "fy_end", "expected_start", "expected_days"),
    [
        ("2023-07-02", "2024-03-30", "2024-06-29", "2024-03-31", 91),
        ("2024-06-30", "2025-03-29", "2025-07-05", "2025-03-30", 98),
    ],
)
def test_52_and_53_week_fiscal_years_derive_q4(
    start: str,
    q3_end: str,
    fy_end: str,
    expected_start: str,
    expected_days: int,
) -> None:
    facts = [
        duration_fact(
            start,
            q3_end,
            300,
            fp="Q3",
            form="10-Q",
            filed="2025-04-20",
            accession="q3",
        ),
        duration_fact(
            start,
            fy_end,
            425,
            fp="FY",
            form="10-K",
            filed="2025-08-01",
            accession="fy",
        ),
    ]

    assert (date_span := duration_facts_to_standalone_quarters(facts))
    assert len(date_span) == 1
    q4 = date_span[0]
    assert q4["derivation"] == "fy_difference"
    assert q4["value"] == 125
    assert q4["period_start"] == expected_start
    assert q4["period_end"] == fy_end
    from datetime import date

    assert (date.fromisoformat(fy_end) - date.fromisoformat(expected_start)).days + 1 == expected_days


def test_negative_ytd_and_fy_differences_are_not_emitted() -> None:
    facts = [
        duration_fact(
            "2023-01-01",
            "2023-03-31",
            100,
            fp="Q1",
            form="10-Q",
            filed="2023-04-20",
            accession="q1",
        ),
        duration_fact(
            "2023-01-01",
            "2023-06-30",
            90,
            fp="Q2",
            form="10-Q",
            filed="2023-07-20",
            accession="q2-ytd",
        ),
        duration_fact(
            "2023-01-01",
            "2023-09-30",
            300,
            fp="Q3",
            form="10-Q",
            filed="2023-10-20",
            accession="q3-ytd",
        ),
        duration_fact(
            "2023-01-01",
            "2023-12-31",
            250,
            fp="FY",
            form="10-K",
            filed="2024-02-20",
            accession="fy",
        ),
    ]

    quarters = duration_facts_to_standalone_quarters(facts)
    assert [(row["calendar_quarter"], row["value"]) for row in quarters] == [
        ("2023Q1", 100),
        ("2023Q3", 210),
    ]


def test_periods_are_mapped_by_midpoint_not_end_date() -> None:
    assert calendar_quarter_for_period("2023-08-01", "2023-10-31") == "2023Q3"


def test_configured_alias_tags_can_derive_q4_without_interpolation() -> None:
    q3 = duration_fact(
        "2020-01-27",
        "2020-10-25",
        300,
        fp="Q3",
        form="10-Q",
        filed="2020-11-18",
        accession="q3",
    )
    q3["tag"] = "Revenues"
    q3["_metric_group"] = "revenue"
    annual = duration_fact(
        "2020-01-27",
        "2021-01-31",
        450,
        fp="FY",
        form="10-K",
        filed="2021-02-26",
        accession="fy",
    )
    annual["tag"] = "RevenueFromContractWithCustomerExcludingAssessedTax"
    annual["_metric_group"] = "revenue"

    rows = duration_facts_to_standalone_quarters([q3, annual])

    assert len(rows) == 1
    assert rows[0]["calendar_quarter"] == "2020Q4"
    assert rows[0]["value"] == 150
    assert rows[0]["derivation"] == "fy_difference"
    assert rows[0]["source_tags"] == [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
    ]


def quarter_candidate(
    *,
    cik: str,
    filed: str,
    source_priority: int,
    tag_priority: int,
    derivation: str,
    value: int,
) -> dict[str, object]:
    return {
        "calendar_quarter": "2018Q2",
        "cik": cik,
        "filed": filed,
        "source_priority": source_priority,
        "tag_priority": tag_priority,
        "derivation": derivation,
        "value": value,
    }


def test_calendar_dedup_prefers_earliest_effective_filing_across_ciks() -> None:
    old_cik_derived = quarter_candidate(
        cik="0001441634",
        filed="2018-09-01",
        source_priority=2,
        tag_priority=2,
        derivation="ytd_difference",
        value=100,
    )
    new_cik_comparative = quarter_candidate(
        cik="0001730168",
        filed="2020-03-01",
        source_priority=0,
        tag_priority=0,
        derivation="reported_quarter",
        value=999,
    )

    assert deduplicate_quarters(
        [new_cik_comparative, old_cik_derived]
    )[0]["value"] == 100


def test_calendar_dedup_uses_priorities_before_direct_tie_breaker() -> None:
    derived_primary_tag = quarter_candidate(
        cik="0001730168",
        filed="2018-09-01",
        source_priority=0,
        tag_priority=0,
        derivation="ytd_difference",
        value=100,
    )
    direct_secondary_tag = quarter_candidate(
        cik="0001730168",
        filed="2018-09-01",
        source_priority=0,
        tag_priority=1,
        derivation="reported_quarter",
        value=200,
    )
    direct_same_priority = quarter_candidate(
        cik="0001730168",
        filed="2018-09-01",
        source_priority=0,
        tag_priority=0,
        derivation="reported_quarter",
        value=300,
    )

    assert deduplicate_quarters(
        [direct_secondary_tag, derived_primary_tag]
    )[0]["value"] == 100
    assert deduplicate_quarters(
        [derived_primary_tag, direct_same_priority]
    )[0]["value"] == 300


def test_earlier_derived_quarter_beats_later_comparative_direct_fact() -> None:
    facts = [
        duration_fact(
            "2018-01-01",
            "2018-03-31",
            100,
            fp="Q1",
            form="10-Q",
            filed="2018-04-20",
            accession="q1-ytd",
        ),
        duration_fact(
            "2018-01-01",
            "2018-06-30",
            250,
            fp="Q2",
            form="10-Q",
            filed="2018-07-20",
            accession="q2-ytd",
        ),
        duration_fact(
            "2018-04-01",
            "2018-06-30",
            999,
            fp="Q2",
            form="10-Q",
            filed="2019-07-20",
            accession="later-comparative-direct",
        ),
    ]

    rows = duration_facts_to_standalone_quarters(facts)
    q2 = next(row for row in rows if row["calendar_quarter"] == "2018Q2")

    assert q2["value"] == 150
    assert q2["derivation"] == "ytd_difference"
    assert q2["filed"] == "2018-07-20"
    assert "later-comparative-direct" not in q2["component_accessions"]


def test_asml_us_gaap_exhibit_parser_reads_current_eur_millions_column() -> None:
    html = """
    <html><body>
      <h1>Summary U.S. GAAP Consolidated Statements of Operations</h1>
      <table>
        <tr><th>EUR (in millions)</th><th colspan="2">Three months ended June 30</th></tr>
        <tr><th></th><th>2024</th><th>2023</th></tr>
        <tr><td>Total net sales</td><td>6,243.1</td><td>6,902.0</td></tr>
      </table>
    </body></html>
    """

    assert parse_asml_gaap_quarterly_revenue(html, "2024Q2") == 6_243_100_000
    assert parse_asml_gaap_quarterly_revenue(html, "2023Q2") == 6_902_000_000
    assert parse_asml_gaap_quarterly_revenue(html, "2022Q2") is None


def test_asml_slide_text_parser_reads_newer_image_based_exhibit() -> None:
    html = """
    <html><body>
      <div>Summary US GAAP Consolidated Statements of Operations</div>
      <div>Three months ended Six months ended Jun 30, Jun 29, Jun 30, Jun 29,
      (unaudited, in millions EUR, except per share data) 2024 2025 2024 2025
      Net system sales 4,760.9 5,596.1 8,726.8 11,336.5
      Net service and field option sales 1,481.9 2,095.6 2,806.0 4,096.7
      Total net sales 6,242.8 7,691.7 11,532.8 15,433.2
      Total cost of sales (3,030.6) (3,562.2) (5,624.0) (7,124.0)</div>
    </body></html>
    """

    assert parse_asml_gaap_quarterly_revenue(html, "2025Q2") == 7_691_700_000
    assert parse_asml_gaap_quarterly_revenue(html, "2024Q2") == 6_242_800_000
