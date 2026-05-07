"""LLM completion functionality using DashScope-compatible API (streaming by default)."""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from .schema import Message, ToolCallSchema, AssistantMessage
from .config import config

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_ANTHROPIC_DEFAULT_MAX_TOKENS = 8096


class LLMResponse(BaseModel):
    """Response from LLM completion."""

    message: AssistantMessage
    original_content: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoint / header helpers
# ---------------------------------------------------------------------------

def _build_endpoint() -> str:
    base = (config.LLM_BASE_URL or "").rstrip("/")
    if not base:
        return _DEFAULT_BASE_URL
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _build_headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.LLM_API_KEY}",
    }


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

# Keys Gemini rejects as JSON Schema *annotations*. Do NOT include names that can also be MCP
# argument keys (e.g. "title"): stripping those deletes entire properties while `required` may
# still list them → "required fields ['title'] are not defined in the schema properties".
_GEMINI_UNSUPPORTED_SCHEMA_KEYS = {
    "$schema", "$id", "$ref", "$defs", "$anchor",
    "additionalProperties", "unevaluatedProperties",
    "readOnly", "writeOnly", "const",
    # JSON Schema numeric bounds — Gemini function-declaration schema rejects these (HTTP 400).
    "exclusiveMinimum",
    "exclusiveMaximum",
}

_EXCLUSIVE_BOUND_KEYS = frozenset({"exclusiveMinimum", "exclusiveMaximum"})


def strip_exclusive_numeric_bounds(schema: Any) -> Any:
    """Remove exclusiveMinimum/exclusiveMaximum everywhere (safe for OpenAI/Anthropic).

    DashScope (and similar gateways) often accept OpenAI-shaped ``tools`` but translate them
    server-side into Gemini ``function_declarations``. Those requests skip ``_to_gemini_payload``
    when ``model`` does not match ``vertex_ai.*``, so Gemini-only stripping never ran — unless we
    strip these bounds unconditionally.
    """
    if isinstance(schema, dict):
        for k in _EXCLUSIVE_BOUND_KEYS:
            schema.pop(k, None)
        for value in schema.values():
            strip_exclusive_numeric_bounds(value)
    elif isinstance(schema, list):
        for item in schema:
            strip_exclusive_numeric_bounds(item)
    return schema


def strip_all_additional_properties(schema: Any) -> Any:
    """Recursively remove `additionalProperties` (OpenAI compat pass)."""
    if isinstance(schema, dict):
        schema.pop("additionalProperties", None)
        for value in schema.values():
            strip_all_additional_properties(value)
    elif isinstance(schema, list):
        for item in schema:
            strip_all_additional_properties(item)
    return schema


def _strip_gemini_unsupported(schema: Any) -> Any:
    """Recursively remove all fields that Gemini rejects in parameter schemas."""
    if isinstance(schema, dict):
        for key in _GEMINI_UNSUPPORTED_SCHEMA_KEYS:
            schema.pop(key, None)
        for value in list(schema.values()):
            _strip_gemini_unsupported(value)
    elif isinstance(schema, list):
        for item in schema:
            _strip_gemini_unsupported(item)
    return schema


def _prune_gemini_required_to_properties(schema: Any) -> Any:
    """Gemini requires every name in `required` to exist under `properties`."""
    if isinstance(schema, dict):
        props = schema.get("properties")
        req = schema.get("required")
        if isinstance(props, dict) and isinstance(req, list):
            filtered = [k for k in req if isinstance(k, str) and k in props]
            if filtered:
                schema["required"] = filtered
            else:
                schema.pop("required", None)
        for value in schema.values():
            _prune_gemini_required_to_properties(value)
    elif isinstance(schema, list):
        for item in schema:
            _prune_gemini_required_to_properties(item)
    return schema


def _normalize_gemini_parameter_schema(schema: Any) -> Any:
    """Apply all Gemini-safe transforms (mutates in place)."""
    _strip_gemini_unsupported(schema)
    _prune_gemini_required_to_properties(schema)
    return schema


# ---------------------------------------------------------------------------
# Model family detection
# ---------------------------------------------------------------------------

def _is_gemini_model(model: str) -> bool:
    return model.lower().startswith("vertex_ai.") or "gemini" in model.lower()


def _is_claude_model(model: str) -> bool:
    m = model.lower()
    return m.startswith("aws.claude") or m.startswith("anthropic") or "claude" in m


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _parse_json_maybe(raw: str) -> Optional[Any]:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _merge_usage_dict(dst: Dict[str, Any], src: Optional[Dict[str, Any]]) -> None:
    if not isinstance(src, dict):
        return
    for key, value in src.items():
        if isinstance(value, dict):
            current = dst.setdefault(key, {})
            _merge_usage_dict(current, value)
        else:
            dst[key] = value


# ---------------------------------------------------------------------------
# Anthropic streaming parser  (adapted from DashScope reference probe)
# ---------------------------------------------------------------------------

async def _read_anthropic_stream(response: httpx.Response) -> Dict[str, Any]:
    """Consume an Anthropic SSE stream and return a complete response dict."""
    message: Dict[str, Any] = {}
    usage: Dict[str, Any] = {}
    stop_reason: Optional[str] = None
    content_blocks: Dict[int, Dict[str, Any]] = {}
    partial_json_buffers: Dict[int, str] = {}
    pending_payload = ""

    async for raw_line in response.aiter_lines():
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue

        payload_str = line[len("data:"):].strip()
        if not payload_str or payload_str == "[DONE]":
            continue

        event = _parse_json_maybe(payload_str)
        if event is None and pending_payload:
            event = _parse_json_maybe(pending_payload + payload_str)
        if event is None:
            pending_payload += payload_str
            continue
        pending_payload = ""
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")

        if event_type == "message_start":
            em = event.get("message", {})
            if isinstance(em, dict):
                for k in ("id", "model", "role", "type"):
                    if k in em:
                        message[k] = em[k]
                _merge_usage_dict(usage, em.get("usage"))

        elif event_type == "content_block_start":
            idx = int(event.get("index", 0))
            block = event.get("content_block", {})
            content_blocks[idx] = dict(block) if isinstance(block, dict) else {}

        elif event_type == "content_block_delta":
            idx = int(event.get("index", 0))
            delta = event.get("delta", {})
            if not isinstance(delta, dict):
                continue
            block = content_blocks.setdefault(idx, {})
            dt = delta.get("type")

            if dt == "text_delta":
                block["type"] = block.get("type", "text")
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif dt == "thinking_delta":
                block["type"] = block.get("type", "thinking")
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif dt == "signature_delta":
                block["type"] = block.get("type", "thinking")
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif dt == "input_json_delta":
                block["type"] = block.get("type", "tool_use")
                partial = delta.get("partial_json", "")
                if partial:
                    merged = partial_json_buffers.get(idx, "") + partial
                    partial_json_buffers[idx] = merged
                    parsed = _parse_json_maybe(merged)
                    if isinstance(parsed, dict):
                        block["input"] = parsed

        elif event_type == "content_block_stop":
            idx = int(event.get("index", 0))
            pending = partial_json_buffers.get(idx)
            if pending and "input" not in content_blocks.get(idx, {}):
                parsed = _parse_json_maybe(pending)
                if isinstance(parsed, dict):
                    content_blocks[idx]["input"] = parsed

        elif event_type == "message_delta":
            delta = event.get("delta", {})
            if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                stop_reason = delta.get("stop_reason")
            _merge_usage_dict(usage, event.get("usage"))

        elif event_type == "message_stop":
            _merge_usage_dict(usage, event.get("usage"))

    content: List[Dict[str, Any]] = []
    for idx in sorted(content_blocks):
        block = content_blocks[idx]
        bt = block.get("type")
        if bt == "text" and not block.get("text"):
            continue
        if bt == "thinking" and not block.get("thinking"):
            continue
        content.append(block)

    return {
        "content": content,
        "stop_reason": stop_reason,
        "type": message.get("type", "message"),
        "role": message.get("role", "assistant"),
        "model": message.get("model", ""),
        "id": message.get("id", ""),
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# Gemini streaming parser
# ---------------------------------------------------------------------------

async def _read_gemini_stream(response: httpx.Response) -> Dict[str, Any]:
    """Consume a Gemini SSE stream and return a combined response dict."""
    accumulated_parts: List[Dict[str, Any]] = []
    finish_reason: Optional[str] = None

    async for raw_line in response.aiter_lines():
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload_str = line[len("data:"):].strip()
        if not payload_str or payload_str == "[DONE]":
            continue

        event = _parse_json_maybe(payload_str)
        if not isinstance(event, dict):
            continue

        candidates = event.get("candidates") or []
        if not candidates:
            continue

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])

        for part in parts:
            # Merge adjacent text parts (non-thought) to keep the list compact
            if (
                "text" in part
                and not part.get("thought")
                and accumulated_parts
                and "text" in accumulated_parts[-1]
                and not accumulated_parts[-1].get("thought")
            ):
                accumulated_parts[-1]["text"] += part["text"]
            else:
                accumulated_parts.append(dict(part))

        if candidate.get("finishReason"):
            finish_reason = candidate.get("finishReason")

    return {
        "candidates": [{
            "content": {"role": "model", "parts": accumulated_parts},
            "finishReason": finish_reason,
        }]
    }


# ---------------------------------------------------------------------------
# OpenAI streaming parser
# ---------------------------------------------------------------------------

async def _read_openai_stream(response: httpx.Response) -> Dict[str, Any]:
    """Consume an OpenAI SSE stream and return a complete response dict."""
    content = ""
    tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
    finish_reason: Optional[str] = None

    async for raw_line in response.aiter_lines():
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload_str = line[len("data:"):].strip()
        if not payload_str or payload_str == "[DONE]":
            continue

        event = _parse_json_maybe(payload_str)
        if not isinstance(event, dict):
            continue

        choices = event.get("choices") or []
        if not choices:
            continue

        delta = choices[0].get("delta", {})
        if delta.get("content"):
            content += delta["content"]

        for tc_delta in (delta.get("tool_calls") or []):
            idx = tc_delta.get("index", 0)
            if idx not in tool_calls_by_index:
                tool_calls_by_index[idx] = {
                    "id": tc_delta.get("id", ""),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            tc = tool_calls_by_index[idx]
            func = tc_delta.get("function", {})
            if func.get("name"):
                tc["function"]["name"] += func["name"]
            if func.get("arguments"):
                tc["function"]["arguments"] += func["arguments"]
            if tc_delta.get("id"):
                tc["id"] = tc_delta["id"]

        if choices[0].get("finish_reason"):
            finish_reason = choices[0]["finish_reason"]

    tool_calls = (
        [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
        if tool_calls_by_index
        else None
    )

    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls,
            },
            "finish_reason": finish_reason,
        }]
    }


# ---------------------------------------------------------------------------
# Gemini format conversion
# ---------------------------------------------------------------------------

def _to_gemini_payload(
    model: str,
    messages_payload: List[Dict[str, Any]],
    tools_payload: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Convert OpenAI-format messages/tools to a Gemini-native payload."""
    tool_id_to_name: Dict[str, str] = {}
    for msg in messages_payload:
        if msg.get("role") == "assistant":
            for tc in (msg.get("tool_calls") or []):
                tool_id_to_name[tc["id"]] = tc["function"]["name"]

    system_instruction: Optional[Dict[str, Any]] = None
    contents: List[Dict[str, Any]] = []
    pending_fn_responses: List[Dict[str, Any]] = []

    def _flush() -> None:
        nonlocal pending_fn_responses
        if pending_fn_responses:
            contents.append({"role": "user", "parts": pending_fn_responses})
            pending_fn_responses = []

    for msg in messages_payload:
        role = msg["role"]
        if role == "tool":
            func_name = tool_id_to_name.get(msg.get("tool_call_id", ""), "unknown_tool")
            pending_fn_responses.append({
                "functionResponse": {
                    "name": func_name,
                    "response": {"result": msg.get("content", "")},
                }
            })
        else:
            _flush()
            if role == "system":
                system_instruction = {"parts": [{"text": msg["content"]}]}
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": msg["content"]}]})
            elif role == "assistant":
                raw_parts = msg.get("raw_parts")
                if raw_parts:
                    contents.append({"role": "model", "parts": raw_parts})
                else:
                    parts: List[Dict[str, Any]] = []
                    if msg.get("content"):
                        parts.append({"text": msg["content"]})
                    for tc in (msg.get("tool_calls") or []):
                        parts.append({
                            "functionCall": {
                                "name": tc["function"]["name"],
                                "args": json.loads(tc["function"]["arguments"]),
                            }
                        })
                    if parts:
                        contents.append({"role": "model", "parts": parts})
    _flush()

    payload: Dict[str, Any] = {"model": model, "contents": contents, "stream": True}
    if system_instruction:
        payload["system_instruction"] = system_instruction

    if tools_payload:
        declarations = []
        for tool in tools_payload:
            func = tool.get("function", {})
            decl: Dict[str, Any] = {
                "name": func["name"],
                "description": func.get("description", ""),
            }
            params = func.get("parameters")
            if params:
                decl["parameters"] = _normalize_gemini_parameter_schema(params)
            declarations.append(decl)
        payload["tools"] = [{"function_declarations": declarations}]

    return payload


def _parse_gemini_response(body: Dict[str, Any]) -> AssistantMessage:
    """Parse a Gemini response dict (accumulated from stream) into AssistantMessage."""
    candidates = body.get("candidates") or []
    if not candidates:
        raise Exception(f"No candidates in Gemini response: {body}")

    parts: List[Dict[str, Any]] = candidates[0].get("content", {}).get("parts", [])
    text_parts = [p["text"] for p in parts if "text" in p and not p.get("thought")]
    func_calls = [p["functionCall"] for p in parts if "functionCall" in p]

    text = "\n".join(text_parts) if text_parts else None
    tool_calls = None
    if func_calls:
        tool_calls = [
            {
                "id": f"gemini_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": fc["name"],
                    "arguments": json.dumps(fc.get("args", {})),
                },
            }
            for fc in func_calls
        ]

    return AssistantMessage(
        role="assistant", content=text, tool_calls=tool_calls, raw_parts=parts
    )


# ---------------------------------------------------------------------------
# Anthropic / Claude format conversion
# ---------------------------------------------------------------------------

def _to_anthropic_payload(
    model: str,
    messages_payload: List[Dict[str, Any]],
    tools_payload: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Convert OpenAI-format messages/tools to Anthropic-native payload."""
    system_blocks: List[Dict[str, Any]] = []
    anthropic_messages: List[Dict[str, Any]] = []
    pending_tool_results: List[Dict[str, Any]] = []

    def _flush() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            anthropic_messages.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for msg in messages_payload:
        role = msg["role"]
        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": msg.get("content", ""),
            })
        else:
            _flush()
            if role == "system":
                system_blocks.append({"type": "text", "text": msg["content"]})
            elif role == "user":
                anthropic_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": msg["content"]}],
                })
            elif role == "assistant":
                raw_parts = msg.get("raw_parts")
                content: List[Dict[str, Any]] = raw_parts if raw_parts else []
                if not raw_parts:
                    if msg.get("content"):
                        content.append({"type": "text", "text": msg["content"]})
                    for tc in (msg.get("tool_calls") or []):
                        content.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "input": json.loads(tc["function"]["arguments"]),
                        })
                if content:
                    anthropic_messages.append({"role": "assistant", "content": content})
    _flush()

    anthropic_tools = [
        {
            "type": "custom",
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "input_schema": tool["function"].get("parameters", {}),
        }
        for tool in tools_payload
    ]

    payload: Dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": _ANTHROPIC_DEFAULT_MAX_TOKENS,
        "stream": True,
        "dashscope_extend_params": {"using_native_protocol": "true"},
    }
    if system_blocks:
        payload["system"] = system_blocks
    if anthropic_tools:
        payload["tools"] = anthropic_tools

    return payload


def _parse_anthropic_response(body: Dict[str, Any]) -> AssistantMessage:
    """Parse an Anthropic response dict (accumulated from stream) into AssistantMessage."""
    content_blocks: List[Dict[str, Any]] = body.get("content") or []
    text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
    tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]

    text = "\n".join(text_parts) if text_parts else None
    tool_calls = None
    if tool_use_blocks:
        tool_calls = [
            {
                "id": b["id"],
                "type": "function",
                "function": {
                    "name": b["name"],
                    "arguments": json.dumps(b.get("input", {})),
                },
            }
            for b in tool_use_blocks
        ]

    return AssistantMessage(
        role="assistant",
        content=text,
        tool_calls=tool_calls,
        raw_parts=content_blocks,
    )


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class _RetryableError(Exception):
    """Wraps transient errors worth retrying (rate limits, network failures)."""


@retry(
    retry=retry_if_exception_type(_RetryableError),
    wait=wait_exponential(multiplier=1, min=5, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
    before_sleep=lambda retry_state: logger.warning(
        f"Transient error, retrying in {retry_state.next_action.sleep:.0f}s "
        f"(attempt {retry_state.attempt_number}/5)..."
    ),
)
async def create_completion(
    model: str,
    messages: List[Message],
    tools: List[ToolCallSchema],
) -> LLMResponse:
    """Create a streaming completion using the DashScope-compatible API."""

    messages_payload = [msg.model_dump() for msg in messages]
    tools_payload = [strip_all_additional_properties(tool.model_dump()) for tool in tools]
    for tp in tools_payload:
        func = tp.get("function") or {}
        params = func.get("parameters")
        if isinstance(params, dict) and params:
            strip_exclusive_numeric_bounds(params)

    if _is_gemini_model(model):
        payload = _to_gemini_payload(model, messages_payload, tools_payload)
    elif _is_claude_model(model):
        payload = _to_anthropic_payload(model, messages_payload, tools_payload)
    else:
        payload = {"model": model, "messages": messages_payload, "stream": True}
        if tools_payload:
            payload["tools"] = tools_payload

    endpoint = _build_endpoint()
    headers = _build_headers()

    logger.debug(
        f"POST {endpoint} model={model} "
        f"gemini={_is_gemini_model(model)} claude={_is_claude_model(model)}"
    )

    try:
        async with httpx.AsyncClient(timeout=config.DEFAULT_TIMEOUT) as client:
            async with client.stream(
                "POST", endpoint, headers=headers, json=payload
            ) as response:
                if response.status_code == 429:
                    body_bytes = await response.aread()
                    raise _RetryableError(f"429 Rate limit: {body_bytes.decode()}")

                if response.status_code != 200:
                    body_bytes = await response.aread()
                    error_text = body_bytes.decode()
                    logger.error(
                        f"DashScope error {response.status_code}: {error_text[:1000]}"
                    )
                    raise Exception(
                        f"DashScope API error: HTTP {response.status_code}: {error_text[:500]}"
                    )

                if _is_gemini_model(model):
                    body = await _read_gemini_stream(response)
                    assistant_message = _parse_gemini_response(body)
                elif _is_claude_model(model):
                    body = await _read_anthropic_stream(response)
                    assistant_message = _parse_anthropic_response(body)
                else:
                    body = await _read_openai_stream(response)
                    choices = body.get("choices") or []
                    if not choices:
                        raise Exception(f"Empty choices in stream response: {body}")
                    message = choices[0].get("message", {})
                    raw_tcs = message.get("tool_calls")
                    tool_calls = None
                    if raw_tcs:
                        tool_calls = [
                            {
                                "id": tc["id"],
                                "type": tc["type"],
                                "function": {
                                    "name": tc["function"]["name"],
                                    "arguments": tc["function"]["arguments"],
                                },
                            }
                            for tc in raw_tcs
                        ]
                    assistant_message = AssistantMessage(
                        role="assistant",
                        content=message.get("content"),
                        tool_calls=tool_calls,
                    )

    except (_RetryableError, Exception):
        raise

    return LLMResponse(message=assistant_message)


# ---------------------------------------------------------------------------
# Tool schema transformation (MCP → ToolCallSchema)
# ---------------------------------------------------------------------------

def _transform_tool_calls(tools: List[Dict[str, Any]]) -> List[ToolCallSchema]:
    """Transform MCP tool definitions to ToolCallSchema format."""
    return [
        ToolCallSchema(
            type="function",
            function={
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool.get("input_schema", {}),
                "strict": False,
            },
        )
        for tool in tools
    ]
