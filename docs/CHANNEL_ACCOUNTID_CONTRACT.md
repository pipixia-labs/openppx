# Channel AccountId 契约（多智能体 v1）

本文定义各 channel 在多智能体路由中的 `accountId` 输入契约，目标是避免“同 channel 多账号”场景下的路由歧义。

## 1. 路由侧统一语义

- 路由键：`channel + accountId + peer`
- 优先级：`peer > account > channel > default`
- Router 读取顺序：
  - `metadata.account_id`
  - `metadata.accountId`
  - 以上都没有时，按空字符串处理（无法命中 account 级 binding）

结论：只要要用到 account 级路由，channel 入站消息必须稳定提供 `accountId`。

## 2. 当前实现约定（按 channel）

- `whatsapp`
  - 已实现自动透传：读取 webhook 中的 `accountId/account_id`，写入消息 metadata。
  - 建议：多账号（personal/biz）必须在上游桥接层保证字段始终存在。
- `telegram` / `discord` / `feishu`
  - 当前主要保证 `peer` 语义（`peer_kind/peer_id`），默认不注入 `accountId`。
  - 建议：如果有多 bot token 或多租户入口，需在接入层补充 `metadata.accountId`。
- 其他 channel（`slack/dingtalk/qq/mochat/email/local`）
  - 统一建议：单账号场景可不填 `accountId`；多账号场景必须补齐 `metadata.accountId`。

## 3. 自定义 channel 的最小输入契约

入站消息 metadata 至少提供：

- `accountId`（多账号场景必填，推荐小写稳定标识，如 `personal`/`business`）
- `peer.kind` + `peer.id`（或等价的 `peer_kind/peer_id`）

推荐同时写入兼容键，降低历史代码耦合风险：

- `accountId` 与 `account_id` 同时写入
- `peer` 与 `peer_kind/peer_id` 同时写入

## 4. 验证与排障

```bash
openheron routes lint --json
openheron doctor --json
openheron routes stats --json --window-hours 24
```

重点检查：

- `routes lint --json` 的 `summary.conflicts` 为空
- `doctor --json` 的 `multiAgent.summary.byChannel` 与预期 channel 一致
- `routes stats --json` 的 `stats.byMatchedBy` 中 account/peer 命中比例符合预期
