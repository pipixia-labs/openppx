---
name: cron
description: Schedule reminders and recurring tasks; for cron add, message must be an executable action instruction with expected output constraints (exact text for reminders), never raw text/title/number, and relative time must be computed from current request time (not gateway startup time).
---
  
# Cron

Use the `cron` tool to schedule reminders or recurring tasks.

## Core Rule

`message` MUST describe what the system should do at trigger time.
Treat it as executable task instruction for the agent, not as a title or raw note.

## Message Field (Most Important)

When building `cron(action="add", ...)`:

1. `message` 描述的是一个任务，也就是在 xx 时间，系统需要做的事情。所以在message字段，智能体需要填充一个任务，这个任务一定是一个动作来描述的，而不是仅仅是一个文本。
系统需要的格式为：`发送 yy 消息`或者 `执行 yy 任务`。
2. For reminder-like jobs, explicitly say whether output must be exact text.
3. Avoid ambiguous `message` values like only a number or a noun phrase.

Bad examples (ambiguous):
```
cron(action="add", message="139121235123", at="<ISO>")
```

Good examples (explicit action):
```
cron(action="add", message="打开word", at="<ISO>")
cron(action="add", message="只输出“时间到了"，不要添加其他内容。", at="<ISO>")
cron(action="add", message="只输出"139121235123"。不要解释，不要改写。", at="<ISO>")
cron(action="add", message="检查项目状态并输出三条摘要，每条不超过20字。", every_seconds=600)
```

## Scheduling Modes

1. Interval: `every_seconds`
2. Cron expression: `cron_expr` (+ optional `tz`)
3. One-time: `at` (absolute ISO datetime; usually auto-delete after execution)

## Time Expression Mapping

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| at a specific time | at: ISO datetime string |

## Relative Time Rule (Required)

For requests like "in 20 minutes", "after 2 hours", or "过2分钟/1小时后执行",
the reference point MUST be the current conversation message time (request receive time),
NOT gateway startup time.

Workflow:

1. Read request receive time as `now`.
2. Compute `at = now + delta`.
3. Call `cron(action="add", message="...", at="<ISO from step 2>")`.

Example:

- User says: "2分钟之后，发一个“139121235123”的消息给我"
- Correct cron call shape:
```
cron(
  action="add",
  message="你是提醒助手。请只输出：139121235123。不要解释，不要改写。",
  at="<ISO datetime computed from current request time>"
)
```

## Basic Management

```
cron(action="list")
cron(action="remove", job_id="abc123")
```
