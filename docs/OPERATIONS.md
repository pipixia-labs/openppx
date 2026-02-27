# openheron 运行与操作指南

## 安装

```bash
cd openheron
pip install -e .
```

## 初始化（推荐）

```bash
openheron install
```

`openheron install` 会执行：

- 初始化配置与工作区
- 交互式选择 provider/channel（TTY 模式）
- 交互式缺失字段复核与补填（仅交互模式）
- 运行 `openheron doctor`
- 输出安装摘要与下一步命令
- 可选一步安装并启用 gateway daemon（`--install-daemon`）

## Gateway 后台服务（进程级）

```bash
# 启动后台 gateway（写 pid/meta/log 到 ~/.openheron/log）
openheron gateway start --channels local,feishu

# 查看状态（可加 --json）
openheron gateway status
openheron gateway status --json

# 重启 / 停止
openheron gateway restart --channels local,feishu
openheron gateway stop
```

后台运行相关文件：

- `~/.openheron/log/gateway.pid`
- `~/.openheron/log/gateway.meta.json`
- `~/.openheron/log/gateway.out.log`
- `~/.openheron/log/gateway.err.log`
- `~/.openheron/log/gateway.debug.log`

常用安装命令变体：

```bash
# 标准交互式安装（推荐）
openheron install

# 仅初始化配置与工作区
openheron install --init-only

# 非交互安装（CI/远程终端常用，需要显式风险确认）
openheron install --non-interactive --accept-risk

# 重置后重新安装引导
openheron install --force

# 安装并启用用户级 gateway daemon（launchd/systemd user）
openheron install --install-daemon

# 安装 daemon 且显式指定 daemon channels
openheron install --install-daemon --daemon-channels local,feishu
```

一键 smoke（install -> doctor，可选 gateway 探活）：

```bash
scripts/install_smoke.sh
scripts/install_smoke.sh --force
scripts/install_smoke.sh --with-gateway
```

多智能体一键演练（doctor/routes lint/routes stats，可选严格 stats）：

```bash
scripts/multi_agent_e2e.sh
scripts/multi_agent_e2e.sh --with-gateway-probe
scripts/multi_agent_e2e.sh --strict-routes-stats
```

Gateway service manifest（对齐 OpenClaw install-daemon 的最小实现）：

```bash
# 写入用户级 service manifest（不直接执行 launchctl/systemctl）
openheron gateway-service install
openheron gateway-service install --force --channels local,feishu

# 写入后立即启用并启动（会调用 launchctl/systemctl --user）
openheron gateway-service install --enable

# 查看当前平台下 manifest 状态
openheron gateway-service status
openheron gateway-service status --json
```

### install 输出字段说明

安装结束后，会先看到 `Install summary`，再看到 `Install prereq`。可按下面理解：

- `Install summary: provider=..., channels=...`  
  当前启用的模型 provider 与 channel 列表。
- `Install summary: missing=[...]`  
  当前启用配置里，网关启动前建议补齐的关键字段。
- `Install summary: fixes=[...]`  
  对应 `missing` 的直接修复建议（告诉你去 `~/.openheron/config.json` 填什么）。
- `Install summary: next[1]` / `next[2]`  
  推荐下一步命令，一般是先 `openheron doctor` 再启动 `openheron gateway ...`。
- `Install prereq: ...`  
  本地环境前置检查（如 `.venv`、`adk`、可选的 `questionary/rich`）。
  在 `doctor` 文本模式下会显示为 `Install prereq [ok]: ...` 或 `Install prereq [warn]: ...`。

常见 `missing` 字段与修复路径（按目前实现）：

- provider: `<provider>.apiKey`  
  填 `providers.<provider>.apiKey`。
- feishu: `channels.feishu.appId` / `channels.feishu.appSecret`
- telegram: `channels.telegram.token`
- discord: `channels.discord.token`
- dingtalk: `channels.dingtalk.clientId` / `channels.dingtalk.clientSecret`
- slack: `channels.slack.botToken`
- whatsapp: `channels.whatsapp.bridgeUrl`
- mochat: `channels.mochat.baseUrl` / `channels.mochat.clawToken`
- email: `channels.email.consentGranted` / `channels.email.smtpHost` / `channels.email.smtpUsername` / `channels.email.smtpPassword`
- qq: `channels.qq.appId` / `channels.qq.secret`

注意：如果你在 install 交互里回车跳过了这些项，系统仍会继续安装，但 `doctor`/`gateway` 可能会提示缺失，这属于预期行为。

### 安装/修复规则单源说明（开发者）

当前 install 与 doctor 的核心配置修复规则已尽量走“单源表驱动”：

- channel env 回填规则：`CHANNEL_ENV_BACKFILL_MAPPINGS` -> `DOCTOR_CHANNEL_ENV_BACKFILL_RULES`。
- channel install summary 缺失项：由 `DOCTOR_CHANNEL_ENV_BACKFILL_RULES` 派生（并补 bool 规则，如 email consent）。
- provider install summary 与 doctor env 回填：由 `INSTALL_PROVIDER_SUMMARY_REQUIREMENTS` 驱动。
- install `fixes=[...]`：走统一渲染函数，不再在主流程内分支拼接。

当前相关代码位置：

- `openheron/doctor_rules.py`：doctor/install 共用的基础规则表与 doctor backfill 元数据。
- `openheron/install_rules.py`：install summary/prompt 规则模型与渲染、缺失项聚合逻辑。
- `openheron/onboarding_adapters.py`：provider/channel onboarding adapter 协议、默认 adapter 与注册表。
- `openheron/cli.py`：命令编排层，调用上述模块执行规则与 adapter。

建议后续扩展字段时，优先改规则表，再补测试，不要直接在流程函数里新增硬编码 if/else。

如果只想初始化文件，不跑安装流程：

```bash
openheron install --init-only
```

初始化入口统一为 `openheron install --init-only`。

初始化后会生成：

- `~/.openheron/config.json`
- `~/.openheron/runtime.json`（高级运行时 env 调优配置）
- `~/.openheron/workspace`

### install 常见问题

- `Missing ... API key`  
  打开 `~/.openheron/config.json`，给启用 provider 填 `apiKey`，再运行 `openheron doctor`。
  如果本地环境变量已配置，也可先运行 `openheron doctor --fix` 让系统自动回填缺失项。

- `channels....` 凭证字段缺失（例如 feishu/telegram/discord/dingtalk/slack/whatsapp/mochat/email/qq）  
  在 `~/.openheron/config.json` 的 `channels` 段补齐对应字段，再运行 `openheron doctor`。
  如果不确定具体字段，直接看 install 输出里的 `Install summary: fixes=[...]`。

- `MCP server ... health check failed`  
  先确认 MCP 服务进程可达，再用 `openheron doctor --json` 查看 `mcp.health` 明细错误。

- 想重新走安装向导  
  执行 `openheron install --force`（会重置配置后重新引导）。

- provider/channel 全部被关闭导致无法运行  
  执行 `openheron doctor --fix`，会自动启用默认 provider 与 `channels.local`（最小可运行修复）。

### doctor --fix --json 字段说明（新增）

当你需要把修复结果喂给上层自动化逻辑（例如告警/重试/策略回路）时，建议用：

```bash
openheron doctor --fix --json
```

`fix` 节点关键字段：

- `fix.changes`：本次实际修复项文本列表。
- `fix.summary.counts`：按 `defaults/env_backfill/legacy_migration/other` 的分类计数。
- `fix.reasonCodes`：按标准 reason code 聚合的计数（便于程序判断“主要失败/跳过原因”）。
- `fix.byRule`：按规则维度聚合（每条 rule 下 `applied/skipped/failed/total`）。

示例（节选）：

```json
{
  "fix": {
    "applied": true,
    "dryRun": false,
    "changes": ["providers.google.apiKey <- GOOGLE_API_KEY"],
    "reasonCodes": {
      "provider.env.api_key_backfilled": 1,
      "channel.env.source_missing": 1
    },
    "byRule": {
      "provider_env_backfill": {"applied": 1, "skipped": 0, "failed": 0, "total": 1},
      "channel_env_backfill": {"applied": 0, "skipped": 1, "failed": 0, "total": 1}
    },
    "summary": {
      "counts": {"defaults": 0, "env_backfill": 1, "legacy_migration": 0, "other": 0}
    }
  }
}
```

### doctor 多智能体诊断（summary / routePreview）

当你在排查多 agent 路由错配时，建议优先看：

```bash
openheron routes lint
openheron routes lint --json
openheron routes stats
openheron routes stats --json
openheron doctor --json
openheron doctor --verbose
```

说明：

- `routes lint` 是独立的静态路由检查命令（只读，不改配置）。
- `doctor` 适合看全局健康状态（provider/channel/mcp/security + multiAgent）。

`doctor --json` 中新增 `multiAgent` 关键字段：

- `multiAgent.issues`
  多智能体配置结构错误（如默认 agent 数量异常、binding 指向不存在 agent、binding 缺 channel）。
- `multiAgent.summary`
  路由汇总信息：
  - `agentCount` / `bindingCount`
  - `byChannel`
  - `routeTierCount`（`peer/account/channel`）
  - `conflicts`（同一匹配签名绑定到不同 agent）
- `multiAgent.routePreview`
  路由样例预览（含 `sessionIdExample`），便于人工快速核对会话键是否符合预期。

示例（节选）：

```json
{
  "multiAgent": {
    "issues": [],
    "summary": {
      "agentCount": 3,
      "bindingCount": 4,
      "byChannel": {"whatsapp": 3, "telegram": 1},
      "routeTierCount": {"peer": 1, "account": 2, "channel": 1},
      "conflicts": []
    },
    "routePreview": [
      {
        "type": "binding",
        "tier": "account",
        "agentId": "whatsapp_business",
        "channel": "whatsapp",
        "accountId": "business",
        "sessionIdExample": "agent:whatsapp_business:whatsapp:business:direct:sample-peer"
      }
    ]
  }
}
```

常见排错示例：

- 默认 agent 配置错误  
  现象：`multiAgent.issues` 出现 `exactly one default agent is required`。  
  处理：保证 `agents.list` 里且仅有一个 `default=true`。

- binding 指向不存在 agent  
  现象：`multiAgent.issues` 出现 `agentId 'xxx' does not exist in agents.list`。  
  处理：修正 `bindings[*].agentId`，或在 `agents.list` 新增对应 agent。

- 路由冲突（同签名多目标）  
  现象：`multiAgent.summary.conflicts` 非空。  
  处理：检查冲突 binding（按输出中的 `bindings[idx]`），同一个 `channel/accountId/peer` 签名只保留一个目标 agent。

`routes lint --json` 失败示例（节选）：

```json
{
  "ok": false,
  "issues": [],
  "summary": {
    "conflicts": [
      "bindings[1] duplicates route match ('whatsapp','business','','') but targets 'biz' (first: 'main')."
    ]
  }
}
```

推荐修复步骤：

1. 定位冲突项里提到的 `bindings[idx]`。  
2. 对同一签名只保留一个目标 agent（或收敛为更高优先级的 `peer` 规则）。  
3. 重新执行 `openheron routes lint --json`，确认 `summary.conflicts` 为空。  
4. 再执行 `openheron doctor --json` 做全链路确认。  

`routes stats` 使用说明：

- 作用：查看最近网关运行期的路由命中统计（按 agent/channel/matchedBy 聚合）。
- 多账号 channel 的 `accountId` 输入契约见：`docs/CHANNEL_ACCOUNTID_CONTRACT.md`。
- 文本模式会额外输出 Top 排名视图（按命中次数降序，受 `--limit` 控制）。
- 文本模式会输出 `Recent samples`（最近明细预览，默认最多 5 条，仍受 `--limit` 约束）。
- 数据文件：`<workspace>/.openheron/route_stats.json`。
- 常用命令：

```bash
openheron routes stats
openheron routes stats --json
openheron routes stats --limit 10
openheron routes stats --json --limit 50
openheron routes stats --json --window-hours 24
```

- 无快照时（首次运行常见）：
  1. 先启动并产生网关流量（本地或真实 channel）。
  2. 再执行 `openheron routes stats`。
  3. 若仍无数据，检查 workspace 是否正确，以及进程是否有写权限。

## 运行方式

### 单轮调用

```bash
python -m openheron.cli -m "Describe what you can do"
```

可显式指定会话标识：

```bash
python -m openheron.cli -m "Describe what you can do" --user-id local --session-id demo001
```

### ADK CLI 模式

```bash
adk run openheron
```

### Wrapper CLI

```bash
openheron run
```

### 常用工具命令

```bash
openheron skills
openheron routes lint
openheron routes lint --json
openheron routes stats
openheron routes stats --json
openheron doctor
openheron doctor --fix
openheron doctor --fix-dry-run
openheron heartbeat status
openheron heartbeat status --json
openheron token stats
openheron token stats --provider google --limit 50
openheron token stats --json
openheron gateway-service install
openheron gateway-service status
openheron provider list
openheron provider status
openheron provider status --json
openheron provider login github-copilot
openheron provider login openai-codex
openheron provider login codex
openheron channels login
openheron channels bridge start
openheron channels bridge status
openheron channels bridge stop
```

## Gateway 模式

### 本地通道

```bash
python -m openheron.cli gateway-local
```

### 多通道模式（含 Feishu）

```bash
openheron gateway --channels local,feishu --interactive-local
```

也可通过环境变量指定默认通道：

```bash
export OPENHERON_CHANNELS=feishu
openheron gateway
```

## WhatsApp Bridge

`openheron` 使用本地 Node.js Bridge（Baileys + WebSocket）完成 WhatsApp 登录和消息收发。

```bash
# 前台扫码登录
openheron channels login

# 后台 bridge 生命周期
openheron channels bridge start
openheron channels bridge status
openheron channels bridge stop
```

快速自检：

```bash
scripts/whatsapp_bridge_e2e.sh full
scripts/whatsapp_bridge_e2e.sh smoke
```

## Cron 调度

`openheron` 的 cron 是进程内调度器，不写系统 crontab。只有网关运行时任务才会执行。

- 存储文件：`OPENHERON_WORKSPACE/.openheron/cron_jobs.json`
- 支持调度：`every`、`cron`（可配 `tz`）、`at`

常用命令：

```bash
openheron cron list
openheron cron add --name weather --message "check weather and summarize" --every 300
openheron cron add --name daily --message "daily report" --cron "0 9 * * 1-5" --tz Asia/Shanghai
openheron cron add --name reminder --message "remind me to review PR" --at 2026-02-19T09:30:00
openheron cron add --name push --message "send update" --every 600 --deliver --channel feishu --to ou_xxx
openheron cron run <job_id>
openheron cron enable <job_id>
openheron cron enable <job_id> --disable
openheron cron remove <job_id>
openheron cron status
```

## Token 统计

`openheron` 会在每次 LLM 调用结束后记录 token 使用信息（请求/响应、文本/图像、时间戳）。

- 存储位置：`~/.openheron/token_usage.db`（SQLite）
- 记录粒度：每次 request/response 一条事件
- 查询命令：

```bash
openheron token stats
openheron token stats --provider google --limit 50
openheron token stats --provider openai --json
```

说明：

- `token stats` 默认输出汇总统计 + 最近记录。
- `--provider` 可按 provider 过滤（如 `google`、`openai`）。
- `--limit` 控制最近记录返回条数（默认 20）。
- `--json` 输出机器可读 JSON，适合脚本/监控接入。
- 是否能统计到该次调用，取决于 provider 是否返回 usage 信息；无 usage 的调用不会计入。

## 测试

```bash
source .venv/bin/activate
pytest -q
```

## 示例

```bash
python -m openheron.cli -m "search for the latest research progress today, and create a PPT for me."
python -m openheron.cli -m "download all PDF files from this page: https://bbs.kangaroo.study/forum.php?mod=viewthread&tid=467"
```
