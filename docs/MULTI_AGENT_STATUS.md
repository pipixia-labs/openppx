# 多智能体阶段状态（v1 / v1.1）

本文用于记录当前多智能体能力的实施进度，便于后续迭代排期。

## 1. 已完成（已上线）

### 1.1 路由与会话

- 路由键：`channel + accountId + peer`
- 路由优先级：`peer > account > channel > default`
- 可选 scope 过滤：`guild/team/roles`（在各 tier 内生效，不改变 tier 顺序）
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
- `guild/team/roles` 配置校验 + `routeScopeCount` 摘要统计 + 运行时匹配过滤
- `routes stats` 独立命令（路由命中统计与审计，含 `--json/--limit/--window-hours`，文本模式 Top 排序 + Recent samples 预览）

### 1.6 文档与模板

- 多智能体配置模板：`docs/examples/multi-agent.v1.config.json`
- 迁移文档：`docs/MULTI_AGENT_MIGRATION.md`
- channel accountId 输入契约：`docs/CHANNEL_ACCOUNTID_CONTRACT.md`
- 运维诊断说明：`docs/OPERATIONS.md`（multiAgent 章节）
- 发布前检查清单：`docs/MULTI_AGENT_RELEASE_CHECKLIST.md`

### 1.7 上线演练脚本

- `scripts/multi_agent_e2e.sh`：聚合 `doctor/routes lint/routes stats` 的一键演练脚本
- 支持 `--with-gateway-probe`（尝试产生本地流量）与 `--strict-routes-stats`（无快照即失败）

## 2. 待推进（建议优先级）

1. 通道侧 `guild/team/roles` 元数据标准化（目前已支持匹配能力，但不同 channel 注入一致性仍可继续增强）。

## 3. 已验证命令（最近回归）

```bash
./.venv/bin/python -m pytest -q tests/test_agent_routing.py tests/test_bus_gateway.py tests/test_whatsapp_channel.py tests/test_telegram_channel.py tests/test_discord_channel.py tests/test_feishu_channel.py tests/test_cli.py
```

最近结果：`212 passed`。

## 4. 当前结论

- v1 核心目标已完成并上线。
- v1.1 的可观测性、通道元数据一致性、doctor 诊断增强已完成并上线。
- 下一阶段建议聚焦“通道侧主体元数据标准化 + 实机流量验证”。
