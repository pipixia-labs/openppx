# Channel Scope 契约（guild/team/roles）

本文定义多智能体 `match.guild / match.team / match.roles` 在各 channel 的可用性约定。

## 1. 总体规则

- 路由优先级仍是：`peer > account > channel > default`。
- scope（`guild/team/roles`）是各 tier 内的附加过滤条件。
- 若 scope 不匹配，会回退到同 tier 下一个可命中规则，最终可回退到 default。

## 2. 通道能力矩阵（当前）

| Channel | guildId | teamId | roles | 说明 |
|---|---|---|---|---|
| discord | yes (best-effort) | yes (best-effort) | yes (best-effort) | 上游事件提供时透传；建议在真实流量下验证 |
| telegram | no | no | no | 协议侧通常无对应主体模型 |
| feishu | no (v1) | no (v1) | no (v1) | 当前主要是 peer/chat_type 语义 |
| whatsapp | no | no | no | 当前聚焦 accountId + peer |
| local | no | no | no | 本地调试通道，无主体模型 |

## 3. 配置建议

- 需要稳定 scope 路由时，优先在 `discord` 场景使用，并做实机验证。
- 对于不提供 scope 的 channel，不建议配置 `match.guild/team/roles`，避免“看起来可配但永远命不中”。
- 可通过 `openheron routes lint --json` 观察 `warnings` 字段中的可达性提示。

## 4. 验证命令

```bash
openheron routes lint --json
openheron routes stats --json --window-hours 24
```

重点检查：

- `routes lint --json` 的 `warnings` 是否提示了 scope 可达性风险。
- `routes stats --json` 的 `stats.recent[*]` 是否出现 `guildId/teamId/roles`。
