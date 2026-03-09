"""Unit tests for ingestion/llm_comp_extractor.py.

All OpenAI API calls are mocked. No network requests are made.
Tests cover: valid extraction, retry on bad JSON, retry success,
double failure fallback, empty table_text guard, confidence clamping.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ingestion.llm_comp_extractor import (
    CompanyCompResult,
    extract_company_comp_from_summary_table,
    extract_grants_from_plan_based_table,
)

VALID_RESPONSE = {
    "ceo": {
        "name": "Jane Smith",
        "title": "Chief Executive Officer",
        "salary": "1250000",
        "bonus": "500000",
        "stock_awards": "2000000",
        "option_awards": None,
        "total": "3850000",
        "fiscal_year": "2023",
    },
    "cfo": {
        "name": "Bob Lee",
        "title": "Chief Financial Officer",
        "salary": "800000",
        "bonus": None,
        "stock_awards": None,
        "option_awards": None,
        "total": "900000",
        "fiscal_year": "2023",
    },
    "coo": {
        "name": "",
        "title": "",
        "salary": None,
        "bonus": None,
        "stock_awards": None,
        "option_awards": None,
        "total": None,
        "fiscal_year": "",
    },
    "other1": {
        "name": "",
        "title": "",
        "salary": None,
        "bonus": None,
        "stock_awards": None,
        "option_awards": None,
        "total": None,
        "fiscal_year": "",
    },
    "other2": {
        "name": "",
        "title": "",
        "salary": None,
        "bonus": None,
        "stock_awards": None,
        "option_awards": None,
        "total": None,
        "fiscal_year": "",
    },
    "confidence": 0.92,
    "notes": "Clean extraction from standard table.",
}

VALID_GRANTS_RESPONSE = {
    "rows": [
        {
            "name": "Jane Smith",
            "grant_type": "Performance Restricted Stock Units (PRSU)",
            "grant_date": "2024-03-01",
            "non_equity_threshold": "100000",
            "non_equity_target": "200000",
            "non_equity_maximum": "300000",
            "equity_threshold": "1000",
            "equity_target": "2000",
            "equity_maximum": "3000",
            "all_other_stock_awards_shares": "500",
            "all_other_option_awards_securities": None,
            "exercise_or_base_price": None,
            "grant_date_fair_value": "400000",
        }
    ],
    "confidence": 0.88,
    "notes": "Structured grants extracted.",
}


def _mock_client(responses: list[str]) -> MagicMock:
    """Build a mock OpenAI client returning sequential responses."""
    client = MagicMock()
    choices = [MagicMock(message=MagicMock(content=response)) for response in responses]
    client.chat.completions.create.side_effect = [MagicMock(choices=[choice]) for choice in choices]
    return client


class TestExtractCompanyComp:
    def test_valid_response_returns_correct_model(self) -> None:
        client = _mock_client([json.dumps(VALID_RESPONSE)])
        result = extract_company_comp_from_summary_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2023-04-15",
            accession_number="0001234567-23-000001",
            table_text="Name | Year | Salary | Total\nJane Smith CEO | 2023 | 1250000 | 3850000",
            client=client,
        )
        assert isinstance(result, CompanyCompResult)
        assert result.ceo.name == "Jane Smith"
        assert result.ceo.salary == "1250000"
        assert result.ceo.total == "3850000"
        assert result.confidence == pytest.approx(0.92)

    def test_invalid_json_triggers_retry(self) -> None:
        """First response is not JSON; second is valid. Retry must succeed."""
        client = _mock_client(["not json at all", json.dumps(VALID_RESPONSE)])
        result = extract_company_comp_from_summary_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2023-04-15",
            accession_number="0001234567-23-000001",
            table_text="Name | Salary\nJane CEO | 1250000",
            client=client,
        )
        assert result.ceo.name == "Jane Smith"
        assert client.chat.completions.create.call_count == 2

    def test_double_failure_returns_empty_result(self) -> None:
        """Both attempts return invalid JSON; must return empty CompanyCompResult."""
        client = _mock_client(["bad json 1", "bad json 2"])
        result = extract_company_comp_from_summary_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2023-04-15",
            accession_number="0001234567-23-000001",
            table_text="Name | Salary\nJane CEO | 1250000",
            client=client,
        )
        assert isinstance(result, CompanyCompResult)
        assert result.ceo.name == ""
        assert result.confidence == 0.0

    def test_empty_table_text_returns_empty_result_without_api_call(self) -> None:
        """Empty table_text must return CompanyCompResult() without any API call."""
        client = _mock_client([])
        result = extract_company_comp_from_summary_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2023-04-15",
            accession_number="0001234567-23-000001",
            table_text="",
            client=client,
        )
        assert result.confidence == 0.0
        assert client.chat.completions.create.call_count == 0

    def test_api_error_returns_empty_result(self) -> None:
        """OpenAI API error must not propagate; return empty CompanyCompResult."""
        from openai import OpenAIError

        client = MagicMock()
        client.chat.completions.create.side_effect = OpenAIError("rate limit")
        result = extract_company_comp_from_summary_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2023-04-15",
            accession_number="0001234567-23-000001",
            table_text="Name | Salary\nJane CEO | 1250000",
            client=client,
        )
        assert result.confidence == 0.0

    def test_confidence_clamped_above_one(self) -> None:
        """LLM confidence > 1.0 must be clamped to 1.0 by model_validator."""
        bad_response = {**VALID_RESPONSE, "confidence": 1.5}
        client = _mock_client([json.dumps(bad_response)])
        result = extract_company_comp_from_summary_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2023-04-15",
            accession_number="0001234567-23-000001",
            table_text="Name | Salary\nJane CEO | 1250000",
            client=client,
        )
        assert result.confidence <= 1.0

    def test_cfo_fields_populated(self) -> None:
        client = _mock_client([json.dumps(VALID_RESPONSE)])
        result = extract_company_comp_from_summary_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2023-04-15",
            accession_number="0001234567-23-000001",
            table_text="Name | Salary\nJane CEO | 1250000 | Bob CFO | 800000",
            client=client,
        )
        assert result.cfo.name == "Bob Lee"
        assert result.cfo.total == "900000"


class TestExtractGrants:
    def test_valid_grants_response_returns_rows(self) -> None:
        client = _mock_client([json.dumps(VALID_GRANTS_RESPONSE)])
        result = extract_grants_from_plan_based_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2024-04-15",
            accession_number="0001234567-24-000001",
            table_text="Name | Grant Date | Threshold | Target | Maximum",
            client=client,
        )
        assert len(result.rows) == 1
        assert result.rows[0].name == "Jane Smith"
        assert result.rows[0].grant_type == "Performance Restricted Stock Units (PRSU)"
        assert result.confidence == pytest.approx(0.88)

    def test_invalid_json_triggers_retry_for_grants(self) -> None:
        client = _mock_client(["not json", json.dumps(VALID_GRANTS_RESPONSE)])
        result = extract_grants_from_plan_based_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2024-04-15",
            accession_number="0001234567-24-000001",
            table_text="Name | Grant Date | Threshold",
            client=client,
        )
        assert len(result.rows) == 1
        assert client.chat.completions.create.call_count == 2

    def test_empty_table_text_returns_empty_grants_result_without_api_call(self) -> None:
        client = _mock_client([])
        result = extract_grants_from_plan_based_table(
            company_name="Test Corp",
            cik="1234567",
            filing_date="2024-04-15",
            accession_number="0001234567-24-000001",
            table_text="",
            client=client,
        )
        assert result.confidence == 0.0
        assert result.rows == []
        assert client.chat.completions.create.call_count == 0
