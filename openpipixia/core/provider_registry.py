"""Provider registry for openpipixia.

This module is the single source of truth for provider metadata:
- Default model name
- Runtime type (Google native vs LiteLLM vs unsupported)
- API key env mapping
- Model prefix normalization rules
- OAuth login capability
"""

from __future__ import annotations

from dataclasses import dataclass

RUNTIME_GOOGLE = "google"
RUNTIME_LITELLM = "litellm"
RUNTIME_CODEX = "codex"
RUNTIME_UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ProviderSpec:
    """Metadata describing one LLM provider."""

    name: str
    default_model: str
    display_name: str
    runtime: str
    api_key_env: str | None = None
    litellm_prefix: str = ""
    skip_prefixes: tuple[str, ...] = ()
    default_api_base: str | None = None
    strip_model_prefix: bool = False
    is_oauth: bool = False
    oauth_login: str | None = None
    unsupported_reason: str = ""


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="google",
        default_model="gemini-3-flash-preview",
        display_name="Google Gemini",
        runtime=RUNTIME_GOOGLE,
        api_key_env="GOOGLE_API_KEY",
    ),
    ProviderSpec(
        name="openai",
        default_model="openai/gpt-4.1-mini",
        display_name="OpenAI",
        runtime=RUNTIME_LITELLM,
        api_key_env="OPENAI_API_KEY",
        litellm_prefix="openai",
        skip_prefixes=("openai/",),
    ),
    ProviderSpec(
        name="openrouter",
        default_model="openai/gpt-4.1-mini",
        display_name="OpenRouter",
        runtime=RUNTIME_LITELLM,
        api_key_env="OPENROUTER_API_KEY",
        litellm_prefix="openrouter",
        skip_prefixes=("openrouter/",),
        default_api_base="https://openrouter.ai/api/v1",
    ),
    ProviderSpec(
        name="anthropic",
        default_model="claude-3-7-sonnet",
        display_name="Anthropic",
        runtime=RUNTIME_LITELLM,
        api_key_env="ANTHROPIC_API_KEY",
    ),
    ProviderSpec(
        name="deepseek",
        default_model="deepseek-chat",
        display_name="DeepSeek",
        runtime=RUNTIME_LITELLM,
        api_key_env="DEEPSEEK_API_KEY",
        litellm_prefix="deepseek",
        skip_prefixes=("deepseek/",),
    ),
    ProviderSpec(
        name="groq",
        default_model="llama-3.3-70b-versatile",
        display_name="Groq",
        runtime=RUNTIME_LITELLM,
        api_key_env="GROQ_API_KEY",
        litellm_prefix="groq",
        skip_prefixes=("groq/",),
    ),
    ProviderSpec(
        name="gemini",
        default_model="gemini-2.5-flash",
        display_name="Gemini (LiteLLM)",
        runtime=RUNTIME_LITELLM,
        api_key_env="GEMINI_API_KEY",
        litellm_prefix="gemini",
        skip_prefixes=("gemini/",),
    ),
    ProviderSpec(
        name="dashscope",
        default_model="qwen-max",
        display_name="DashScope",
        runtime=RUNTIME_LITELLM,
        api_key_env="DASHSCOPE_API_KEY",
        litellm_prefix="dashscope",
        skip_prefixes=("dashscope/",),
    ),
    ProviderSpec(
        name="zhipu",
        default_model="glm-4-plus",
        display_name="Zhipu",
        runtime=RUNTIME_LITELLM,
        api_key_env="ZAI_API_KEY",
        litellm_prefix="zai",
        skip_prefixes=("zai/", "zhipu/"),
    ),
    ProviderSpec(
        name="moonshot",
        default_model="kimi-k2.5",
        display_name="Moonshot",
        runtime=RUNTIME_LITELLM,
        api_key_env="MOONSHOT_API_KEY",
        litellm_prefix="moonshot",
        skip_prefixes=("moonshot/",),
        default_api_base="https://api.moonshot.ai/v1",
    ),
    ProviderSpec(
        name="minimax",
        default_model="MiniMax-M2.1",
        display_name="MiniMax",
        runtime=RUNTIME_LITELLM,
        api_key_env="MINIMAX_API_KEY",
        litellm_prefix="minimax",
        skip_prefixes=("minimax/",),
        default_api_base="https://api.minimax.io/v1",
    ),
    ProviderSpec(
        name="aihubmix",
        default_model="openai/gpt-4.1-mini",
        display_name="AiHubMix",
        runtime=RUNTIME_LITELLM,
        api_key_env="OPENAI_API_KEY",
        litellm_prefix="openai",
        skip_prefixes=("openai/",),
        default_api_base="https://aihubmix.com/v1",
        strip_model_prefix=True,
    ),
    ProviderSpec(
        name="siliconflow",
        default_model="Qwen/Qwen3-32B",
        display_name="SiliconFlow",
        runtime=RUNTIME_LITELLM,
        api_key_env="OPENAI_API_KEY",
        litellm_prefix="openai",
        skip_prefixes=("openai/",),
        default_api_base="https://api.siliconflow.cn/v1",
    ),
    ProviderSpec(
        name="vllm",
        default_model="meta-llama/Llama-3.1-8B-Instruct",
        display_name="vLLM/Local",
        runtime=RUNTIME_LITELLM,
        api_key_env="HOSTED_VLLM_API_KEY",
        litellm_prefix="hosted_vllm",
        skip_prefixes=("hosted_vllm/",),
    ),
    ProviderSpec(
        name="custom",
        default_model="openai/gpt-4.1-mini",
        display_name="Custom OpenAI-Compatible",
        runtime=RUNTIME_LITELLM,
        api_key_env="OPENAI_API_KEY",
        litellm_prefix="openai",
        skip_prefixes=("openai/",),
        default_api_base="http://localhost:8000/v1",
    ),
    ProviderSpec(
        name="github_copilot",
        default_model="github_copilot/gpt-4o",
        display_name="GitHub Copilot",
        runtime=RUNTIME_LITELLM,
        api_key_env=None,
        litellm_prefix="github_copilot",
        skip_prefixes=("github_copilot/",),
        is_oauth=True,
        oauth_login="github_copilot",
    ),
    ProviderSpec(
        name="openai_codex",
        default_model="openai-codex/gpt-5.1-codex",
        display_name="OpenAI Codex",
        runtime=RUNTIME_CODEX,
        api_key_env=None,
        default_api_base="https://chatgpt.com/backend-api/codex/responses",
        is_oauth=True,
        oauth_login="openai_codex",
    ),
)


def find_provider_spec(name: str) -> ProviderSpec | None:
    """Find a provider spec by name."""
    for spec in PROVIDERS:
        if spec.name == name:
            return spec
    return None


def provider_names() -> tuple[str, ...]:
    """Return provider names in registry order."""
    return tuple(spec.name for spec in PROVIDERS)


def oauth_provider_names() -> tuple[str, ...]:
    """Return OAuth-enabled provider names in registry order."""
    return tuple(spec.name for spec in PROVIDERS if spec.is_oauth)


def provider_api_key_env_names() -> tuple[str, ...]:
    """Return unique non-empty API-key env names used by providers."""
    unique = {spec.api_key_env for spec in PROVIDERS if spec.api_key_env}
    return tuple(sorted(unique))
