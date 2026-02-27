# 单 Agent 配置迁移到多 Agent（v1）

本文给出从旧的单 agent 配置（`agent.*`）迁移到多智能体配置（`agents.* + bindings`）的最小步骤。

## 目标能力

- 路由键：`channel + accountId + peer`
- 路由优先级：`peer > account > channel > default`
- DM 会话隔离：每个 DM 独立会话（且区分 accountId）
- 每个 agent 独立：`workspace / agentDir / security / fs / tools / skills / systemPermissions`
- 同一 channel 支持多 accountId 路由

## 迁移步骤

1. 把旧 `agent.workspace` 迁移到 `agents.defaults.workspace`。
2. 新增 `agents.defaults.agentDir`，为默认 agent 指定独立目录。
3. 在 `agents.list` 里创建至少一个 `default=true` 的 `main` agent。
4. 为每个业务 agent 添加独立 `id/workspace/agentDir`。
5. 通过 `bindings` 配置路由规则：
   - channel 级别：`{"match": {"channel": "whatsapp"}}`
   - account 级别：`{"match": {"channel": "whatsapp", "accountId": "business"}}`
   - peer 级别：`{"match": {"channel": "whatsapp", "accountId": "business", "peer": {"kind": "direct", "id": "+1555..."}}}`
   - scope 过滤（可选）：`{"match": {"channel": "discord", "guild": {"id": "guild-ops-main"}, "roles": ["admin"]}}`
6. 将权限策略下沉到对应 agent：
   - `security`（exec/network/workspace）
   - `fs`（allowed/deny/readOnly/workspaceOnly）
   - `tools`（allow/deny）
   - `skills`（allowlist）
   - `systemPermissions`（browser/gui/screenshot）
7. 按需登录 OAuth（OpenAI Codex/GitHub Copilot）。认证文件会写到对应 `agentDir/auth/...`。
8. 若使用 `guild/team/roles`，先运行 `openheron routes lint --json` 检查 `warnings`，确认 channel 具备对应 metadata。

## 最小示例

参考：

- `docs/examples/multi-agent.v1.config.json`

## 验证清单

1. 同一 channel 下，不同 `accountId` 能命中不同 agent。
2. `/new` 只重置当前路由键对应会话，不影响其他 DM/account。
3. 为某 agent 关闭 `systemPermissions.gui=false` 后，`computer_use/computer_task` 被拒绝。
4. 为某 agent 配置 `tools.deny=["exec"]` 后，`exec` 被拒绝。
5. OAuth 登录后，token 文件路径位于该 agent 的 `agentDir/auth/...`。
