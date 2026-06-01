# openppx ADK eval baseline

This directory contains the minimal ADK eval baseline for openppx.

`adk eval` loads an agent directory by importing its `__init__.py` as module
`agent`, then reads `agent.root_agent`. It also infers `app_name` from the
agent directory basename. For that reason, the eval entry directory is named
`openppx` and re-exports `openppx.app.agent`.

Run from the `openppx_root` repository root:

```bash
adk eval tests/eval/openppx tests/eval/evalsets/openppx_smoke.evalset.json --config_file_path tests/eval/eval_config.json
```

If the local environment was installed without ADK eval extras, install the
optional dependency first:

```bash
pip install ".[eval]"
```

This command calls the configured model and therefore requires normal model
credentials. Deterministic pytest coverage only validates the entrypoint,
schema, and app-name wiring; it does not run live LLM inference.
