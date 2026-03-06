"""Unit tests for Ollama fallback in llm_comp_extractor.py.

When OPENAI_API_KEY is "dummy" (as in CI), the extractor must route
to _call_ollama() instead of _call_openai(). These tests verify:
1. Ollama path is taken when OPENAI_API_KEY=dummy
2. OpenAI path is taken when OPENAI_API_KEY is a real-looking key
3. Ollama failure falls back to empty CompanyCompResult
4. Ollama retry works on bad first response
5. OLLAMA_MODEL and OLLAMA_BASE_URL env vars are respected
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from ingestion.llm_comp_extractor import (
    CompanyCompResult,
    extract_company_comp_from_summary_table,
)

VALID_RESPONSE = {
    "ceo": {
        "name": "Jane Smith",
        "title": "Chief Executive Officer",
        "salary": "1250000",
        "bonus": None,
        "stock_awards": None,
        "option_awards": None,
        "total": "3850000",
        "fiscal_year": "2023",
    },
    "cfo": {
        "name": "",
        "title": "",
        "salary": None,
        "bonus": None,
        "stock_awards": None,
        "option_awards": None,
        "total": None,
        "fiscal_year": "",
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
    "confidence": 0.88,
    "notes": "Ollama extraction.",
}

TABLE_TEXT = "Name | Year | Salary | Total\nJane Smith CEO | 2023 | 1250000 | 3850000"
CALL_KWARGS = {
    "company_name": "Test Corp",
    "cik": "1234567",
    "filing_date": "2023-04-15",
    "accession_number": "0001234567-23-000001",
    "table_text": TABLE_TEXT,
}


def _mock_ollama_client(responses: list[str]) -> MagicMock:
    """Return a mock ollama.Client that produces sequential responses."""
    mock_client_instance = MagicMock()
    mock_client_instance.chat.side_effect = [
        {"message": {"content": response}} for response in responses
    ]
    return MagicMock(return_value=mock_client_instance)


class TestOllamaRouting:
    def test_dummy_key_routes_to_ollama(self) -> None:
        """OPENAI_API_KEY=dummy must trigger Ollama path, not OpenAI."""
        mock_ollama_cls = _mock_ollama_client([json.dumps(VALID_RESPONSE)])

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "dummy"}),
            patch("ollama.Client", mock_ollama_cls),
        ):
            result = extract_company_comp_from_summary_table(**CALL_KWARGS)

        assert result.ceo.name == "Jane Smith"
        assert result.confidence == pytest.approx(0.88)
        mock_ollama_cls.return_value.chat.assert_called()

    def test_real_key_routes_to_openai_not_ollama(self) -> None:
        """A non-dummy OPENAI_API_KEY must use OpenAI, not Ollama."""
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=json.dumps(VALID_RESPONSE)))]
        )
        mock_ollama_cls = _mock_ollama_client([json.dumps(VALID_RESPONSE)])

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "sk-real-key-abc123"}),
            patch("ollama.Client", mock_ollama_cls),
        ):
            result = extract_company_comp_from_summary_table(
                **CALL_KWARGS,
                client=mock_openai_client,
            )

        assert result.ceo.name == "Jane Smith"
        mock_openai_client.chat.completions.create.assert_called()
        mock_ollama_cls.assert_not_called()

    def test_empty_key_routes_to_ollama(self) -> None:
        """Empty OPENAI_API_KEY must also trigger Ollama path."""
        mock_ollama_cls = _mock_ollama_client([json.dumps(VALID_RESPONSE)])

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": ""}),
            patch("ollama.Client", mock_ollama_cls),
        ):
            result = extract_company_comp_from_summary_table(**CALL_KWARGS)

        assert isinstance(result, CompanyCompResult)
        assert result.ceo.name == "Jane Smith"

    def test_ollama_failure_returns_empty_result(self) -> None:
        """Ollama connection error must return empty CompanyCompResult."""
        mock_ollama_cls = MagicMock()
        mock_ollama_cls.return_value.chat.side_effect = ConnectionRefusedError(
            "Ollama not running"
        )

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "dummy"}),
            patch("ollama.Client", mock_ollama_cls),
        ):
            result = extract_company_comp_from_summary_table(**CALL_KWARGS)

        assert result.confidence == 0.0
        assert result.ceo.name == ""

    def test_ollama_retry_on_bad_first_response(self) -> None:
        """Bad JSON from Ollama attempt 1 must trigger retry; retry must succeed."""
        mock_ollama_cls = _mock_ollama_client([
            "not valid json",
            json.dumps(VALID_RESPONSE),
        ])

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "dummy"}),
            patch("ollama.Client", mock_ollama_cls),
        ):
            result = extract_company_comp_from_summary_table(**CALL_KWARGS)

        assert result.ceo.name == "Jane Smith"
        assert mock_ollama_cls.return_value.chat.call_count == 2

    def test_ollama_model_env_var_respected(self) -> None:
        """OLLAMA_MODEL env var must be passed to ollama.Client.chat()."""
        mock_ollama_cls = _mock_ollama_client([json.dumps(VALID_RESPONSE)])

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "dummy", "OLLAMA_MODEL": "mistral"}),
            patch("ollama.Client", mock_ollama_cls),
        ):
            extract_company_comp_from_summary_table(**CALL_KWARGS)

        call_kwargs = mock_ollama_cls.return_value.chat.call_args.kwargs
        assert call_kwargs["model"] == "mistral"

    def test_ollama_base_url_env_var_passed_to_client(self) -> None:
        """OLLAMA_BASE_URL must be passed as host to ollama.Client constructor."""
        mock_ollama_cls = _mock_ollama_client([json.dumps(VALID_RESPONSE)])

        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "dummy",
                    "OLLAMA_BASE_URL": "http://ollama:11434",
                },
            ),
            patch("ollama.Client", mock_ollama_cls),
        ):
            extract_company_comp_from_summary_table(**CALL_KWARGS)

        mock_ollama_cls.assert_called_with(host="http://ollama:11434")
