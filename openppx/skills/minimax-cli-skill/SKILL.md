---
name: minimax-cli-skill
description: Use when the user explicitly asks to use MiniMax, minimax-cli, or mmx, or when the task needs MiniMax terminal capabilities that Creative Claw does not already expose well through experts, especially music generation, speech synthesis, or MiniMax file upload. Prefer existing Creative Claw experts for generic image, video, search, and vision tasks unless MiniMax is explicitly required.
---

# MiniMax CLI Skill

Use `mmx` through `exec_command` to access MiniMax CLI capabilities from Creative Claw.

This skill is intentionally project-specific. It keeps MiniMax usage aligned with Creative Claw's fixed workspace, current expert boundaries, and non-interactive execution model.

## When To Use

- The user explicitly mentions `MiniMax`, `minimax-cli`, or `mmx`.
- The task needs MiniMax music generation.
- The task needs MiniMax speech synthesis or TTS.
- The task needs MiniMax file upload or `file_id`-based follow-up workflows.
- The user explicitly wants MiniMax for image generation, video generation, image understanding, web search, or text chat.

## When Not To Use

- Do not use this skill for generic image, video, search, or vision tasks when existing Creative Claw experts already cover the need and the user did not explicitly ask for MiniMax.
- Do not use this skill as the default orchestrator reasoning backend.
- Do not start interactive OAuth login unless the user explicitly asks for authentication help.

## Preflight Checks

Run these checks before real work:

```bash
command -v mmx
mmx auth status --output json --non-interactive 2>/dev/null
```

Rules:

- If `mmx` is missing, explain that MiniMax CLI is not installed.
- If auth check fails, explain the blocker clearly.
- Do not run `mmx auth login` interactively unless the user explicitly asks for it.
- Do not write secrets into project files.

## Creative Claw Workspace Rules

- `exec_command` runs inside the fixed Creative Claw workspace by default.
- Keep all outputs as workspace-relative paths.
- Prefer storing MiniMax outputs under `generated/minimax/`.
- Create the directory first when needed:

```bash
mkdir -p generated/minimax
```

- Do not save outputs to user-home directories such as `~/Music`, `~/Downloads`, or `/tmp` unless the user explicitly asks.
- When using local inputs, prefer files already tracked in the current session, such as `inbox/...` or `generated/...`.

## Command Rules

- Always add `--non-interactive`.
- For metadata or text-like responses, prefer `--output json`.
- For file-producing commands, prefer explicit `--out` or `--out-dir`.
- Use `--quiet` only when you intentionally want stdout to contain a simple saved path or identifier.
- `mmx` may print progress or model banners to stderr. If you need clean JSON on stdout, use `--output json` and redirect stderr when safe.
- Quote prompts and paths safely, especially when they contain spaces or punctuation.

## Recommended Command Patterns

### Authentication Status

```bash
mmx auth status --output json --non-interactive 2>/dev/null
```

Use this as the authority check for whether MiniMax CLI is ready.

### Text Chat

Use only when the user explicitly wants MiniMax text generation or MiniMax CLI behavior.

```bash
mmx text chat \
  --message "user:Explain this concept clearly." \
  --output json \
  --non-interactive \
  2>/dev/null
```

### Image Generation

Use for explicit MiniMax image generation requests.

```bash
mkdir -p generated/minimax
mmx image generate \
  --prompt "A cinematic poster of a cat astronaut." \
  --out-dir generated/minimax \
  --out-prefix cat-astronaut \
  --quiet \
  --non-interactive
```

Notes:

- With `--out-dir` and `--quiet`, stdout should be saved file paths.
- Prefer this pattern over URL-only output because Creative Claw works better with workspace files.

### Video Generation

For one-step generation, prefer blocking mode with explicit download path:

```bash
mkdir -p generated/minimax
mmx video generate \
  --prompt "Ocean waves crashing on black rocks." \
  --download generated/minimax/ocean-waves.mp4 \
  --quiet \
  --non-interactive
```

If the user explicitly wants an async workflow or a task id:

```bash
mmx video generate \
  --prompt "Ocean waves crashing on black rocks." \
  --async \
  --quiet \
  --non-interactive
```

Then poll and download explicitly:

```bash
mmx video task get --task-id <task-id> --output json --non-interactive 2>/dev/null
mmx video download --file-id <file-id> --out generated/minimax/ocean-waves.mp4 --quiet --non-interactive
```

### Image Understanding

Use for explicit MiniMax vision requests:

```bash
mmx vision describe \
  --image inbox/reference.png \
  --prompt "Describe the visual style and key objects." \
  --output json \
  --non-interactive \
  2>/dev/null
```

If the file is already uploaded, `--file-id` is also valid.

### Web Search

Use for explicit MiniMax search requests:

```bash
mmx search query \
  --q "MiniMax latest announcements" \
  --output json \
  --non-interactive \
  2>/dev/null
```

### Speech Synthesis

Use for TTS or voiceover output:

```bash
mkdir -p generated/minimax
mmx speech synthesize \
  --text "Hello from Creative Claw." \
  --out generated/minimax/voiceover.mp3 \
  --quiet \
  --non-interactive
```

Notes:

- With `--out` and `--quiet`, stdout should be the saved audio path.
- Prefer writing to a workspace file rather than streaming audio to stdout.

### Music Generation

Use for music creation, song drafts, or instrumentals.

Fast agent-friendly patterns:

```bash
mkdir -p generated/minimax
mmx music generate \
  --prompt "Warm indie folk song about sunrise." \
  --lyrics-optimizer \
  --out generated/minimax/sunrise-song.mp3 \
  --quiet \
  --non-interactive
```

```bash
mkdir -p generated/minimax
mmx music generate \
  --prompt "Cinematic orchestral background music." \
  --instrumental \
  --out generated/minimax/cinematic-bgm.mp3 \
  --quiet \
  --non-interactive
```

Rules:

- `music generate` requires one of `--lyrics`, `--lyrics-file`, `--instrumental`, or `--lyrics-optimizer`.
- For long lyrics, save them in a workspace text file and pass `--lyrics-file`.
- Prefer `--lyrics-optimizer` or `--instrumental` when the user did not provide finished lyrics.

### File Upload

Use when MiniMax needs a hosted `file_id`:

```bash
mmx file upload \
  --file inbox/reference.png \
  --purpose vision \
  --output json \
  --non-interactive \
  2>/dev/null
```

Typical follow-up:

1. Upload the file and capture `file_id`.
2. Call MiniMax commands that accept `--file-id`.

## Selection Guidance Inside Creative Claw

- Prefer existing experts for normal Creative Claw image, video, search, and image-understanding tasks.
- Prefer this skill when the user explicitly wants MiniMax or when the needed capability is currently MiniMax-specific.
- For music and speech, this skill is the preferred MiniMax entry point.

## Failure Handling

- If `mmx` returns a usage error, check flags first before retrying.
- If auth fails, stop and explain the fix instead of retrying blindly.
- If a file-producing command succeeds, keep the returned workspace path for later attachment or downstream use.
- If the output is a task id or file id, surface that identifier clearly in your reasoning and next step.
