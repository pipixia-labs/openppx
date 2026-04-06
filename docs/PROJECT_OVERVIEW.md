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
  - 默认目录：`~/.openpipixia/<agent_name>/memory`
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
ppx doctor --fix
```

会生成：

- `~/.openpipixia/<agent_name>/config.json`
- `~/.openpipixia/<agent_name>/runtime.json`
- `~/.openpipixia/<agent_name>/AGENTS.md` 等 agent 元信息文件
- `agent.workspace` 指向的独立工作目录

### 6.1.1 Agent 配置目录核心文件约定

每个 agent 的配置目录除了 `config.json`、`runtime.json` 外，还会包含一组用于定义 agent 行为、能力和状态的核心文件。`workspace` 只保留任务输入输出文件、代码和临时产物。

#### `IDENTITY.md`

定义 agent 的稳定身份，用于回答“这个 agent 是谁”。

建议承载的内容：

- agent 名称；
- 基本角色定位；
- 稳定的自我描述；
- 不应频繁变化的身份属性。

它应尽量短、稳定，不应混入临时任务信息。

#### `SOUL.md`

定义 agent 的价值观、行为倾向和表达风格，用于回答“这个 agent 以什么原则做事”。

建议承载的内容：

- 价值排序；
- 行为风格；
- 沟通偏好；
- 遇到冲突时优先遵守的原则。

如果说 `IDENTITY.md` 更强调“我是谁”，那么 `SOUL.md` 更强调“我按什么气质和原则工作”。

#### `AGENTS.md`

定义 agent 的高层行为规则和协作约定，用于回答“这个 agent 工作时遵守什么规则”。

建议承载的内容：

- 接到任务后如何行动；
- 采取写操作前如何沟通；
- 如何使用工具；
- 如何处理不确定性；
- 如何与用户或其他 agent 协作；
- 如何使用和维护记忆。

#### `USER.md`

定义用户画像和交互偏好，用于帮助 agent 更贴合当前用户。

建议承载的内容：

- 用户背景；
- 技术水平；
- 沟通风格偏好；
- 任务上下文中的特殊要求；
- 对输出长度、解释方式等的偏好。

#### `TOOLS.md`

定义当前 agent 可见的工具集合及其使用边界。

建议承载的内容：

- 有哪些工具可用；
- 每个工具的用途；
- 使用限制和安全边界；
- 推荐或禁止的调用方式。

`TOOLS.md` 定义的是动作接口层，而不是方法模板层。

#### `skills/`

`skills/` 用于存放可复用的技能单元。

每个 skill 本质上是一套完成某类任务的方法模板，通常包括：

- 适用场景；
- 输入输出约定；
- 推荐步骤；
- 对工具的组织方式；
- 领域经验和约束。

`skills/` 与 `TOOLS.md` 不同：

- `TOOLS.md` 回答“能做什么动作”；
- `skills/` 回答“怎样组织这些动作完成某类任务”。

#### `memory/`

`memory/` 用于存放长期记忆，而不是一次任务中的所有临时过程。

其中通常包括：

- `memory/MEMORY.md`
  - 长期有效、可复用的事实，例如用户偏好、项目背景、稳定上下文。
- `memory/HISTORY.md`
  - 原始历史记录或对话轨迹，偏 append-only 日志。

可以把它理解为：

- `MEMORY.md` 偏提炼后的长期记忆；
- `HISTORY.md` 偏原始历史轨迹。

#### `HEARTBEAT.md`

`HEARTBEAT.md` 用于定义或记录周期性任务、持续关注事项和运行期心跳任务。

建议承载的内容：

- 定时检查任务；
- 周期性提醒；
- 持续跟踪的待办；
- 正在运行的周期任务状态。

它更接近运行时状态层，而不是稳定身份层。

### 6.2 常用运行方式

```bash
# 单轮调用
python -m openpipixia.cli -m "Describe what you can do"

# 网关本地模式
python -m openpipixia.cli gateway run --channels local --interactive-local

# 网关多渠道模式
ppx gateway run --channels local,feishu --interactive-local
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
