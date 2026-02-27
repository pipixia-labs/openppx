# 多智能体阶段状态（v1 / v1.1）

本文用于记录当前多智能体能力的实施进度，便于后续迭代排期。

## 1. 已完成（已上线）

### 1.1 路由与会话

- 路由键：`channel + accountId + peer`
- 路由优先级：`peer > account > channel > default`
- DM 会话：每个 DM 独立会话（并区分 `accountId`）
- 同 channel 多 accountId 路由：已支持

### 1.2 每 agent 独立策略

- `security`：`restrictToWorkspace/allowExec/allowNetwork/execAllowlist`
- `fs`：`workspaceOnly/allowedPaths/denyPaths/readOnlyPaths`
- `tools`：`allow/deny`
- `skills`：allowlist
- `systemPermissions`：`browser/gui/screenshot`

### 1.3 每 agent 独立认证存储

- OpenAI Codex OAuth：按 `agentDir/auth/openai_codex/...` 隔离
- GitHub Copilot token：按 `agentDir/auth/github_copilot/...` 隔离

### 1.4 通道元数据规范化

- WhatsApp：补齐 `accountId + peer` 相关 metadata
- Telegram/Discord/Feishu：补齐 `peer_kind/peer_id/peer/chat_type`

### 1.5 可观测与诊断

- Gateway 出站 metadata 增加 `openheron_route`
- `doctor` 新增多智能体校验（默认 agent、重复 id、binding 指向、channel 必填）
- `doctor` 新增 `multiAgent.summary` 与 `multiAgent.routePreview`
- `doctor` 文本模式冲突提示与排查指引
- `routes lint` 独立命令（含 `--json/--limit` 与建议动作输出）

### 1.6 文档与模板

- 多智能体配置模板：`docs/examples/multi-agent.v1.config.json`
- 迁移文档：`docs/MULTI_AGENT_MIGRATION.md`
- 运维诊断说明：`docs/OPERATIONS.md`（multiAgent 章节）
- 发布前检查清单：`docs/MULTI_AGENT_RELEASE_CHECKLIST.md`

## 2. 待推进（建议优先级）

1. 路由命中统计与审计：按 channel/account/agent 聚合最近命中情况。
2. 更细粒度主体模型扩展：guild/team/roles（当前 v1 未覆盖）。
3. 通道侧 accountId 标准化约定文档（各 channel 输入契约统一）。
4. 端到端实机验证脚本（非单元测试），用于上线前快速演练。

## 3. 已验证命令（最近回归）

```bash
./.venv/bin/python -m pytest -q tests/test_agent_routing.py tests/test_bus_gateway.py tests/test_whatsapp_channel.py tests/test_telegram_channel.py tests/test_discord_channel.py tests/test_feishu_channel.py tests/test_cli.py
```

最近结果：`202 passed`。

## 4. 当前结论

- v1 核心目标已完成并上线。
- v1.1 的可观测性、通道元数据一致性、doctor 诊断增强已完成并上线。
- 下一阶段建议聚焦“冲突预防 + 运行态观测 + 更细粒度主体模型扩展”。
