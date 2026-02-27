# 多智能体上线前检查清单（v1）

本文用于发布前快速自检，目标是确保多智能体路由、权限与通道元数据在生产前可控。

## 1. 配置结构检查

1. 确认只有一个默认 agent：`agents.list[*].default=true` 仅出现一次。
2. 确认所有 `bindings[*].agentId` 都存在于 `agents.list[*].id`。
3. 确认每条 binding 都有 `match.channel`。
4. 确认关键 agent 有独立 `agentDir`（用于 auth 隔离）。

## 2. doctor 诊断检查

执行：

```bash
openheron routes lint --json
openheron routes stats --json
openheron routes stats --json --window-hours 24
openheron doctor --json
openheron doctor --verbose
```

检查点：

1. `routes lint --json` 返回 `ok=true`，且 `summary.conflicts` 为空。
2. `routes stats --json` 返回 `ok=true`，并且 `stats.totalMessagesInWindow` 与预期流量量级一致。
3. `routes stats --json --window-hours 24` 的窗口统计与近期流量预期一致。
4. `doctor --json` 的 `issues` 为空。
5. `doctor --json` 的 `multiAgent.issues` 为空。
6. `doctor --json` 的 `multiAgent.summary.conflicts` 为空。
7. `doctor --json` 的 `multiAgent.routePreview` 中 `sessionIdExample` 与预期一致。

可选一键脚本（本节命令聚合）：

```bash
scripts/multi_agent_e2e.sh
scripts/multi_agent_e2e.sh --with-gateway-probe
scripts/multi_agent_e2e.sh --strict-routes-stats
```

## 3. 路由行为检查（本地）

建议至少验证以下场景：

1. 同 channel 不同 `accountId` 路由到不同 agent。
2. `peer` 级绑定优先于 `account/channel`。
3. DM 会话按 peer 隔离（同 account 下不同 peer 不串会话）。

示例测试命令：

```bash
./.venv/bin/python -m pytest -q tests/test_agent_routing.py tests/test_bus_gateway.py
```

## 4. 权限与策略检查

1. `tools.deny` 是否对高风险工具生效（如 `exec`、`spawn_subagent`）。
2. `systemPermissions` 是否符合预期（如禁用 `gui/screenshot`）。
3. `fs` 策略是否限制到目标目录（`workspaceOnly/allowedPaths/denyPaths/readOnlyPaths`）。

示例测试命令：

```bash
./.venv/bin/python -m pytest -q tests/test_agent_runtime_security.py tests/test_agent_runtime_tool_policy.py
```

## 5. 通道元数据一致性检查

确认通道入站 metadata 包含可路由字段：

- `chat_type`
- `peer_kind`
- `peer_id`
- `peer`
- （适用场景）`accountId`

示例测试命令：

```bash
./.venv/bin/python -m pytest -q tests/test_whatsapp_channel.py tests/test_telegram_channel.py tests/test_discord_channel.py tests/test_feishu_channel.py
```

## 6. OAuth 隔离检查

1. 使用目标 provider 登录（如 `openheron provider login openai-codex`）。
2. 切换不同 agent 触发登录/检查后，确认 token 存储位于各自 `agentDir/auth/...`。

示例测试命令：

```bash
./.venv/bin/python -m pytest -q tests/test_auth_paths.py tests/test_openai_codex_llm_auth.py
```

## 7. 最终回归建议（发布前）

```bash
./.venv/bin/python -m pytest -q tests/test_agent_routing.py tests/test_bus_gateway.py tests/test_agent_runtime_security.py tests/test_agent_runtime_tool_policy.py tests/test_whatsapp_channel.py tests/test_telegram_channel.py tests/test_discord_channel.py tests/test_feishu_channel.py tests/test_cli.py
```

若以上检查全部通过，再执行发布（push/tag/deploy）。
