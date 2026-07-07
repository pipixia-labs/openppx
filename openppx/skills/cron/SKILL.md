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

1. `message` describes the task that the system must perform at the scheduled time. The agent must fill it with an action instruction, not only a raw text value or title. Use shapes such as `send the yy message` or `run the yy task`.
2. For reminder-like jobs, explicitly say whether output must be exact text.
3. Avoid ambiguous `message` values like only a number or a noun phrase.

Bad examples (ambiguous):
```
cron(action="add", message="139121235123", at="<ISO>")
```

Good examples (explicit action):
```
cron(action="add", message="Open Word.", at="<ISO>")
cron(action="add", message="Output exactly: Time is up. Do not add anything else.", at="<ISO>")
cron(action="add", message="Output exactly: 139121235123. Do not explain or rewrite it.", at="<ISO>")
cron(action="add", message="Check project status and output three short summary bullets.", every_seconds=600)
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

For requests like "in 20 minutes", "after 2 hours", "run in 2 minutes", or "run after 1 hour",
the reference point MUST be the current conversation message time (request receive time),
NOT gateway startup time.

Workflow:

1. Read request receive time as `now`.
2. Compute `at = now + delta`.
3. Call `cron(action="add", message="...", at="<ISO from step 2>")`.

Example:

- User says: "In 2 minutes, send me a message containing 139121235123."
- Correct cron call shape:
```
cron(
  action="add",
  message="You are a reminder assistant. Output exactly: 139121235123. Do not explain or rewrite it.",
  at="<ISO datetime computed from current request time>"
)
```

## Basic Management

```
cron(action="list")
cron(action="remove", job_id="abc123")
```
