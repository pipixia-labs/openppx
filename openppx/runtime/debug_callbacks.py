"""ADK plugins for model compatibility, usage metrics, and debug tracing."""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

from ..core.logging_utils import debug_logging_enabled, emit_debug
from .token_usage_store import extract_usage_tokens, write_token_usage_event

_DEFAULT_MAX_TEXT_CHARS = 0
_MAX_TOOL_CALL_ID_CHARS = 40
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*['\"]?([^\s'\",]+)"),
]
_LEGACY_REQUEST_META_ATTR = "_openppx_request_meta"


@dataclass(slots=True)
class _RequestRecord:
    """Request-side state shared by one App's model plugins."""

    request_meta: dict[str, Any] = field(default_factory=dict)
    patched_tool_ids: int = 0


class OpenPpxModelCallbackState:
    """Invocation-scoped state shared by full-profile model plugins.

    ADK plugins are App-scoped, so this state intentionally belongs to one
    plugin list instance instead of a module global. Keys include profile,
    session id, and invocation id to prevent concurrent runners from sharing
    request metadata by accident.
    """

    def __init__(self, *, profile: str) -> None:
        self._profile = profile
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str, str], _RequestRecord] = {}

    def _key_from_callback_context(self, callback_context: Any) -> tuple[str, str, str] | None:
        invocation_id = _non_empty_str(getattr(callback_context, "invocation_id", None))
        if invocation_id is None:
            return None
        session_id = str(getattr(getattr(callback_context, "session", None), "id", "") or "")
        return (self._profile, session_id, invocation_id)

    def record_patched_tool_ids(self, callback_context: Any, patched_tool_ids: int) -> None:
        """Record provider-compatibility mutations for the current model call."""
        key = self._key_from_callback_context(callback_context)
        if key is None:
            return
        with self._lock:
            record = self._records.setdefault(key, _RequestRecord())
            record.patched_tool_ids = max(0, int(patched_tool_ids))

    def patched_tool_ids(self, callback_context: Any) -> int:
        """Return the number of tool ids patched for the current model call."""
        key = self._key_from_callback_context(callback_context)
        if key is None:
            return 0
        with self._lock:
            record = self._records.get(key)
            return 0 if record is None else record.patched_tool_ids

    def record_request_meta(self, callback_context: Any, request_meta: dict[str, Any]) -> None:
        """Record request metadata until the matching final model response."""
        key = self._key_from_callback_context(callback_context)
        if key is None:
            return
        with self._lock:
            record = self._records.setdefault(key, _RequestRecord())
            record.request_meta = dict(request_meta)

    def pop_request_meta(self, callback_context: Any) -> dict[str, Any]:
        """Remove and return request metadata for the current model call."""
        key = self._key_from_callback_context(callback_context)
        if key is None:
            return {}
        with self._lock:
            record = self._records.pop(key, None)
        return {} if record is None else dict(record.request_meta)

    def discard_callback_context(self, callback_context: Any) -> None:
        """Discard any request state for the current callback context."""
        key = self._key_from_callback_context(callback_context)
        if key is None:
            return
        with self._lock:
            self._records.pop(key, None)

    def discard_invocation_context(self, invocation_context: Any) -> None:
        """Discard all request state associated with one invocation."""
        invocation_id = _non_empty_str(getattr(invocation_context, "invocation_id", None))
        if invocation_id is None:
            return
        session_id = str(getattr(getattr(invocation_context, "session", None), "id", "") or "")
        with self._lock:
            for key in list(self._records):
                profile, key_session_id, key_invocation_id = key
                if profile != self._profile or key_invocation_id != invocation_id:
                    continue
                if session_id and key_session_id != session_id:
                    continue
                self._records.pop(key, None)


def _max_chars() -> int:
    raw = os.getenv("OPENPPX_DEBUG_MAX_CHARS", str(_DEFAULT_MAX_TEXT_CHARS)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = _DEFAULT_MAX_TEXT_CHARS
    if value <= 0:
        # Non-positive values explicitly disable clipping for debug payload text.
        return 0
    return max(200, min(value, 20000))


def _redact(text: str) -> str:
    value = text
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.lower().startswith("(?i)(api"):
            value = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", value)
        else:
            value = pattern.sub("<redacted>", value)
    return value


def _clip(text: str) -> str:
    max_chars = _max_chars()
    if max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated {len(text) - max_chars} chars)"


def _extract_part_text(part: Any) -> str:
    if bool(getattr(part, "thought", False)):
        return ""
    text = getattr(part, "text", None)
    return text or ""


def _extract_content_text(content: Any) -> str:
    parts = getattr(content, "parts", None)
    if not parts:
        return ""
    chunks: list[str] = []
    for part in parts:
        text = _extract_part_text(part)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _request_texts(llm_request: LlmRequest) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    system_instruction = getattr(getattr(llm_request, "config", None), "system_instruction", None)
    if isinstance(system_instruction, str) and system_instruction.strip():
        rows.append({"role": "system", "text": system_instruction.strip()})

    for content in getattr(llm_request, "contents", []) or []:
        text = _extract_content_text(content)
        if not text:
            continue
        role = str(getattr(content, "role", "") or "unknown")
        rows.append({"role": role, "text": text})
    return rows


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _tool_id_from_source(source: str, prefix: str) -> str:
    digest = sha1(source.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _normalize_tool_id(raw_id: Any) -> str | None:
    current_id = _non_empty_str(raw_id)
    if current_id is None:
        return None

    if len(current_id) > _MAX_TOOL_CALL_ID_CHARS:
        return _tool_id_from_source(current_id, "t")
    return current_id


def _ensure_unique_tool_id(base_id: str, used_ids: set[str]) -> str:
    if base_id not in used_ids:
        return base_id
    # Keep IDs unique in a single request while remaining within provider length limits.
    suffix = 1
    while True:
        candidate = f"{base_id[:_MAX_TOOL_CALL_ID_CHARS - 3]}_{suffix:02d}"
        if candidate not in used_ids:
            return candidate
        suffix += 1


def _new_tool_id(invocation_id: str, fallback_counter: int, prefix: str) -> str:
    seed = f"{invocation_id}:{fallback_counter}:{uuid.uuid4().hex[:6]}"
    return _tool_id_from_source(seed, prefix)


def _sanitize_tool_ids(callback_context: CallbackContext, llm_request: LlmRequest) -> int:
    """Ensure tool call / tool response ids are present before provider calls."""
    invocation_id = _non_empty_str(getattr(callback_context, "invocation_id", None)) or "inv"
    patched = 0
    fallback_counter = 0
    pending_tool_call_ids: list[str] = []
    used_ids: set[str] = set()

    for content in getattr(llm_request, "contents", []) or []:
        parts = getattr(content, "parts", None) or []
        for part in parts:
            function_call = getattr(part, "function_call", None)
            if function_call is not None:
                raw_id = getattr(function_call, "id", None)
                current_id = _normalize_tool_id(raw_id)
                if current_id is None:
                    fallback_counter += 1
                    current_id = _new_tool_id(invocation_id, fallback_counter, "t")
                    while current_id in used_ids:
                        fallback_counter += 1
                        current_id = _new_tool_id(invocation_id, fallback_counter, "t")
                current_id = _ensure_unique_tool_id(current_id, used_ids)
                if current_id != raw_id:
                    function_call.id = current_id
                    patched += 1
                pending_tool_call_ids.append(current_id)
                used_ids.add(current_id)

            function_response = getattr(part, "function_response", None)
            if function_response is not None:
                raw_response_id = getattr(function_response, "id", None)
                response_id = _normalize_tool_id(raw_response_id)
                if response_id is None:
                    if pending_tool_call_ids:
                        response_id = pending_tool_call_ids.pop(0)
                    else:
                        fallback_counter += 1
                        response_id = _new_tool_id(invocation_id, fallback_counter, "t")
                        while response_id in used_ids:
                            fallback_counter += 1
                            response_id = _new_tool_id(invocation_id, fallback_counter, "t")
                else:
                    if response_id in pending_tool_call_ids:
                        pending_tool_call_ids.remove(response_id)
                    elif response_id in used_ids:
                        # Response IDs may legally match prior function call IDs.
                        pass
                    else:
                        response_id = _ensure_unique_tool_id(response_id, used_ids)
                if response_id != raw_response_id:
                    patched += 1
                function_response.id = response_id
                used_ids.add(response_id)

    return patched


def _response_text(llm_response: LlmResponse) -> str:
    return _extract_content_text(getattr(llm_response, "content", None))


def _write_debug(tag: str, payload: dict[str, Any]) -> None:
    emit_debug(tag, payload, depth=2)


def _request_meta_from_context(callback_context: CallbackContext, llm_request: LlmRequest) -> dict[str, Any]:
    """Build minimal request-side metadata for later usage persistence."""
    request_at_ms = int(time.time() * 1000)
    request_at = datetime.fromtimestamp(request_at_ms / 1000, tz=timezone.utc).isoformat()
    model = str(getattr(llm_request, "model", "") or "")
    provider = str(os.getenv("OPENPPX_PROVIDER", "") or "").strip().lower()
    if not provider:
        model_l = model.lower()
        if model_l.startswith("gemini") or model_l.startswith("google/"):
            provider = "google"
        elif model_l.startswith("openai/") or model_l.startswith("gpt-") or model_l.startswith("o1-"):
            provider = "openai"
    return {
        "request_at_ms": request_at_ms,
        "request_at": request_at,
        "provider": provider,
        "model": model,
        "session_id": str(getattr(getattr(callback_context, "session", None), "id", "") or ""),
        "invocation_id": str(getattr(callback_context, "invocation_id", "") or ""),
    }


def _write_token_usage_if_possible(
    *,
    callback_context: CallbackContext,
    llm_response: LlmResponse,
    request_meta: dict[str, Any],
) -> None:
    """Persist one final LLM usage event when usage counters are available."""
    usage_tokens = extract_usage_tokens(llm_response)
    if usage_tokens["total_tokens"] <= 0:
        return

    response_at_ms = int(time.time() * 1000)
    response_at = datetime.fromtimestamp(response_at_ms / 1000, tz=timezone.utc).isoformat()
    invocation_id = str(getattr(callback_context, "invocation_id", "") or "")
    raw_usage = {
        "usage_metadata": getattr(llm_response, "usage_metadata", None),
        "usage": getattr(llm_response, "usage", None),
    }
    payload: dict[str, Any] = {
        "request_at": request_meta.get("request_at", response_at),
        "request_at_ms": request_meta.get("request_at_ms", response_at_ms),
        "response_at": response_at,
        "response_at_ms": response_at_ms,
        "provider": request_meta.get("provider", ""),
        "model": request_meta.get("model", ""),
        "session_id": request_meta.get("session_id", ""),
        "invocation_id": invocation_id,
        "raw_usage": raw_usage,
    }
    payload.update(usage_tokens)
    write_token_usage_event(payload)


def _matches_target_agent(callback_context: Any, target_agent_name: str | None) -> bool:
    """Return whether a model callback should run for this agent."""
    if not target_agent_name:
        return True
    agent_name = _non_empty_str(getattr(callback_context, "agent_name", None))
    return agent_name is None or agent_name == target_agent_name


class OpenPpxProviderCompatibilityPlugin(BasePlugin):
    """Patch provider-sensitive model request details before LLM calls."""

    def __init__(
        self,
        *,
        state: OpenPpxModelCallbackState,
        target_agent_name: str | None = None,
    ) -> None:
        super().__init__(name="openppx_provider_compatibility")
        self._state = state
        self._target_agent_name = target_agent_name

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        if not _matches_target_agent(callback_context, self._target_agent_name):
            return None
        patched = _sanitize_tool_ids(callback_context, llm_request)
        self._state.record_patched_tool_ids(callback_context, patched)
        return None


class OpenPpxUsageMetricsPlugin(BasePlugin):
    """Persist token usage metrics independently from debug logging."""

    def __init__(
        self,
        *,
        state: OpenPpxModelCallbackState,
        target_agent_name: str | None = None,
    ) -> None:
        super().__init__(name="openppx_usage_metrics")
        self._state = state
        self._target_agent_name = target_agent_name

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        if not _matches_target_agent(callback_context, self._target_agent_name):
            return None
        self._state.record_request_meta(
            callback_context,
            _request_meta_from_context(callback_context, llm_request),
        )
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> None:
        if not _matches_target_agent(callback_context, self._target_agent_name):
            return None
        if bool(getattr(llm_response, "partial", False)):
            return None

        request_meta = self._state.pop_request_meta(callback_context)
        try:
            _write_token_usage_if_possible(
                callback_context=callback_context,
                llm_response=llm_response,
                request_meta=request_meta,
            )
        except Exception:
            # Token accounting must never block the main response path.
            pass
        return None

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> None:
        _ = llm_request, error
        self._state.discard_callback_context(callback_context)
        return None

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        self._state.discard_invocation_context(invocation_context)
        return None


class OpenPpxDebugTracePlugin(BasePlugin):
    """Emit redacted model request and response payloads when debug is enabled."""

    def __init__(
        self,
        *,
        state: OpenPpxModelCallbackState,
        target_agent_name: str | None = None,
    ) -> None:
        super().__init__(name="openppx_debug_trace")
        self._state = state
        self._target_agent_name = target_agent_name

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        if not _matches_target_agent(callback_context, self._target_agent_name):
            return None
        if not debug_logging_enabled():
            return None

        request_meta = _request_meta_from_context(callback_context, llm_request)
        texts = _request_texts(llm_request)
        payload = {
            "invocation_id": request_meta["invocation_id"],
            "session_id": request_meta["session_id"],
            "agent": getattr(callback_context, "agent_name", ""),
            "user_id": getattr(callback_context, "user_id", ""),
            "model": request_meta["model"],
            "tools": sorted((getattr(llm_request, "tools_dict", {}) or {}).keys()),
            "messages": [
                {"role": row["role"], "text": _clip(_redact(row["text"]))}
                for row in texts
            ],
        }
        patched = self._state.patched_tool_ids(callback_context)
        if patched:
            payload["patched_tool_ids"] = patched
        _write_debug("llm.before_model", payload)
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> None:
        if not _matches_target_agent(callback_context, self._target_agent_name):
            return None
        if not debug_logging_enabled():
            return None

        payload = {
            "invocation_id": getattr(callback_context, "invocation_id", ""),
            "session_id": getattr(getattr(callback_context, "session", None), "id", ""),
            "agent": getattr(callback_context, "agent_name", ""),
            "finish_reason": str(getattr(llm_response, "finish_reason", "") or ""),
            "partial": bool(getattr(llm_response, "partial", False)),
            "turn_complete": bool(getattr(llm_response, "turn_complete", False)),
            "error_code": getattr(llm_response, "error_code", None),
            "error_message": getattr(llm_response, "error_message", None),
            "text": _clip(_redact(_response_text(llm_response))),
        }
        _write_debug("llm.after_model", payload)
        return None


def build_openppx_llm_plugins(
    *,
    profile: str,
    target_agent_name: str | None = None,
) -> list[BasePlugin]:
    """Build the full-profile LLM plugin set with shared per-App state."""
    state = OpenPpxModelCallbackState(profile=profile)
    return [
        OpenPpxProviderCompatibilityPlugin(state=state, target_agent_name=target_agent_name),
        OpenPpxUsageMetricsPlugin(state=state, target_agent_name=target_agent_name),
        OpenPpxDebugTracePlugin(state=state, target_agent_name=target_agent_name),
    ]


def before_model_debug_callback(callback_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse | None:
    """Compatibility wrapper for older direct callback users.

    Production runtime uses the split ADK plugins above. This wrapper preserves
    the previous callable without reintroducing cross-invocation module state.
    """
    patched = _sanitize_tool_ids(callback_context, llm_request)
    request_meta = _request_meta_from_context(callback_context, llm_request)
    try:
        setattr(callback_context, _LEGACY_REQUEST_META_ATTR, request_meta)
    except Exception:
        pass

    if not debug_logging_enabled():
        return None

    texts = _request_texts(llm_request)
    payload = {
        "invocation_id": request_meta["invocation_id"],
        "session_id": request_meta["session_id"],
        "agent": getattr(callback_context, "agent_name", ""),
        "user_id": getattr(callback_context, "user_id", ""),
        "model": request_meta["model"],
        "tools": sorted((getattr(llm_request, "tools_dict", {}) or {}).keys()),
        "messages": [
            {"role": row["role"], "text": _clip(_redact(row["text"]))}
            for row in texts
        ],
    }
    if patched:
        payload["patched_tool_ids"] = patched
    _write_debug("llm.before_model", payload)
    return None


def after_model_debug_callback(callback_context: CallbackContext, llm_response: LlmResponse) -> LlmResponse | None:
    """Compatibility wrapper for older direct callback users."""
    if not bool(getattr(llm_response, "partial", False)):
        request_meta = getattr(callback_context, _LEGACY_REQUEST_META_ATTR, {})
        try:
            delattr(callback_context, _LEGACY_REQUEST_META_ATTR)
        except Exception:
            pass
        try:
            _write_token_usage_if_possible(
                callback_context=callback_context,
                llm_response=llm_response,
                request_meta=request_meta if isinstance(request_meta, dict) else {},
            )
        except Exception:
            pass

    if not debug_logging_enabled():
        return None

    payload = {
        "invocation_id": getattr(callback_context, "invocation_id", ""),
        "session_id": getattr(getattr(callback_context, "session", None), "id", ""),
        "agent": getattr(callback_context, "agent_name", ""),
        "finish_reason": str(getattr(llm_response, "finish_reason", "") or ""),
        "partial": bool(getattr(llm_response, "partial", False)),
        "turn_complete": bool(getattr(llm_response, "turn_complete", False)),
        "error_code": getattr(llm_response, "error_code", None),
        "error_message": getattr(llm_response, "error_message", None),
        "text": _clip(_redact(_response_text(llm_response))),
    }
    _write_debug("llm.after_model", payload)
    return None
