import json
from decimal import Decimal
from pathlib import Path

import pytest
import requests
import responses

from zucai.web_research_provider import DeepSeek, ProviderError

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "t70"
URL = "https://api.deepseek.com/chat/completions"
TEST_KEY = "synthetic-credential-not-production"
MATERIAL = {"match_id": "jc:1", "jc_num": "周五001", "home": "主队", "away": "客队", "recent": []}


def fixture(stage):
    return json.loads((FIXTURES / f"provider-{stage}.synthetic.json").read_bytes())


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    return DeepSeek()


@responses.activate
def test_two_distinct_request_stages_preserve_response_evidence(provider):
    research, structure = fixture("research"), fixture("structure")
    responses.post(URL, json=research)
    responses.post(URL, json=structure)
    prompt = "提示词原文。"

    first = provider.research(prompt, MATERIAL)
    second = provider.structure(first.text, MATERIAL)

    assert len(responses.calls) == 2
    first_request, second_request = (call.request for call in responses.calls)
    first_params, second_params = (json.loads(r.body) for r in (first_request, second_request))
    assert all(
        r.headers["Authorization"] == f"Bearer {TEST_KEY}" for r in (first_request, second_request)
    )
    assert all(TEST_KEY.encode() not in r.body for r in (first_request, second_request))
    assert first_params["messages"][0] == {"role": "system", "content": prompt}
    assert json.loads(first_params["messages"][1]["content"]) == MATERIAL
    assert {tool["function"]["name"] for tool in first_params["tools"]} == {
        "deepseek_web_search",
        "deepseek_read_url",
    }
    assert "response_format" not in first_params
    assert "tools" not in second_params
    assert second_params["response_format"] == {"type": "json_object"}
    assert json.loads(second_params["messages"][1]["content"]) == {
        "report": first.text,
        "target_numbers": {"match_id": "jc:1", "jc_num": "周五001"},
    }
    assert first_params["max_tokens"] > 0 and second_params["max_tokens"] > 0

    assert first.raw == research and second.raw == structure
    assert first.text == research["choices"][0]["message"]["content"]
    assert first.citations == research["choices"][0]["message"]["annotations"]
    assert first.usage == research["usage"]
    assert first.tool_calls == 3 and second.tool_calls == 0
    assert first.estimated_cost == Decimal("0.0029511")
    assert second.estimated_cost == Decimal("0.0000942")
    assert provider.research_reserve(prompt, MATERIAL) > first.estimated_cost
    assert provider.structure_reserve(first.text, MATERIAL) > second.estimated_cost


@responses.activate
def test_missing_citations_and_tool_accounting_remain_unknown(provider):
    payload = fixture("research")
    del payload["choices"][0]["message"]["annotations"]
    del payload["usage"]["tool_calls"]
    responses.post(URL, json=payload)

    result = provider.research("test", MATERIAL)

    assert result.citations == []
    assert result.tool_calls is None
    assert result.estimated_cost is None
    assert result.raw == payload


@responses.activate
def test_no_annotations_is_distinct_from_unknown_usage(provider):
    payload = fixture("research")
    del payload["choices"][0]["message"]["annotations"]
    payload["usage"]["billed_cost"] = "0.0031"
    payload["usage"]["currency"] = "CNY"
    responses.post(URL, json=payload)
    result = provider.research("test", MATERIAL)
    assert result.citations == [] and result.tool_calls == 3
    assert result.estimated_cost == Decimal("0.0029511")
    assert (result.billed_cost, result.billed_currency) == (Decimal("0.0031"), "CNY")


@pytest.mark.parametrize(
    ("status", "code"), [(401, "http"), (429, "http"), (503, "http"), (302, "http")]
)
@responses.activate
def test_unsuccessful_status_has_no_external_body_no_retry_and_no_redirect(provider, status, code):
    responses.post(URL, status=status, body=f"echo {TEST_KEY}", headers={"Location": URL + "x"})
    with pytest.raises(ProviderError) as caught:
        provider.research("test", MATERIAL)
    assert (caught.value.code, caught.value.status_code) == (code, status)
    assert TEST_KEY not in str(caught.value)
    assert len(responses.calls) == 1


@responses.activate
def test_timeout_and_secret_echo_are_reported_safely(provider):
    responses.post(URL, body=requests.Timeout(f"timeout with {TEST_KEY}"))
    payload = fixture("research")
    payload["choices"][0]["message"]["content"] += TEST_KEY
    responses.post(URL, json=payload)
    with pytest.raises(ProviderError, match="timeout") as timeout:
        provider.research("test", MATERIAL)
    assert TEST_KEY not in str(timeout.value)
    with pytest.raises(ProviderError, match="secret_echo") as echo:
        provider.research("test", MATERIAL)
    assert TEST_KEY not in str(echo.value)


@responses.activate
def test_escaped_credential_echo_cannot_enter_raw_response(monkeypatch):
    escaped_key = 'synthetic-\\-credential-"-with-escapes'
    monkeypatch.setenv("DEEPSEEK_API_KEY", escaped_key)
    payload = fixture("research")
    payload["choices"][0]["message"]["content"] += escaped_key
    responses.post(URL, json=payload)
    with pytest.raises(ProviderError, match="secret_echo"):
        DeepSeek().research("test", MATERIAL)


@responses.activate
def test_client_tool_request_is_not_executed(provider):
    payload = fixture("research")
    payload["choices"][0]["finish_reason"] = "tool_calls"
    responses.post(URL, json=payload)
    with pytest.raises(ProviderError, match="unsupported_tool_protocol"):
        provider.research("test", MATERIAL)
    assert len(responses.calls) == 1


@responses.activate
def test_truncated_content_retains_usage_and_reports_finish_reason(provider):
    payload = fixture("research")
    payload["choices"][0]["finish_reason"] = "length"
    responses.post(URL, json=payload)
    result = provider.research("test", MATERIAL)
    assert result.finish_reason == "length"
    assert result.raw == payload
    assert result.tool_calls == 3


@responses.activate
def test_invalid_utf8_string_cannot_enter_frozen_records(provider):
    payload = fixture("research")
    payload["choices"][0]["message"]["content"] += "\ud800"
    responses.post(URL, body=json.dumps(payload))
    with pytest.raises(ProviderError, match="invalid_response"):
        provider.research("test", MATERIAL)


def test_missing_key_validation(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="missing_key"):
        DeepSeek()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "invalid\nheader")
    with pytest.raises(ProviderError, match="invalid_key"):
        DeepSeek()
