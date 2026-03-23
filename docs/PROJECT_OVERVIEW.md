# openpipixia 项目说明

## 1. 项目定位

`openpipixia` 是一个基于 Google ADK 的轻量级 Agent 系统，目标是用尽量小的实现覆盖完整的 Agent 运行链路：

- 多渠道消息接入（local/feishu/telegram/whatsapp/discord/dingtalk/email/slack/qq）
- Skills 驱动的能力扩展（`SKILL.md`）
- 内置工具执行（文件、命令、Web、消息、定时任务、子代理）
- 可持续会话（SQLite）与可选长期记忆（ADK Memory Service）

它不是“单纯聊天机器人”，而是可落地执行任务的 Agent runtime 骨架。

## 2. 核心架构

### 2.1 关键模块

- `openpipixia/agent.py`
  - 定义根代理 `root_agent`（`LlmAgent`）
  - 注册工具（含 `PreloadMemoryTool`、`spawn_subagent`）
  - `after_agent_callback` 中调用 `add_session_to_memory()` 做记忆写入
- `openpipixia/gateway.py`
  - 网关主循环：消费 inbound，调用 ADK Runner，发布 outbound
  - 处理 `/help`、`/new` 等会话命令
- `openpipixia/runtime/runner_factory.py`
  - 统一创建 `Runner`，启用 `ResumabilityConfig` 与 `EventsCompactionConfig`
- `openpipixia/runtime/session_service.py`
  - 会话存储服务（SQLite `DatabaseSessionService`）
- `openpipixia/runtime/memory_service.py`
  - 记忆服务工厂（`in_memory` / `markdown`）
- `openpipixia/runtime/markdown_memory_service.py`
  - 本地 Markdown 记忆实现（按 `app_name/user_id` 分目录）

### 2.2 消息处理主链路

1. Channel 产生 `InboundMessage`
2. Gateway 解析消息并路由
3. Gateway 组装 `UserContent` 调用 `runner.run_async(...)`
4. ADK 流式返回事件，Gateway 合并文本输出
5. Gateway 发布 `OutboundMessage` 给目标 Channel

## 3. Session 与 Memory 机制

### 3.1 Session 模型

- 用户隔离：`user_id` 作为用户级作用域
- 会话隔离：`session_id` 作为单轮/多轮上下文容器
- 默认 session key：`{channel}:{chat_id}`（由 `InboundMessage.session_key` 生成）
- Session 持久化：SQLite，默认 `~/.openpipixia/database/sessions.db`

### 3.2 `/new` 与 `/help`

网关已内置两条会话命令：

- `/help`
  - 直接返回命令说明
  - 不调用模型
- `/new`
  - 先尝试将当前活动 session 写入 memory（若已启用 memory service）
  - 再为当前 `channel:chat_id` 绑定一个新的 ADK `session_id`
  - 后续对话进入新会话上下文
  - 不调用模型

当前实现是“进程内映射”，重启进程后会回到默认 `session_key` 路由。

### 3.3 Memory 后端

通过 `OPENPIPIXIA_MEMORY_BACKEND` 选择：

- `markdown`（默认）
  - 本地落盘到 `OPENPIPIXIA_MEMORY_MARKDOWN_DIR`
  - 默认目录：`~/.openpipixia/workspace/memory`
- `in_memory`（调试）
  - 进程内记忆，不落盘

可通过 `OPENPIPIXIA_MEMORY_ENABLED` 控制是否启用记忆（默认开启）。

### 3.4 Markdown Memory 落盘结构

当后端为 `markdown` 时，目录结构如下：

```text
<memory_root>/
  MEMORY.md
  HISTORY.md
  .event_ids.<app_name>.<user_id>.json
```

- `MEMORY.md`
  - 仅保存长期事实（偏好/上下文/关系等），每条都带原始对话时间戳
  - `search_memory` 只检索该文件
- `HISTORY.md`
  - 纯文本对话转录（append-only，只追加不改写）
- `.event_ids.<app_name>.<user_id>.json`
  - 已摄取 event id 去重索引，避免重复写入

## 4. Context Compaction（防上下文膨胀）

`runner_factory` 会为 ADK App 注入 `EventsCompactionConfig`。

可配置项：

- `OPENPIPIXIA_COMPACTION_ENABLED`（默认 `1`）
- `OPENPIPIXIA_COMPACTION_INTERVAL`（默认 `8`）
- `OPENPIPIXIA_COMPACTION_OVERLAP`（默认 `1`）
- `OPENPIPIXIA_COMPACTION_TOKEN_THRESHOLD`（可选，正整数）
- `OPENPIPIXIA_COMPACTION_EVENT_RETENTION`（可选，非负整数）

注意：`TOKEN_THRESHOLD` 和 `EVENT_RETENTION` 必须成对设置；只设置一个会被忽略（防止启动时报错）。

## 5. 工具能力

内置工具覆盖以下类别：

- 文件：`read_file` / `write_file` / `edit_file` / `list_dir`
- 命令：`exec`
- 网络：`web_search` / `web_fetch`
- 通知：`message` / `message_image`
- 定时：`cron`
- 异步子任务：`spawn_subagent`
- 技能读取：`list_skills` / `read_skill`

此外支持通过 MCP 配置动态挂载外部工具集（stdio / http / sse）。

## 6. 配置与运行

### 6.1 推荐初始化

```bash
openpipixia doctor --fix
```

会生成：

- `~/.openpipixia/config.json`
- `~/.openpipixia/runtime.json`
- `~/.openpipixia/workspace`

### 6.2 常用运行方式

```bash
# 单轮调用
python -m openpipixia.cli -m "Describe what you can do"

# 网关本地模式
python -m openpipixia.cli gateway run --channels local --interactive-local

# 网关多渠道模式
openpipixia gateway run --channels local,feishu --interactive-local
```

### 6.3 测试

```bash
source .venv/bin/activate
pytest -q
```

## 7. 能力体验示例（真实任务）

为避免项目概览文档过长，真实任务示例与可复制提示词模板已拆分到独立文档：

- [`docs/USE_CASES.md`](./USE_CASES.md)

## 8. 适用场景与边界

适用：

- 需要一个可扩展、可接渠道、可调试的 ADK Agent 基座
- 快速迭代技能和工具链路

当前边界（现状）：

- `/new` 会话映射未做持久化（进程重启后失效）
- Markdown 记忆为文本追加与关键词检索，尚未引入高阶语义检索
- 渠道能力受各平台 API 和配置完备度影响

---

如需后续补充，可在 `docs/` 下继续拆分：

- `ARCHITECTURE.md`（架构细节）
- `MEMORY.md`（记忆机制专项）
- `OPERATIONS.md`（部署与运维）
- `MCP_INTEGRATION.md`（MCP 接入规范）
