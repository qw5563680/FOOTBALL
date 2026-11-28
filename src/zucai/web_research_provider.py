"""T70 transport for DeepSeek Chat Completions and its server-side web tools."""

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests

from zucai.web_research_validation import STRUCTURE_INSTRUCTIONS

_URL = "https://api.deepseek.com/chat/completions"
_TOOLS = ("deepseek_web_search", "deepseek_read_url")
_TOOL_COUNTERS = ("tool_search", "tool_fetch", "tool_image_search")
_OUTPUT_TOKENS = {"research": 4096, "structure": 2048}
_INPUT_TOKEN_RATE = Decimal("0.0000003")
_OUTPUT_TOKEN_RATE = Decimal("0.0000012")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ERROR_BYTES = 2048


@dataclass(frozen=True)
class Step:
    text: str
    citations: list
    usage: dict
    tool_calls: int | None
    estimated_cost: Decimal | None
    raw: dict
    billed_cost: Decimal | None = None
    billed_currency: str | None = None
    finish_reason: str = "stop"


class ProviderError(ValueError):
    """Expose bounded, redacted detail separately from the log-safe message."""

    def __init__(self, code, status_code=None, *, detail=None):
        self.code = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(code if status_code is None else f"{code} {status_code}")


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _nonnegative_integer(value):
    return type(value) is int and value >= 0


def _tool_count(message, usage):
    steps = message.get("server_tool_calls")
    if isinstance(steps, list) and all(
        isinstance(step, dict) and isinstance(step.get("calls"), list) for step in steps
    ):
        return sum(len(step["calls"]) for step in steps)
    reported = usage.get("server_tool_use")
    if not isinstance(reported, dict):
        return None
    counters = [reported.get(name, 0) for name in _TOOL_COUNTERS]
    return sum(counters) if all(map(_nonnegative_integer, counters)) else None


def _cost(prompt_tokens, completion_tokens):
    if not all(map(_nonnegative_integer, (prompt_tokens, completion_tokens))):
        return None
    # DeepSeek bills these server-side tools through token usage only.
    return prompt_tokens * _INPUT_TOKEN_RATE + completion_tokens * _OUTPUT_TOKEN_RATE


def _billed(usage):
    currency = usage.get("currency")
    cost = usage.get("billed_cost")
    if (
        not isinstance(currency, str)
        or not re.fullmatch(r"[A-Z]{3}", currency)
        or type(cost) not in (str, int, float)
    ):
        return None, None
    try:
        amount = Decimal(str(cost))
    except InvalidOperation:
        return None, None
    return (amount, currency) if amount.is_finite() and amount >= 0 else (None, None)


def _reserve(payload):
    # Use one token per UTF-8 byte of the request plus chat framing overhead.
    input_allowance = len(_json(payload).encode("utf-8")) + 512
    return _cost(input_allowance, payload["max_tokens"])


def _error_detail(response, key):
    # Read enough lookahead to redact a key even if it crosses the output limit.
    # Each printable ASCII key character takes at most six bytes as a JSON escape.
    limit = _MAX_ERROR_BYTES + 6 * len(key)
    raw = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=_MAX_ERROR_BYTES):
            raw.extend(chunk[: limit - len(raw)])
            if len(raw) == limit:
                break
    except requests.RequestException:
        return None
    if not raw:
        return None

    # Match literal and mixed JSON-escaped forms without rewriting other text.
    pattern = []
    for char in key:
        forms = [re.escape(char), rf"\\u(?i:{ord(char):04x})"]
        if char in '\\"/':
            forms.append(re.escape("\\" + char))
        pattern.append("(?:" + "|".join(forms) + ")")
    text = re.sub("".join(pattern), "[redacted]", raw.decode("utf-8", errors="replace"))
    return text.encode("utf-8")[:_MAX_ERROR_BYTES].decode("utf-8", errors="ignore")


def _response_object(response):
    raw = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        raw.extend(chunk)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ProviderError("response_too_large")
    try:
        data = json.loads(raw)
        canonical = _json(data).encode("utf-8")
    except (ValueError, RecursionError):
        raise ProviderError("invalid_response") from None
    if not isinstance(data, dict):
        raise ProviderError("invalid_response")
    return data, canonical


def _message(data):
    try:
        (choice,) = data["choices"]
        message = choice["message"]
        if choice["finish_reason"] == "tool_calls" or message.get("tool_calls"):
            raise ProviderError("unsupported_tool_protocol")
        if message["role"] == "assistant" and isinstance(message["content"], str):
            if message["content"].strip():
                return message, choice["finish_reason"]
    except ProviderError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    raise ProviderError("invalid_response")


def _citations(data, message):
    for source, key in ((message, "annotations"), (message, "citations"), (data, "citations")):
        value = source.get(key)
        if isinstance(value, list) and value:
            # Keep provider fields, rather than equating the number of links with tool usage.
            return value
    return []


class DeepSeek:
    name = "deepseek"
    model = "deepseek-flash"

    def __init__(self):
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key or not key.strip():
            raise ProviderError("missing_key")
        if any(not 33 <= ord(character) <= 126 for character in key):
            raise ProviderError("invalid_key")
        self._key = key

    def _payload(self, stage, prompt, evidence):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": _json(evidence)},
            ],
            "thinking": {"type": "disabled"},
            "max_tokens": _OUTPUT_TOKENS[stage],
        }
        if stage == "research":
            payload["tools"] = [{"type": name} for name in _TOOLS]
            payload["safety_check"] = {"level": "high"}
        else:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _structuring_payload(self, report, material):
        numbers = {key: material[key] for key in ("match_id", "jc_num") if key in material}
        return self._payload(
            "structure", STRUCTURE_INSTRUCTIONS, {"report": report, "target_numbers": numbers}
        )

    def research_reserve(self, prompt, material):
        return _reserve(self._payload("research", prompt, material))

    def structure_reserve(self, report, material):
        return _reserve(self._structuring_payload(report, material))

    def research(self, prompt, material):
        return self._request(self._payload("research", prompt, material), research=True)

    def structure(self, report, material):
        return self._request(self._structuring_payload(report, material), research=False)

    def _request(self, payload, *, research):
        try:
            with requests.post(
                _URL,
                headers={"Authorization": f"Bearer {self._key}"},
                json=payload,
                timeout=(5, 60),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise ProviderError(
                        "http", response.status_code, detail=_error_detail(response, self._key)
                    )
                data, canonical = _response_object(response)
        except requests.Timeout:
            raise ProviderError("timeout") from None
        except requests.RequestException:
            raise ProviderError("transport") from None
        # Compare the escaped representation too, so quotes/backslashes in
        # the supplied credential do not mask an echo in a JSON string.
        if _json(self._key)[1:-1].encode("ascii") in canonical:
            raise ProviderError("secret_echo")
        message, finish = _message(data)
        if finish not in ("stop", "length"):
            raise ProviderError("invalid_response")
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        tools = _tool_count(message, usage)
        if not research:
            if tools:
                raise ProviderError("unsupported_tool_protocol")
            tools = 0
        billed_cost, billed_currency = _billed(usage)
        return Step(
            text=message["content"],
            citations=_citations(data, message),
            usage=usage,
            tool_calls=tools,
            estimated_cost=_cost(usage.get("prompt_tokens"), usage.get("completion_tokens")),
            raw=data,
            billed_cost=billed_cost,
            billed_currency=billed_currency,
            finish_reason=finish,
        )
