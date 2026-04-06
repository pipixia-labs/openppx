# openpipixia 运行与操作指南

## 安装

```bash
cd openpipixia
pip install -e .
```

## 初始化（推荐）

安装向导模块暂时不对外使用。请改用 `doctor` + 手动配置：

```bash
# 初始化/修复最小可运行配置
ppx doctor --fix

# 查看完整诊断结果
ppx doctor
```

## Gateway 后台服务（进程级）

```bash
# 启动后台 gateway（写 pid/meta/log 到 ~/.openppx/log）
ppx gateway start --channels local,feishu

# 查看状态（可加 --json）
ppx gateway status
ppx gateway status --json

# 重启 / 停止
ppx gateway restart --channels local,feishu
ppx gateway stop
```

后台运行相关文件：

- `~/.openppx/log/gateway.pid`
- `~/.openppx/log/gateway.meta.json`
- `~/.openppx/log/gateway.out.log`
- `~/.openppx/log/gateway.err.log`
- `~/.openppx/log/gateway.debug.log`

一键 smoke（doctor，可选 gateway 探活）：

```bash
scripts/install_smoke.sh
scripts/install_smoke.sh --force
scripts/install_smoke.sh --with-gateway
```

Gateway service manifest（对齐 OpenClaw install-daemon 的最小实现）：

```bash
# 写入用户级 service manifest（不直接执行 launchctl/systemctl）
ppx gateway-service install
ppx gateway-service install --force --channels local,feishu

# 写入后立即启用并启动（会调用 launchctl/systemctl --user）
ppx gateway-service install --enable

# 查看当前平台下 manifest 状态
ppx gateway-service status
ppx gateway-service status --json
```

### 常见缺失字段与修复路径

- provider: `<provider>.apiKey`  
  填 `providers.<provider>.apiKey`。
- feishu: `channels.feishu.appId` / `channels.feishu.appSecret`
- telegram: `channels.telegram.token`
- discord: `channels.discord.token`
- dingtalk: `channels.dingtalk.clientId` / `channels.dingtalk.clientSecret`
- slack: `channels.slack.botToken`
- whatsapp: `channels.whatsapp.bridgeUrl`
- email: `channels.email.consentGranted` / `channels.email.smtpHost` / `channels.email.smtpUsername` / `channels.email.smtpPassword`
- qq: `channels.qq.appId` / `channels.qq.secret`

### 安装/修复规则单源说明（开发者）

当前 `doctor --fix` 的核心配置修复规则已尽量走“单源表驱动”：

- channel env 回填规则：`CHANNEL_ENV_BACKFILL_MAPPINGS` -> `DOCTOR_CHANNEL_ENV_BACKFILL_RULES`。
- provider doctor env 回填：由 `INSTALL_PROVIDER_SUMMARY_REQUIREMENTS` 驱动。

当前相关代码位置：

- `openpipixia/doctor_rules.py`：doctor/install 共用的基础规则表与 doctor backfill 元数据。
- `openpipixia/onboarding_adapters.py`：provider/channel onboarding adapter 协议、默认 adapter 与注册表。
- `openpipixia/cli.py`：命令编排层，调用上述模块执行规则与 adapter。

建议后续扩展字段时，优先改规则表，再补测试，不要直接在流程函数里新增硬编码 if/else。
### 常见问题

- `Missing ... API key`  
  打开目标 Agent 的 `~/.openppx/<agent_name>/config.json`，给启用 provider 填 `apiKey`，再运行 `ppx doctor`。
  如果本地环境变量已配置，也可先运行 `ppx doctor --fix` 让系统自动回填缺失项。

- `channels....` 凭证字段缺失（例如 feishu/telegram/discord/dingtalk/slack/whatsapp/email/qq）  
  在目标 Agent 的 `~/.openppx/<agent_name>/config.json` 的 `channels` 段补齐对应字段，再运行 `ppx doctor`。
  如果不确定具体字段，直接看 `ppx doctor --json` 的缺失项。

- `MCP server ... health check failed`  
  先确认 MCP 服务进程可达，再用 `ppx doctor --json` 查看 `mcp.health` 明细错误。

- provider/channel 全部被关闭导致无法运行  
  执行 `ppx doctor --fix`，会自动启用默认 provider 与 `channels.local`（最小可运行修复）。

### doctor --fix --json 字段说明（新增）

当你需要把修复结果喂给上层自动化逻辑（例如告警/重试/策略回路）时，建议用：

```bash
ppx doctor --fix --json
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
python -m openpipixia.cli -m "Describe what you can do"
```

可显式指定会话标识：

```bash
python -m openpipixia.cli -m "Describe what you can do" --user-id local --session-id demo001
```

### ADK CLI 模式

```bash
adk run openpipixia
```

### Wrapper CLI

```bash
ppx run
```

### 常用工具命令

```bash
ppx skills
ppx doctor
ppx doctor --fix
ppx doctor --fix-dry-run
ppx heartbeat status
ppx heartbeat status --json
ppx token stats
ppx token stats --provider google --limit 50
ppx token stats --json
ppx gateway-service install
ppx gateway-service status
ppx provider list
ppx provider status
ppx provider status --json
ppx provider login github-copilot
ppx provider login openai-codex
ppx provider login codex
ppx channels login
ppx channels bridge start
ppx channels bridge status
ppx channels bridge stop
```

## Gateway 模式

### 本地通道

```bash
python -m openpipixia.cli gateway run --channels local --interactive-local
```

### 多通道模式（含 Feishu）

```bash
ppx gateway run --channels local,feishu --interactive-local
```

也可通过环境变量指定默认通道：

```bash
export OPENPPX_CHANNELS=feishu
ppx gateway
```

## WhatsApp Bridge

`openpipixia` 使用本地 Node.js Bridge（Baileys + WebSocket）完成 WhatsApp 登录和消息收发。

```bash
# 前台扫码登录
ppx channels login

# 后台 bridge 生命周期
ppx channels bridge start
ppx channels bridge status
ppx channels bridge stop
```

快速自检：

```bash
scripts/whatsapp_bridge_e2e.sh full
scripts/whatsapp_bridge_e2e.sh smoke
```

## Cron 调度

`openpipixia` 的 cron 是进程内调度器，不写系统 crontab。只有网关运行时任务才会执行。

- 存储文件：`OPENPPX_WORKSPACE/.openppx/cron_jobs.json`
- 支持调度：`every`、`cron`（可配 `tz`）、`at`

常用命令：

```bash
ppx cron list
ppx cron add --name weather --message "check weather and summarize" --every 300
ppx cron add --name daily --message "daily report" --cron "0 9 * * 1-5" --tz Asia/Shanghai
ppx cron add --name reminder --message "remind me to review PR" --at 2026-02-19T09:30:00
ppx cron add --name push --message "send update" --every 600 --deliver --channel feishu --to ou_xxx
ppx cron run <job_id>
ppx cron enable <job_id>
ppx cron enable <job_id> --disable
ppx cron remove <job_id>
ppx cron status
```

## Token 统计

`openpipixia` 会在每次 LLM 调用结束后记录 token 使用信息（请求/响应、文本/图像、时间戳）。

- 存储位置：`~/.openppx/token_usage.db`（SQLite）
- 记录粒度：每次 request/response 一条事件
- 查询命令：

```bash
ppx token stats
ppx token stats --provider google --limit 50
ppx token stats --provider openai --json
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
python -m openpipixia.cli -m "search for the latest research progress today, and create a PPT for me."
python -m openpipixia.cli -m "download all PDF files from this page: https://bbs.kangaroo.study/forum.php?mod=viewthread&tid=467"
```
