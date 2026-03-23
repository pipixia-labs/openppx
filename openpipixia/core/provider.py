"""Provider resolution helpers for openpipixia runtime."""

from __future__ import annotations

import importlib.util
import json
import os
from typing import Any

from .provider_registry import (
    PROVIDERS,
    RUNTIME_CODEX,
    RUNTIME_GOOGLE,
    RUNTIME_LITELLM,
    find_provider_spec,
    provider_api_key_env_names,
    provider_names as _registry_provider_names,
)

DEFAULT_PROVIDER = "google"
_PROVIDER_ALIASES: dict[str, str] = {
    "codex": "openai_codex",
    "copilot": "github_copilot",
    "kimi": "moonshot",
    "zai": "zhipu",
    "qwen": "dashscope",
}


def canonical_provider_name(raw: str | None) -> str:
    """Normalize provider name and map supported aliases to canonical names."""
    name = (raw or "").strip().lower().replace("-", "_")
    return _PROVIDER_ALIASES.get(name, name)


def normalize_provider_name(raw: str | None) -> str:
    """Normalize provider name from env/config."""
    name = canonical_provider_name(raw)
    if not name:
        return DEFAULT_PROVIDER
    return name if find_provider_spec(name) else DEFAULT_PROVIDER


def default_model_for_provider(provider: str) -> str:
    """Return provider-specific default model name."""
    spec = find_provider_spec(provider) or find_provider_spec(DEFAULT_PROVIDER)
    if spec is None:
        raise RuntimeError("Provider registry is missing default provider spec.")
    return spec.default_model


def normalize_model_name(provider: str, raw_model: str | None) -> str:
    """Normalize provider model value with safe defaults."""
    spec = find_provider_spec(provider) or find_provider_spec(DEFAULT_PROVIDER)
    if spec is None:
        raise RuntimeError("Provider registry is missing default provider spec.")
    model = (raw_model or "").strip() or spec.default_model

    if spec.runtime != RUNTIME_LITELLM:
        return model

    if spec.strip_model_prefix and "/" in model:
        model = model.split("/")[-1]

    if spec.litellm_prefix and not any(model.startswith(prefix) for prefix in spec.skip_prefixes):
        if not model.startswith(f"{spec.litellm_prefix}/"):
            model = f"{spec.litellm_prefix}/{model}"
    return model


def provider_api_key_env(provider: str) -> str | None:
    """Return the API key env var used by a provider."""
    spec = find_provider_spec(provider)
    return spec.api_key_env if spec else None


def provider_names() -> tuple[str, ...]:
    """Return supported provider names in selection priority order."""
    return _registry_provider_names()


def provider_api_key_env_keys() -> tuple[str, ...]:
    """Return the full API-key env key set managed by provider config."""
    return provider_api_key_env_names()


def provider_default_api_base(provider: str) -> str:
    """Return provider default api_base, or empty string when unset."""
    spec = find_provider_spec(provider)
    if spec is None:
        return ""
    return (spec.default_api_base or "").strip()


def validate_provider_runtime(provider: str) -> str | None:
    """Validate whether provider runtime dependencies are available."""
    spec = find_provider_spec(provider)
    if spec is None:
        supported = ", ".join(sorted(provider_names()))
        return f"Provider '{provider}' is not supported by runtime yet (supported: {supported})."

    if spec.runtime == RUNTIME_LITELLM and importlib.util.find_spec("litellm") is None:
        return (
            f"Provider '{provider}' requires `litellm`. "
            "Install dependencies with: pip install -e ."
        )

    if spec.runtime == RUNTIME_CODEX:
        if importlib.util.find_spec("httpx") is None:
            return (
                "Provider 'openai_codex' requires `httpx`. "
                "Install dependencies with: pip install -e ."
            )
        if importlib.util.find_spec("oauth_cli_kit") is None:
            return (
                "Provider 'openai_codex' requires `oauth-cli-kit`. "
                "Install dependencies with: pip install -e ."
            )

    if spec.runtime not in {RUNTIME_GOOGLE, RUNTIME_LITELLM, RUNTIME_CODEX}:
        if spec.unsupported_reason:
            return spec.unsupported_reason
        return f"Provider '{provider}' is not supported by runtime yet."

    return None


def build_adk_model_from_env() -> Any:
    """Build ADK model object/string based on selected provider."""
    provider = normalize_provider_name(os.getenv("OPENPIPIXIA_PROVIDER"))
    model_name = normalize_model_name(provider, os.getenv("OPENPIPIXIA_MODEL"))
    issue = validate_provider_runtime(provider)
    if issue:
        raise RuntimeError(issue)

    spec = find_provider_spec(provider)
    if spec is None:
        raise RuntimeError(f"Unsupported provider '{provider}'.")

    if spec.runtime == RUNTIME_GOOGLE:
        return model_name

    if spec.runtime == RUNTIME_LITELLM:
        from google.adk.models.lite_llm import LiteLlm

        kwargs: dict[str, Any] = {"drop_params": True}

        api_key_env = provider_api_key_env(provider)
        if api_key_env:
            api_key = os.getenv(api_key_env, "").strip()
            if api_key:
                kwargs["api_key"] = api_key

        api_base = os.getenv("OPENPIPIXIA_PROVIDER_API_BASE", "").strip() or provider_default_api_base(provider)
        if api_base:
            kwargs["api_base"] = api_base

        raw_headers = os.getenv("OPENPIPIXIA_PROVIDER_EXTRA_HEADERS_JSON", "").strip()
        if raw_headers:
            try:
                parsed = json.loads(raw_headers)
            except Exception:
                parsed = {}
            if isinstance(parsed, dict) and parsed:
                kwargs["extra_headers"] = parsed

        return LiteLlm(model=model_name, **kwargs)

    if spec.runtime == RUNTIME_CODEX:
        from .openai_codex_llm import OpenAICodexLlm

        codex_url = os.getenv("OPENPIPIXIA_PROVIDER_API_BASE", "").strip() or provider_default_api_base(provider)
        return OpenAICodexLlm(model=model_name, codex_url=codex_url)

    raise RuntimeError(f"Unsupported provider '{provider}'.")


def oauth_provider_names() -> tuple[str, ...]:
    """Return OAuth provider names."""
    return tuple(spec.name for spec in PROVIDERS if spec.is_oauth)
