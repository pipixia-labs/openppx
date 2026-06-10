"""Narrow subprocess runner for declarative skill Node.js APIs."""

from __future__ import annotations

import json
import os
import shutil


_BRIDGE_JS = r"""
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");
const { createRequire } = require("module");

const templateRe = /\{([A-Za-z_][A-Za-z0-9_.-]*)\}/g;
const fullTemplateRe = /^\{([A-Za-z_][A-Za-z0-9_.-]*)\}$/;

function emit(payload) {
  process.stdout.write(JSON.stringify(payload) + "\n");
}

function loadJsonEnv(name, fallback) {
  const raw = String(process.env[name] || "").trim();
  if (!raw) {
    return fallback;
  }
  return JSON.parse(raw);
}

function lookupArg(args, rawPath) {
  let lookupPath = rawPath;
  if (lookupPath === "args") {
    return args;
  }
  if (lookupPath.startsWith("args.")) {
    lookupPath = lookupPath.slice(5);
  }
  let current = args;
  for (const part of lookupPath.split(".")) {
    if (current && typeof current === "object" && Object.prototype.hasOwnProperty.call(current, part)) {
      current = current[part];
      continue;
    }
    throw new Error(`missing argument for template placeholder {${rawPath}}`);
  }
  return current;
}

function renderString(template, args) {
  const full = fullTemplateRe.exec(template);
  if (full) {
    return lookupArg(args, full[1]);
  }
  return template.replace(templateRe, (_match, key) => String(lookupArg(args, key)));
}

function renderValue(value, args) {
  if (typeof value === "string") {
    return renderString(value, args);
  }
  if (Array.isArray(value)) {
    return value.map((item) => renderValue(item, args));
  }
  if (value && typeof value === "object") {
    const output = {};
    for (const [key, item] of Object.entries(value)) {
      output[String(key)] = renderValue(item, args);
    }
    return output;
  }
  return value;
}

function resolveFunction(moduleExports, functionPath) {
  if (!functionPath || functionPath === "default") {
    return moduleExports.default || moduleExports;
  }
  let current = moduleExports;
  for (const part of functionPath.split(".")) {
    if (current && typeof current === "object" && Object.prototype.hasOwnProperty.call(current, part)) {
      current = current[part];
      continue;
    }
    throw new Error(`Node API function ${functionPath} was not found`);
  }
  return current;
}

async function loadModule(modulePath) {
  const skillRoot = process.cwd();
  const resolved = path.resolve(skillRoot, modulePath);
  const relative = path.relative(skillRoot, resolved);
  if (path.isAbsolute(modulePath) || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error("Node API recipe module must resolve under the skill root");
  }
  if (!fs.existsSync(resolved)) {
    throw new Error(`Node API module not found: ${modulePath}`);
  }
  if (resolved.endsWith(".cjs")) {
    return createRequire(pathToFileURL(resolved))(resolved);
  }
  return import(pathToFileURL(resolved).href);
}

function callArgs(recipe, argsPayload) {
  if (Array.isArray(recipe.args)) {
    return recipe.args.map((item) => renderValue(item, argsPayload));
  }
  if (recipe.kwargs && typeof recipe.kwargs === "object" && !Array.isArray(recipe.kwargs)) {
    return [renderValue(recipe.kwargs, argsPayload)];
  }
  if (recipe.spread_args === true && Array.isArray(argsPayload)) {
    return argsPayload;
  }
  return [argsPayload || {}];
}

(async () => {
  try {
    const recipe = loadJsonEnv("OPENPPX_NODE_API_RECIPE_JSON", null);
    if (!recipe || typeof recipe !== "object" || Array.isArray(recipe)) {
      throw new Error("OPENPPX_NODE_API_RECIPE_JSON must be a JSON object");
    }
    const argsPayload = loadJsonEnv("OPENPPX_SKILL_ARGS_JSON", {});
    const modulePath = String(recipe.module || recipe.file || "").trim();
    if (!modulePath) {
      throw new Error("Node API recipe must define module or file");
    }
    const functionPath = String(recipe.function || "default").trim() || "default";
    const moduleExports = await loadModule(modulePath);
    const target = resolveFunction(moduleExports, functionPath);
    if (typeof target !== "function") {
      throw new Error(`Node API target ${functionPath} is not callable`);
    }
    const result = await target(...callArgs(recipe, argsPayload));
    emit({ ok: true, result });
    if (recipe.fail_on_ok_false === true && result && typeof result === "object" && result.ok === false) {
      process.exitCode = 1;
    }
  } catch (error) {
    emit({
      ok: false,
      error: error && error.message ? error.message : String(error),
      error_type: error && error.name ? error.name : "Error",
    });
    process.exitCode = 1;
  }
})();
"""


def main() -> int:
    """Replace this Python runner with a Node.js bridge process."""
    node_bin = os.getenv("OPENPPX_NODE_BIN", "").strip() or shutil.which("node")
    if not node_bin:
        _emit_response(ok=False, error="Node.js executable was not found on PATH.", error_type="NodeNotFound")
        return 1
    os.execvp(node_bin, [node_bin, "-e", _BRIDGE_JS])
    return 1


def _emit_response(*, ok: bool, error: str = "", error_type: str = "") -> None:
    payload = {"ok": ok, "error": error, "error_type": error_type}
    print(json.dumps(payload, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
