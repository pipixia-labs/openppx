# openheron Docs

## 文档索引

- [PROJECT_OVERVIEW.md](./PROJECT_OVERVIEW.md): 项目总体说明（架构、Session/Memory、Compaction、工具能力）
- [USE_CASES.md](./USE_CASES.md): 真实任务示例与可直接复制的提示词模板
- [OPERATIONS.md](./OPERATIONS.md): 运行方式、网关模式、WhatsApp Bridge、Cron、测试
- [CONFIGURATION.md](./CONFIGURATION.md): 配置模型、环境变量、配置样例、平台说明
- [MCP_SECURITY.md](./MCP_SECURITY.md): MCP 接入和安全策略
- [examples/multi-agent.v1.config.json](./examples/multi-agent.v1.config.json): 多智能体 v1 可直接改造的配置模板
- [MULTI_AGENT_MIGRATION.md](./MULTI_AGENT_MIGRATION.md): 单 agent 到多 agent（v1）的迁移步骤与验证清单
- [MULTI_AGENT_RELEASE_CHECKLIST.md](./MULTI_AGENT_RELEASE_CHECKLIST.md): 多智能体发布前检查清单（路由/权限/通道/auth）
- [MULTI_AGENT_STATUS.md](./MULTI_AGENT_STATUS.md): 多智能体阶段状态（已完成/待推进/回归命令，含 routes lint）

## 维护建议

- 新增功能时，优先补充 `PROJECT_OVERVIEW.md` 的对应章节
- 当单一主题内容变长时，再拆分为专项文档（如 `MEMORY.md`、`MCP_INTEGRATION.md`）
