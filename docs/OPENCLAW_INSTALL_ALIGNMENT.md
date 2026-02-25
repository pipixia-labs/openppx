# OpenClaw 安装/引导对齐清单（Openheron）

更新时间：2026-02-25

## 目标

对齐 OpenClaw 在“安装与首次引导”上的核心体验，同时保持 openheron 的 Python/ADK 轻量实现。

## 已对齐（当前完成）

1. **统一入口命令**
   - Openheron 已有 `openheron install`，可完成初始化 + 引导 + doctor。
   - 代码：`openheron/cli.py:2186`、`openheron/cli.py:1732`

2. **交互式 provider/channel 采集**
   - 已支持 provider 选择、API key 输入、channel 多选。
   - 已覆盖多 channel 最小凭证采集（feishu/telegram/discord/dingtalk/slack/whatsapp/mochat/email/qq）。
   - 代码：`openheron/cli.py:1256`、`openheron/cli.py:1470`

3. **安装摘要与修复建议**
   - 安装输出包含 `missing`、`fixes`、`next[1]/next[2]`。
   - 代码：`openheron/cli.py:1552`

4. **前置检查可观测**
   - install 与 doctor 共用前置检查；doctor 文本输出带 `[ok]/[warn]`。
   - 代码：`openheron/cli.py:645`、`openheron/cli.py:668`

5. **真实命令链路 smoke**
   - 已提供 `scripts/install_smoke.sh`，可走 `install -> doctor`，可选 gateway 探活。
   - 代码：`scripts/install_smoke.sh:1`

## 与 OpenClaw 的主要差异（待对齐）

### P0（高优先）

1. **Daemon 安装能力部分对齐（差距收敛）**
   - Openheron 已支持 `openheron gateway-service install/status`，可生成用户级 launchd/systemd manifest。
   - `openheron gateway-service install --enable` 可自动调用 launchctl/systemctl 启用服务。
   - `openheron install --install-daemon` 已支持在安装流程内一步执行 daemon 安装与启用。
   - 代码：`openheron/cli.py:2381`、`openheron/cli.py:2847`
   - 与 OpenClaw 差距：当前默认仍需显式传 `--install-daemon`，并且 daemon 失败时采用“告警不阻断主安装”策略。
   - 参考：`../openclaw/src/cli/program/register.onboard.ts:102`、`../openclaw/README.md:59`

2. **Doctor 自动修复能力部分对齐（最小闭环）**
   - Openheron 已支持 `openheron doctor --fix`：从当前环境变量回填启用 provider/channel 的缺失关键字段（最小集合）。
   - 已包含部分迁移修复：provider alias key（如 `openai-codex`）与常见 snake_case 字段（如 `api_key`、`bot_token`）。
   - 支持 `openheron doctor --fix-dry-run` 先看修复计划与摘要，不落盘。
   - 代码：`openheron/cli.py:468`
   - 与 OpenClaw 差距：尚未覆盖配置迁移类自动修复（OpenClaw 有更完整的 migration/fix 流程）。
   - 参考：`../openclaw/src/commands/doctor-config-flow.ts:941`

### P1（中优先）

3. **非交互安装风险确认策略已最小对齐**
   - OpenClaw 非交互 onboarding 要求 `--accept-risk` 显式确认。
   - 参考：`../openclaw/src/commands/onboard.ts:39`
   - Openheron 已要求 `openheron install --non-interactive --accept-risk` 才允许继续执行。

4. **引导可插拔扩展机制未对齐**
   - OpenClaw 对 channel onboarding 有插件化 adapter。
   - 参考：`../openclaw/src/plugin-sdk/index.ts:376`
   - Openheron 当前是内置分支逻辑，扩展性弱于 adapter 机制。
   - 当前进展：install/doctor 的 provider/channel 修复规则已基本表驱动并单源化，但尚未上升到独立 plugin adapter 接口层。

## 建议实施顺序（下一阶段）

1. 继续扩展 **P0-2 doctor --fix**（从最小回填扩展到配置迁移级修复）。
2. 继续扩展 **P1-4 onboarding adapter 化**（抽象 channel 凭证 schema 与输入器）。
