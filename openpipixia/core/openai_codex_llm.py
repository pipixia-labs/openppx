"""ADK BaseLlm adapter for OpenAI Codex OAuth Responses API.

This adapter maps ADK request/response primitives to Codex Responses API:
- Reads OAuth token via `oauth_cli_kit.get_token()`
- Sends conversation and tool state as Codex `input` items
- Parses SSE events into ADK `LlmResponse` with text and function calls
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Iterable

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from loguru import logger

DEFAULT_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "openpipixia"


@dataclass(frozen=True)
class _CodexToolCall:
    """Normalized function-call payload parsed from Codex SSE events."""

    id: str
    name: str
    arguments: dict[str, Any]


class OpenAICodexLlm(BaseLlm):
    """ADK-compatible Codex model adapter based on OAuth credentials."""

    codex_url: str = DEFAULT_CODEX_RESPONSES_URL
    timeout_seconds: float = 60.0

    @classmethod
    def supported_models(cls) -> list[str]:
        """Model matcher for ADK model registry."""
        return [r"^openai-codex/.*$", r"^gpt-5.*codex.*$"]

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """Generate one ADK response turn from Codex.

        Args:
            llm_request: ADK request payload.
            stream: Streaming flag from ADK. Codex is consumed as stream internally.

        Yields:
            One final `LlmResponse` for the turn.
        """
        del stream  # Current adapter emits one final event per turn.
        try:
            instructions, input_items, tools = _convert_llm_request(llm_request)
            token = await asyncio.to_thread(_get_codex_token)
            headers = _build_headers(token.account_id, token.access)

            body: dict[str, Any] = {
                "model": _strip_model_prefix(llm_request.model or self.model),
                "store": False,
                "stream": True,
                "instructions": instructions,
                "input": input_items,
                "text": {"verbosity": "medium"},
                "include": ["reasoning.encrypted_content"],
                "prompt_cache_key": _prompt_cache_key(llm_request),
                "tool_choice": "auto",
                "parallel_tool_calls": True,
            }
            if tools:
                body["tools"] = tools

            config = llm_request.config
            if config and config.temperature is not None:
                body["temperature"] = config.temperature
            if config and config.max_output_tokens is not None:
                body["max_output_tokens"] = max(1, int(config.max_output_tokens))

            url = self.codex_url
            try:
                text, tool_calls, finish_reason = await _request_codex(
                    url=url,
                    headers=headers,
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                    verify=True,
                )
            except Exception as exc:
                if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
                    raise
                logger.warning("Codex SSL verification failed; retrying with verify=False")
                text, tool_calls, finish_reason = await _request_codex(
                    url=url,
                    headers=headers,
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                    verify=False,
                )

            parts: list[types.Part] = []
            if text:
                parts.append(types.Part.from_text(text=text))
            for tool_call in tool_calls:
                part = types.Part.from_function_call(
                    name=tool_call.name,
                    args=tool_call.arguments,
                )
                if part.function_call:
                    part.function_call.id = tool_call.id
                parts.append(part)

            yield LlmResponse(
                content=types.Content(role="model", parts=parts),
                finish_reason=finish_reason,
                turn_complete=True,
                partial=False,
                model_version=self.model,
            )
        except Exception as exc:
            yield LlmResponse(
                error_code="CODEX_ERROR",
                error_message=str(exc),
                finish_reason=types.FinishReason.OTHER,
                turn_complete=True,
                partial=False,
            )


def _get_codex_token() -> Any:
    """Load OAuth token for Codex usage from local oauth-cli-kit store."""
    try:
        from oauth_cli_kit import get_token
    except ImportError as exc:  # pragma: no cover - environment dependency
        raise RuntimeError("oauth-cli-kit is not installed. Run: pip install oauth-cli-kit") from exc

    token = get_token()
    if not token or not getattr(token, "access", ""):
        raise RuntimeError(
            "OpenAI Codex OAuth token missing. Run: openpipixia provider login openai-codex"
        )
    if not getattr(token, "account_id", ""):
        raise RuntimeError("OpenAI Codex OAuth token is missing account_id.")
    return token


def _strip_model_prefix(model: str) -> str:
    """Strip `openai-codex/` namespace for Codex backend API."""
    if model.startswith("openai-codex/"):
        return model.split("/", 1)[1]
    return model


def _build_headers(account_id: str, access_token: str) -> dict[str, str]:
    """Build Codex API request headers from OAuth token metadata."""
    return {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "openpipixia (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


def _prompt_cache_key(llm_request: LlmRequest) -> str:
    """Create a deterministic cache key from ADK request payload."""
    payload = {
        "model": llm_request.model or "",
        "system_instruction": llm_request.config.system_instruction if llm_request.config else "",
        "contents": [
            content.model_dump(exclude_none=True, mode="json") for content in llm_request.contents
        ],
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_content_role(raw_role: str | None) -> str:
    """Map ADK content role to codex message role domain."""
    role = (raw_role or "").strip().lower()
    if role in {"assistant", "model"}:
        return "assistant"
    return "user"


def _safe_json_string(value: Any) -> str:
    """Serialize arbitrary value to string for Codex function I/O items."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _convert_llm_request(
    llm_request: LlmRequest,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert ADK request into Codex `instructions + input + tools` payload."""
    instructions = ""
    if llm_request.config and isinstance(llm_request.config.system_instruction, str):
        instructions = llm_request.config.system_instruction

    input_items: list[dict[str, Any]] = []
    function_call_item_index = 0
    for idx, content in enumerate(llm_request.contents):
        role = _normalize_content_role(content.role)
        text_parts: list[str] = []
        assistant_function_calls: list[dict[str, Any]] = []

        for part in content.parts:
            if part.function_call:
                function_call_item_index += 1
                call_id = part.function_call.id or f"call_{idx}_{function_call_item_index}"
                item_id = f"fc_{idx}_{function_call_item_index}"
                assistant_function_calls.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": part.function_call.name,
                        "arguments": _safe_json_string(part.function_call.args or {}),
                    }
                )
                continue
            if part.function_response:
                call_id = part.function_response.id or f"call_{idx}_{function_call_item_index + 1}"
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": _safe_json_string(part.function_response.response),
                    }
                )
                continue
            if part.text:
                text_parts.append(part.text)

        text_payload = "\n".join(text_parts).strip()
        if text_payload:
            if role == "assistant":
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text_payload}],
                        "status": "completed",
                        "id": f"msg_{idx}",
                    }
                )
            else:
                input_items.append(
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": text_payload}],
                    }
                )

        if assistant_function_calls:
            input_items.extend(assistant_function_calls)

    tools: list[dict[str, Any]] = []
    tool_entries = llm_request.config.tools if llm_request.config else None
    for tool in tool_entries or []:
        declarations = getattr(tool, "function_declarations", None) or []
        for decl in declarations:
            name = getattr(decl, "name", "")
            if not name:
                continue
            params: dict[str, Any] = {"type": "object", "properties": {}}
            if getattr(decl, "parameters_json_schema", None):
                candidate = decl.parameters_json_schema
                if isinstance(candidate, dict):
                    params = candidate
            elif getattr(decl, "parameters", None):
                params = decl.parameters.model_dump(exclude_none=True, mode="json")
            tools.append(
                {
                    "type": "function",
                    "name": name,
                    "description": getattr(decl, "description", "") or "",
                    "parameters": params if isinstance(params, dict) else {"type": "object", "properties": {}},
                }
            )

    return instructions, input_items, tools


async def _request_codex(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_seconds: float,
    verify: bool,
) -> tuple[str, list[_CodexToolCall], types.FinishReason]:
    """Execute one Codex streamed call and parse SSE events into final response."""
    async with httpx.AsyncClient(timeout=timeout_seconds, verify=verify) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = (await response.aread()).decode("utf-8", "ignore")
                raise RuntimeError(_friendly_error(response.status_code, text))
            events = [event async for event in _iter_sse(response)]
    return _consume_codex_events(events)


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    """Yield parsed JSON events from an SSE response stream."""
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if not buffer:
                continue
            data_lines = [entry[5:].strip() for entry in buffer if entry.startswith("data:")]
            buffer = []
            if not data_lines:
                continue
            payload = "\n".join(data_lines).strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except Exception:
                continue
            continue
        buffer.append(line)


def _consume_codex_events(
    events: Iterable[dict[str, Any]],
) -> tuple[str, list[_CodexToolCall], types.FinishReason]:
    """Reduce Codex events into final text, function calls and finish reason."""
    text = ""
    finish_reason = types.FinishReason.STOP
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    tool_calls: list[_CodexToolCall] = []

    for event in events:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            text += event.get("delta") or ""
            continue
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if call_id:
                    tool_call_buffers[call_id] = {
                        "id": item.get("id") or "fc_0",
                        "name": item.get("name"),
                        "arguments": item.get("arguments") or "",
                    }
            continue
        if event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] += event.get("delta") or ""
            continue
        if event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] = event.get("arguments") or ""
            continue
        if event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                continue
            call_id = item.get("call_id")
            if not call_id:
                continue
            buf = tool_call_buffers.get(call_id) or {}
            raw_args = buf.get("arguments") or item.get("arguments") or "{}"
            try:
                parsed_args = json.loads(raw_args)
            except Exception:
                parsed_args = {"raw": raw_args}
            tool_calls.append(
                _CodexToolCall(
                    id=call_id,
                    name=str(buf.get("name") or item.get("name") or ""),
                    arguments=parsed_args if isinstance(parsed_args, dict) else {"value": parsed_args},
                )
            )
            continue
        if event_type == "response.completed":
            status = str((event.get("response") or {}).get("status") or "completed")
            finish_reason = _map_finish_reason(status)
            continue
        if event_type in {"error", "response.failed"}:
            raise RuntimeError("Codex response failed")

    return text, tool_calls, finish_reason


def _map_finish_reason(status: str) -> types.FinishReason:
    """Map Codex response status to ADK finish reason enum."""
    normalized = status.strip().lower()
    if normalized == "completed":
        return types.FinishReason.STOP
    if normalized == "incomplete":
        return types.FinishReason.MAX_TOKENS
    return types.FinishReason.OTHER


def _friendly_error(status_code: int, body_text: str) -> str:
    """Render user-friendly transport error for Codex responses API."""
    if status_code == 401:
        return "Codex authentication failed. Please re-run: openpipixia provider login openai-codex"
    if status_code == 429:
        return "Codex quota exceeded or rate limited. Please retry later."
    return f"Codex HTTP {status_code}: {body_text}"
