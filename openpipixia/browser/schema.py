"""Schema helpers for browser payload compatibility."""

from __future__ import annotations

from typing import Any


PREFERRED_BROWSER_ACTION_ORDER = [
    "status",
    "profiles",
    "tabs",
    "snapshot",
    "open",
    "navigate",
    "act",
    "screenshot",
    "pdf",
    "console",
    "upload",
    "dialog",
    "focus",
    "close",
    "start",
    "stop",
]

DEFAULT_BROWSER_ERROR_CODES = [
    "browser_bad_request",
    "browser_unauthorized",
    "browser_forbidden",
    "browser_not_found",
    "browser_conflict",
    "browser_rate_limited",
    "browser_internal_error",
    "browser_not_implemented",
    "browser_bad_gateway",
    "browser_unavailable",
    "browser_timeout",
    "browser_error",
]

DEFAULT_PROXY_ERROR_CODES = [
    "proxy_http_error",
    "proxy_invalid_json",
    "proxy_invalid_payload_type",
    "proxy_timeout",
    "proxy_connection_refused",
    "proxy_dns_failed",
    "proxy_unavailable",
]


def rank_supported_actions(actions: set[str], *, preferred_order: list[str] | None = None) -> list[str]:
    """Sort actions by preferred browser order, then alphabetically."""

    order = preferred_order or PREFERRED_BROWSER_ACTION_ORDER
    return sorted(
        actions,
        key=lambda item: (
            order.index(item) if item in order else len(order),
            item,
        ),
    )


def build_action_guidance(
    actions: set[str],
    *,
    recommendation_limit: int = 5,
    preferred_order: list[str] | None = None,
) -> dict[str, list[str]]:
    """Build sorted supported actions and a capped recommended subset."""

    supported_actions = rank_supported_actions(actions, preferred_order=preferred_order)
    return {
        "supportedActions": supported_actions,
        "recommendedActions": supported_actions[: max(0, recommendation_limit)],
    }


def make_runtime_capability(
    *,
    backend: str,
    driver: str | None = None,
    mode: str | None = None,
    attach_mode: str | None = None,
    supported_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Build capability payload to describe runtime behavior."""

    capability: dict[str, Any] = {"backend": backend}
    if driver:
        capability["driver"] = driver
    if mode:
        capability["mode"] = mode
    if attach_mode:
        capability["attachMode"] = attach_mode
    if supported_actions is not None:
        capability["supportedActions"] = [str(item) for item in supported_actions]
    capability["errorCodes"] = list(DEFAULT_BROWSER_ERROR_CODES)
    return capability


def make_profile_entry(
    *,
    name: str,
    driver: str,
    description: str,
    available: bool,
    attach_mode: str | None = None,
    ownership_model: dict[str, Any] | None = None,
    requires: dict[str, Any] | None = None,
    capability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one profile payload entry with optional metadata fields."""

    entry: dict[str, Any] = {
        "name": name,
        "driver": driver,
        "description": description,
        "available": available,
    }
    if attach_mode:
        entry["attachMode"] = attach_mode
    if ownership_model is not None:
        entry["ownershipModel"] = ownership_model
    if requires is not None:
        entry["requires"] = requires
    if capability is not None:
        entry["capability"] = capability
    return entry


def apply_status_metadata(
    payload: dict[str, Any],
    *,
    attach_mode: str | None = None,
    browser_owned: bool | None = None,
    context_owned: bool | None = None,
) -> dict[str, Any]:
    """Attach optional status metadata fields to a base status payload."""

    enriched = dict(payload)
    if attach_mode is not None:
        enriched["attachMode"] = attach_mode
    if browser_owned is not None:
        enriched["browserOwned"] = browser_owned
    if context_owned is not None:
        enriched["contextOwned"] = context_owned
    return enriched


def normalize_profile_payload_aliases(payload: Any) -> Any:
    """Add compatibility aliases for browser profile/status payloads."""

    def _normalize_capability(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        capability = dict(value)
        if "attachMode" in capability and "attach_mode" not in capability:
            capability["attach_mode"] = capability.get("attachMode")
        if "supportedActions" in capability and "supported_actions" not in capability:
            capability["supported_actions"] = capability.get("supportedActions")
        if "recommendedOrder" in capability and "recommended_order" not in capability:
            capability["recommended_order"] = capability.get("recommendedOrder")
        if "errorCodes" in capability and "error_codes" not in capability:
            capability["error_codes"] = capability.get("errorCodes")
        return capability

    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    if "attachMode" in normalized and "attach_mode" not in normalized:
        normalized["attach_mode"] = normalized.get("attachMode")
    if "browserOwned" in normalized and "browser_owned" not in normalized:
        normalized["browser_owned"] = normalized.get("browserOwned")
    if "contextOwned" in normalized and "context_owned" not in normalized:
        normalized["context_owned"] = normalized.get("contextOwned")
    if "capabilityWarnings" in normalized and "capability_warnings" not in normalized:
        normalized["capability_warnings"] = normalized.get("capabilityWarnings")
    if "capability" in normalized:
        normalized["capability"] = _normalize_capability(normalized.get("capability"))

    profiles = normalized.get("profiles")
    if isinstance(profiles, list):
        next_profiles: list[Any] = []
        for entry in profiles:
            if not isinstance(entry, dict):
                next_profiles.append(entry)
                continue
            merged = dict(entry)
            if "attachMode" in merged and "attach_mode" not in merged:
                merged["attach_mode"] = merged.get("attachMode")
            if "ownershipModel" in merged and "ownership_model" not in merged:
                merged["ownership_model"] = merged.get("ownershipModel")
            if "requires" in merged and "requirements" not in merged:
                merged["requirements"] = merged.get("requires")
            if "capability" in merged:
                merged["capability"] = _normalize_capability(merged.get("capability"))
            next_profiles.append(merged)
        normalized["profiles"] = next_profiles
    return normalized
