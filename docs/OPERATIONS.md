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

- 初始化配置与工作区（等价 `openheron onboard`）
- 交互式选择 provider/channel（TTY 模式）
- 运行 `openheron doctor`
- 输出安装摘要与下一步命令
- 可选一步安装并启用 gateway daemon（`--install-daemon`）

常用安装命令变体：

```bash
# 标准交互式安装（推荐）
openheron install

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

建议后续扩展字段时，优先改规则表，再补测试，不要直接在流程函数里新增硬编码 if/else。

如果只想初始化文件，不跑安装流程：

```bash
openheron onboard
```

初始化后会生成：

- `~/.openheron/config.json`
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
openheron doctor
openheron doctor --fix
openheron doctor --fix-dry-run
openheron heartbeat status
openheron heartbeat status --json
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
