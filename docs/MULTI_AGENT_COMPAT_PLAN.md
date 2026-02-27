# 多智能体兼容层收敛计划

本文描述多智能体隔离改造后，旧路径兼容层（legacy fallback）的阶段性收敛方案。

## 背景

当前已完成的新路径：

- heartbeat 快照：`~/.openheron/agents/<agentId>/runtime/heartbeat_status.json`
- route stats 快照：`~/.openheron/agents/<agentId>/runtime/route_stats.json`

为降低升级风险，运行时仍保留了旧路径兼容读写（legacy path）。

## 目标

1. 保证线上运行稳定前提下，逐步降低兼容分支复杂度。
2. 最终仅保留 agent 路径，避免“共享 workspace 路径”造成误读。

## 分阶段策略

### Phase 0（已完成）

- 行为：新路径 + 旧路径双写，读取优先新路径，缺失时回退旧路径。
- 目的：平滑升级，兼容旧脚本和运维习惯。

### Phase 1（当前）

- 行为：仅写新路径；读取仍保留“新优先、旧回退”。
- 运维动作：
  - 更新所有运维脚本到 `--agent-id` 形式。
  - 发布告警提示：发现读取到 legacy 文件时输出 warning。

### Phase 2（建议）

- 行为：仅读新路径，不再回退旧路径。
- 运维动作：
  - 发布前做一次目录巡检，确认 agent 目录下快照齐全。
  - 删除遗留 `<workspace>/.openheron/*` 快照文件。

## 切换前检查清单

1. `openheron doctor --json` 的 `observability.byAgent` 全部可见。
2. `openheron heartbeat status --agent-id <id>` 在目标 agent 全部可用。
3. `openheron routes stats --agent-id <id>` 在目标 agent 全部可用。
4. 所有自动化脚本已不再读取 `<workspace>/.openheron/*`。

## 风险与回滚

- 风险：部分旧脚本仍依赖 legacy 路径，切换后读取失败。
- 回滚：短期可恢复 Phase 0 行为（双写 + 回退读）；长期应推动脚本升级而非长期回滚。
