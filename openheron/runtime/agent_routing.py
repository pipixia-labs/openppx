"""Multi-agent routing and per-agent runtime context resolution.

v1 scope:
- bindings match keys: channel, accountId, peer(kind+id), optional guild/team/roles
- deterministic precedence: peer > account > channel > default
- DM session isolation: per-peer (and account-aware)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..bus.events import InboundMessage
from .agent_runtime import AgentRuntimeContext


_DEFAULT_AGENT_ID = "main"
_DEFAULT_ACCOUNT_ID = "default"


def _default_agent_home(agent_id: str) -> Path:
    return Path.home() / ".openheron" / "agents" / agent_id


def _expand_agent_path(raw: Any, *, agent_id: str) -> str:
    text = _normalize_text(raw)
    if not text:
        return ""
    return text.replace("{agentId}", agent_id).replace("{agent_id}", agent_id)


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_lower(value: Any) -> str:
    return _normalize_text(value).lower()


def _normalize_agent_id(value: Any) -> str:
    raw = _normalize_lower(value)
    if not raw:
        return _DEFAULT_AGENT_ID
    out = []
    for ch in raw:
        if ch.isalnum() or ch in {"_", "-"}:
            out.append(ch)
        else:
            out.append("-")
    normalized = "".join(out).strip("-")
    return normalized or _DEFAULT_AGENT_ID


def _normalize_account_id(value: Any) -> str:
    normalized = _normalize_lower(value)
    return normalized or _DEFAULT_ACCOUNT_ID


def _normalize_peer_kind(value: Any) -> str:
    raw = _normalize_lower(value)
    if raw in {"dm", "direct"}:
        return "direct"
    if raw in {"group"}:
        return "group"
    if raw in {"channel", "room"}:
        return "channel"
    return "direct"


def _normalize_roles(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    normalized = {_normalize_lower(item) for item in value if _normalize_lower(item)}
    return tuple(sorted(normalized))


def _resolve_message_account_id(msg: InboundMessage) -> str:
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    return _normalize_account_id(metadata.get("account_id") or metadata.get("accountId"))


def _resolve_message_peer(msg: InboundMessage) -> tuple[str, str]:
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    peer = metadata.get("peer") if isinstance(metadata.get("peer"), dict) else {}
    kind = _normalize_peer_kind(
        peer.get("kind")
        or metadata.get("peer_kind")
        or metadata.get("peerKind")
        or metadata.get("chat_type")
        or "direct"
    )
    peer_id = _normalize_text(peer.get("id") or metadata.get("peer_id") or metadata.get("peerId") or msg.chat_id)
    return kind, peer_id or "unknown"


def _resolve_message_scope(msg: InboundMessage) -> tuple[str, str, tuple[str, ...]]:
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    guild = metadata.get("guild") if isinstance(metadata.get("guild"), dict) else {}
    team = metadata.get("team") if isinstance(metadata.get("team"), dict) else {}
    guild_id = _normalize_lower(guild.get("id") or metadata.get("guild_id") or metadata.get("guildId"))
    team_id = _normalize_lower(team.get("id") or metadata.get("team_id") or metadata.get("teamId"))
    roles = _normalize_roles(metadata.get("roles") or metadata.get("role_ids"))
    return guild_id, team_id, roles


@dataclass(frozen=True, slots=True)
class BindingMatch:
    """Binding match spec for one route rule."""

    channel: str
    account_id: str | None = None
    peer_kind: str | None = None
    peer_id: str | None = None
    guild_id: str | None = None
    team_id: str | None = None
    roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentBinding:
    """One binding entry mapping inbound scope to an agent id."""

    agent_id: str
    match: BindingMatch


@dataclass(frozen=True, slots=True)
class RoutedAgentRequest:
    """Resolved route output for one inbound message."""

    agent_id: str
    matched_by: str
    account_id: str
    peer_kind: str
    peer_id: str
    guild_id: str
    team_id: str
    roles: tuple[str, ...]
    session_id: str
    session_base_key: str
    scoped_user_id: str
    runtime: AgentRuntimeContext


class AgentRouter:
    """Resolve inbound messages to agent-specific runtime/session context."""

    def __init__(self, config: dict[str, Any] | None):
        self._config = config or {}
        self._defaults = self._resolve_agent_defaults(self._config)
        self._agents = self._resolve_agents(self._config)
        self._bindings = self._resolve_bindings(self._config)

    @staticmethod
    def _resolve_agent_defaults(config: dict[str, Any]) -> dict[str, Any]:
        agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
        defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
        return defaults

    def _resolve_agents(self, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
        entries = agents.get("list") if isinstance(agents.get("list"), list) else []
        resolved: dict[str, dict[str, Any]] = {}
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            agent_id = _normalize_agent_id(raw.get("id"))
            merged = {**self._defaults, **raw}
            resolved[agent_id] = merged
        if not resolved:
            # Keep runtime operable even with incomplete config.
            resolved[_DEFAULT_AGENT_ID] = {
                **self._defaults,
                "id": _DEFAULT_AGENT_ID,
                "default": True,
            }
        return resolved

    def _resolve_bindings(self, config: dict[str, Any]) -> list[AgentBinding]:
        raw_bindings = config.get("bindings") if isinstance(config.get("bindings"), list) else []
        out: list[AgentBinding] = []
        for raw in raw_bindings:
            if not isinstance(raw, dict):
                continue
            match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
            channel = _normalize_lower(match.get("channel"))
            if not channel:
                continue
            account_id_raw = match.get("accountId")
            account_id = None
            if account_id_raw is not None:
                account_id = _normalize_account_id(account_id_raw)
            peer = match.get("peer") if isinstance(match.get("peer"), dict) else {}
            peer_kind = _normalize_peer_kind(peer.get("kind")) if peer else None
            peer_id = _normalize_text(peer.get("id")) if peer else None
            guild = match.get("guild") if isinstance(match.get("guild"), dict) else {}
            team = match.get("team") if isinstance(match.get("team"), dict) else {}
            out.append(
                AgentBinding(
                    agent_id=_normalize_agent_id(raw.get("agentId")),
                    match=BindingMatch(
                        channel=channel,
                        account_id=account_id,
                        peer_kind=peer_kind,
                        peer_id=peer_id or None,
                        guild_id=_normalize_lower(guild.get("id")) or None,
                        team_id=_normalize_lower(team.get("id")) or None,
                        roles=_normalize_roles(match.get("roles")),
                    ),
                )
            )
        return out

    def _default_agent_id(self) -> str:
        explicit_default = None
        first = None
        for agent_id, cfg in self._agents.items():
            if first is None:
                first = agent_id
            if bool(cfg.get("default")) and explicit_default is None:
                explicit_default = agent_id
        return explicit_default or first or _DEFAULT_AGENT_ID

    def _resolve_agent_id(
        self,
        *,
        channel: str,
        account_id: str,
        peer_kind: str,
        peer_id: str,
        guild_id: str,
        team_id: str,
        roles: tuple[str, ...],
    ) -> tuple[str, str]:
        channel_bindings = [b for b in self._bindings if b.match.channel == channel]
        role_set = set(roles)

        def _scope_matches(binding: AgentBinding) -> bool:
            match = binding.match
            if match.guild_id and match.guild_id != guild_id:
                return False
            if match.team_id and match.team_id != team_id:
                return False
            if match.roles and not set(match.roles).issubset(role_set):
                return False
            return True

        # Tier 1: exact peer match (channel + optional account + peer)
        for binding in channel_bindings:
            if not (binding.match.peer_kind and binding.match.peer_id):
                continue
            if binding.match.peer_kind != peer_kind or binding.match.peer_id != peer_id:
                continue
            if binding.match.account_id is not None and binding.match.account_id != account_id:
                continue
            if not _scope_matches(binding):
                continue
            return binding.agent_id, "binding.peer"

        # Tier 2: account match (channel + account)
        for binding in channel_bindings:
            if binding.match.peer_kind or binding.match.peer_id:
                continue
            if binding.match.account_id is None:
                continue
            if binding.match.account_id == account_id:
                if not _scope_matches(binding):
                    continue
                return binding.agent_id, "binding.account"

        # Tier 3: channel match (channel only)
        for binding in channel_bindings:
            if binding.match.peer_kind or binding.match.peer_id:
                continue
            if binding.match.account_id is not None:
                continue
            if not _scope_matches(binding):
                continue
            return binding.agent_id, "binding.channel"

        return self._default_agent_id(), "default"

    def _resolve_system_permissions(self, merged_agent_cfg: dict[str, Any]) -> dict[str, bool]:
        raw = merged_agent_cfg.get("systemPermissions")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, bool] = {}
        for key, value in raw.items():
            normalized_key = _normalize_lower(key)
            if not normalized_key:
                continue
            out[normalized_key] = bool(value)
        return out

    @staticmethod
    def _resolve_path_list(raw: Any, *, workspace: Path) -> tuple[Path, ...]:
        if not isinstance(raw, list):
            return ()
        out: list[Path] = []
        for item in raw:
            text = _normalize_text(item)
            if not text:
                continue
            p = Path(text).expanduser()
            if not p.is_absolute():
                p = workspace / p
            out.append(p.resolve(strict=False))
        return tuple(out)

    def _build_runtime_context(self, agent_id: str, merged_agent_cfg: dict[str, Any]) -> AgentRuntimeContext:
        workspace_text = _expand_agent_path(merged_agent_cfg.get("workspace"), agent_id=agent_id)
        if not workspace_text:
            workspace_text = str(_default_agent_home(agent_id) / "workspace")
        agent_dir_text = _expand_agent_path(merged_agent_cfg.get("agentDir"), agent_id=agent_id)
        if not agent_dir_text:
            agent_dir_text = str(_default_agent_home(agent_id))
        workspace = Path(workspace_text).expanduser().resolve(strict=False)
        agent_dir = Path(agent_dir_text).expanduser().resolve(strict=False)

        security = merged_agent_cfg.get("security") if isinstance(merged_agent_cfg.get("security"), dict) else {}
        tools = merged_agent_cfg.get("tools") if isinstance(merged_agent_cfg.get("tools"), dict) else {}
        fs = merged_agent_cfg.get("fs") if isinstance(merged_agent_cfg.get("fs"), dict) else {}

        skills_allow = ()
        raw_skills = merged_agent_cfg.get("skills")
        if isinstance(raw_skills, list):
            skills_allow = tuple({str(item).strip() for item in raw_skills if str(item).strip()})

        return AgentRuntimeContext(
            agent_id=agent_id,
            workspace_root=workspace,
            agent_dir=agent_dir,
            allow_exec=bool(security.get("allowExec", True)),
            allow_network=bool(security.get("allowNetwork", True)),
            restrict_to_workspace=bool(security.get("restrictToWorkspace", False)),
            exec_allowlist=tuple(
                str(item).strip()
                for item in (security.get("execAllowlist") if isinstance(security.get("execAllowlist"), list) else [])
                if str(item).strip()
            ),
            tools_allow=tuple(
                str(item).strip().lower()
                for item in (tools.get("allow") if isinstance(tools.get("allow"), list) else [])
                if str(item).strip()
            ),
            tools_deny=tuple(
                str(item).strip().lower()
                for item in (tools.get("deny") if isinstance(tools.get("deny"), list) else [])
                if str(item).strip()
            ),
            skills_allow=skills_allow,
            fs_allowed_paths=self._resolve_path_list(fs.get("allowedPaths"), workspace=workspace),
            fs_deny_paths=self._resolve_path_list(fs.get("denyPaths"), workspace=workspace),
            fs_read_only_paths=self._resolve_path_list(fs.get("readOnlyPaths"), workspace=workspace),
            fs_workspace_only=bool(fs.get("workspaceOnly", False)),
            system_permissions=self._resolve_system_permissions(merged_agent_cfg),
        )

    def resolve(self, msg: InboundMessage) -> RoutedAgentRequest:
        """Resolve one inbound message into routed agent runtime/session context."""

        channel = _normalize_lower(msg.channel) or "local"
        account_id = _resolve_message_account_id(msg)
        peer_kind, peer_id = _resolve_message_peer(msg)
        guild_id, team_id, roles = _resolve_message_scope(msg)
        agent_id, matched_by = self._resolve_agent_id(
            channel=channel,
            account_id=account_id,
            peer_kind=peer_kind,
            peer_id=peer_id,
            guild_id=guild_id,
            team_id=team_id,
            roles=roles,
        )

        merged_agent_cfg = self._agents.get(agent_id, self._agents.get(self._default_agent_id(), {}))
        runtime = self._build_runtime_context(agent_id, merged_agent_cfg)

        # v1: DM per-peer isolation (account-aware), group/channel peer isolation.
        session_base_key = f"agent:{agent_id}:{channel}:{account_id}:{peer_kind}:{peer_id}"
        session_id = session_base_key
        scoped_user_id = f"agent:{agent_id}:{_normalize_text(msg.sender_id)}"

        return RoutedAgentRequest(
            agent_id=agent_id,
            matched_by=matched_by,
            account_id=account_id,
            peer_kind=peer_kind,
            peer_id=peer_id,
            guild_id=guild_id,
            team_id=team_id,
            roles=roles,
            session_id=session_id,
            session_base_key=session_base_key,
            scoped_user_id=scoped_user_id,
            runtime=runtime,
        )
