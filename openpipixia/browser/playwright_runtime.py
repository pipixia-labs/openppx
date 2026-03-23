"""Playwright-backed browser runtime for openpipixia (Iteration 2)."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
import re
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

from .runtime import (
    BrowserRuntimeError,
    resolve_browser_artifact_path,
    validate_browser_upload_paths,
    validate_browser_url,
)
from .schema import apply_status_metadata, make_profile_entry, make_runtime_capability

_SUPPORTED_PROFILES = {"openpipixia", "chrome"}
_SNAPSHOT_REF_PATTERN = re.compile(r"^e[1-9][0-9]*$")
_PROFILE_ACTIONS = [
    "status",
    "start",
    "stop",
    "profiles",
    "tabs",
    "open",
    "focus",
    "close",
    "navigate",
    "snapshot",
    "screenshot",
    "pdf",
    "console",
    "upload",
    "dialog",
    "act",
]
_CHROME_RELAY_SUPPORTED_ACTIONS = [
    "status",
    "tabs",
    "open",
    "focus",
    "close",
    "snapshot",
    "navigate",
    "act",
    "screenshot",
    "pdf",
    "console",
    "upload",
    "dialog",
]
_CHROME_RELAY_ACT_KINDS = {
    "open",
    "click",
    "type",
    "wait",
    "press",
    "hover",
    "select",
    "drag",
    "fill",
    "resize",
    "close",
    "evaluate",
}


def build_snapshot_refs(
    entries: list[dict[str, Any]],
    *,
    previous_ref_selectors: dict[str, str] | None = None,
    max_refs: int = 30,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Build snapshot refs payload and selector mapping from raw page entries.

    Reuses previous ``eN`` refs when selectors stay the same, so refs remain
    stable across snapshots.
    """

    refs_payload: dict[str, dict[str, str]] = {}
    ref_selectors: dict[str, str] = {}
    previous_selector_to_ref: dict[str, str] = {}
    if previous_ref_selectors:
        for ref, selector in previous_ref_selectors.items():
            if _SNAPSHOT_REF_PATTERN.fullmatch(str(ref)) and str(selector).strip():
                previous_selector_to_ref[str(selector)] = str(ref)
    used_refs: set[str] = set()
    next_counter = 1

    def _next_ref() -> str:
        nonlocal next_counter
        while f"e{next_counter}" in used_refs:
            next_counter += 1
        ref_value = f"e{next_counter}"
        next_counter += 1
        return ref_value

    for raw in entries:
        if len(refs_payload) >= max_refs:
            break
        selector = str(raw.get("selector") or "").strip()
        if not selector:
            continue
        ref = previous_selector_to_ref.get(selector)
        if not ref or ref in used_refs:
            ref = _next_ref()
        used_refs.add(ref)
        refs_payload[ref] = {
            "role": str(raw.get("role") or "").strip() or "element",
            "name": str(raw.get("name") or "").strip(),
        }
        ref_selectors[ref] = selector
    return refs_payload, ref_selectors


@dataclass(slots=True)
class _PlaywrightTab:
    target_id: str
    page: Any


class PlaywrightBrowserRuntime:
    """Minimal Playwright runtime with optional CDP attach mode.

    Environment flags:
    - ``OPENPIPIXIA_BROWSER_CDP_URL``: if set, use connect-over-CDP mode.
    - ``OPENPIPIXIA_BROWSER_HEADLESS``: used for local launch mode (default: true).
    """

    def __init__(self) -> None:
        self._pw_context_manager: Any | None = None
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._owns_browser: bool = False
        self._owns_context: bool = False
        self._tabs: dict[str, _PlaywrightTab] = {}
        self._last_target_id: str | None = None
        self._mode: str = "idle"
        self._cdp_url: str | None = None
        self._chrome_transport: str | None = None
        self._active_profile: str | None = None
        self._console_messages_by_tab: dict[str, list[dict[str, str]]] = {}
        self._bound_console_page_ids: set[int] = set()
        self._snapshot_ref_selectors_by_tab: dict[str, dict[str, str]] = {}

    def status(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        if resolved_profile == "chrome":
            chrome_available = self._chrome_profile_enabled()
            transport = self._chrome_transport or self._resolve_chrome_transport()
            if chrome_available and transport == "relay":
                relay = self._relay_request("/status")
                base = {
                    "enabled": True,
                    "running": bool(relay.get("running", False)),
                    "profile": "chrome",
                    "tabCount": int(relay.get("tabCount", 0)),
                    "lastTargetId": relay.get("lastTargetId"),
                    "backend": "extension-relay",
                    "mode": "relay",
                    "available": True,
                    "transport": "relay",
                    "capability": make_runtime_capability(
                        backend="extension-relay",
                        driver="extension-relay",
                        mode="relay",
                        attach_mode="cdp-required",
                        supported_actions=_CHROME_RELAY_SUPPORTED_ACTIONS,
                    ),
                }
                for key, value in relay.items():
                    if key not in base:
                        base[key] = value
                return apply_status_metadata(base, browser_owned=False, context_owned=False)
            return apply_status_metadata(
                {
                    "enabled": True,
                    "running": bool(chrome_available and self._browser is not None and self._active_profile == "chrome"),
                    "profile": "chrome",
                    "tabCount": len(self._tabs) if chrome_available and self._active_profile == "chrome" else 0,
                    "lastTargetId": self._last_target_id if chrome_available and self._active_profile == "chrome" else None,
                    "backend": "extension-relay",
                    "mode": self._mode if chrome_available and self._active_profile == "chrome" else "unsupported",
                    "available": chrome_available,
                    "transport": transport if chrome_available else "unsupported",
                    "capability": make_runtime_capability(
                        backend="extension-relay",
                        driver="extension-relay",
                        mode=self._mode if chrome_available and self._active_profile == "chrome" else "unsupported",
                        attach_mode="cdp-required",
                        supported_actions=_PROFILE_ACTIONS,
                    ),
                },
                browser_owned=bool(self._owns_browser and self._active_profile == "chrome"),
                context_owned=bool(self._owns_context and self._active_profile == "chrome"),
            )
        return apply_status_metadata(
            {
                "enabled": True,
                "running": self._browser is not None,
                "profile": resolved_profile,
                "tabCount": len(self._tabs),
                "lastTargetId": self._last_target_id,
                "backend": "playwright",
                "mode": self._mode,
                "capability": make_runtime_capability(
                    backend="playwright",
                    driver="playwright",
                    mode=self._mode,
                    attach_mode="launch-or-cdp",
                    supported_actions=_PROFILE_ACTIONS,
                ),
            },
            browser_owned=self._owns_browser,
            context_owned=self._owns_context,
        )

    def start(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        if self._browser is not None:
            if self._active_profile and self._active_profile != resolved_profile:
                raise BrowserRuntimeError(
                    f"profile mismatch: active profile is {self._active_profile}; stop first to switch",
                    status=409,
                )
            return self.status(profile=resolved_profile)
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            self._active_profile = "chrome"
            self._chrome_transport = "relay"
            self._mode = "relay"
            return self.status(profile=resolved_profile)
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - dependency-gated
            raise BrowserRuntimeError(
                f"playwright is not available: {exc}. Install it and run `playwright install chromium`.",
                status=503,
            ) from exc

        self._pw_context_manager = sync_playwright()
        self._pw = self._pw_context_manager.start()
        chromium = self._pw.chromium
        cdp_url = self._resolve_cdp_url_for_profile(resolved_profile)
        self._cdp_url = cdp_url or None
        self._chrome_transport = self._resolve_chrome_transport() if resolved_profile == "chrome" else None
        if cdp_url:
            self._browser = chromium.connect_over_cdp(cdp_url)
            contexts = list(self._browser.contexts)
            self._context = contexts[0] if contexts else self._browser.new_context()
            self._mode = "cdp"
            # In CDP attach mode, browser/context may be owned by an external process.
            self._owns_browser = False
            self._owns_context = not bool(contexts)
        else:
            headless = os.getenv("OPENPIPIXIA_BROWSER_HEADLESS", "1").strip().lower() not in {
                "0",
                "false",
                "off",
                "no",
            }
            self._browser = chromium.launch(headless=headless)
            self._context = self._browser.new_context()
            self._mode = "launch"
            self._owns_browser = True
            self._owns_context = True
        self._active_profile = resolved_profile
        self._sync_existing_tabs()
        return self.status(profile=resolved_profile)

    def stop(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            if self._active_profile == "chrome":
                self._active_profile = None
            self._mode = "idle"
            self._chrome_transport = None
            return self.status(profile=resolved_profile)
        if self._context is not None and self._owns_context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser is not None and self._owns_browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw_context_manager is not None:
            try:
                self._pw_context_manager.stop()
            except Exception:
                pass
        self._pw_context_manager = None
        self._pw = None
        self._browser = None
        self._context = None
        self._owns_browser = False
        self._owns_context = False
        self._chrome_transport = None
        self._tabs = {}
        self._last_target_id = None
        self._active_profile = None
        self._console_messages_by_tab = {}
        self._bound_console_page_ids = set()
        self._snapshot_ref_selectors_by_tab = {}
        self._mode = "idle"
        return self.status(profile=resolved_profile)

    def profiles(self) -> dict[str, Any]:
        chrome_available = self._chrome_profile_enabled()
        chrome_transport = self._resolve_chrome_transport()
        return {
            "profiles": [
                make_profile_entry(
                    name="openpipixia",
                    driver="playwright",
                    description="Playwright runtime profile",
                    available=True,
                    attach_mode="launch-or-cdp",
                    ownership_model={
                        "browser": "owned-in-launch, borrowed-in-cdp",
                        "context": "owned-in-launch, mixed-in-cdp",
                    },
                    capability=make_runtime_capability(
                        backend="playwright",
                        driver="playwright",
                        mode=self._mode if self._browser is not None else "idle",
                        attach_mode="launch-or-cdp",
                        supported_actions=_PROFILE_ACTIONS,
                    ),
                ),
                make_profile_entry(
                    name="chrome",
                    driver="extension-relay",
                    description="Chrome extension relay profile (not implemented yet)",
                    available=chrome_available,
                    attach_mode="cdp-required",
                    requires={
                        "OPENPIPIXIA_BROWSER_CHROME_CDP_URL": True,
                        "OPENPIPIXIA_BROWSER_CHROME_RELAY_URL": True,
                    },
                    ownership_model={
                        "browser": "borrowed",
                        "context": "borrowed-or-owned-if-created",
                    },
                    capability=make_runtime_capability(
                        backend="extension-relay",
                        driver="extension-relay",
                        mode=(
                            self._mode
                            if chrome_available and self._active_profile == "chrome"
                            else chrome_transport
                            if chrome_available
                            else "unsupported"
                        ),
                        attach_mode="cdp-required",
                        supported_actions=(
                            _CHROME_RELAY_SUPPORTED_ACTIONS
                            if chrome_transport == "relay"
                            else _PROFILE_ACTIONS
                        ),
                    ),
                ),
            ]
        }

    def tabs(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay = self._relay_request("/tabs")
            tabs_payload = relay.get("tabs")
            return {
                "running": bool(relay.get("running", True)),
                "profile": "chrome",
                "tabs": tabs_payload if isinstance(tabs_payload, list) else [],
                "backend": "extension-relay",
                "mode": "relay",
            }
        self._ensure_profile_active(resolved_profile)
        self._ensure_running()
        self._sync_existing_tabs()
        return {
            "running": True,
            "profile": resolved_profile,
            "tabs": [self._tab_payload(item) for item in self._tabs.values()],
            "backend": "playwright",
            "mode": self._mode,
        }

    def open_tab(self, *, url: str, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "open")
        validate_browser_url(url)
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay = self._relay_request(
                "/tabs/open",
                method="POST",
                body={"url": url},
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            return payload
        self._ensure_profile_active(resolved_profile)
        self._ensure_running()
        page = self._context.new_page()  # type: ignore[union-attr]
        page.goto(url, wait_until="domcontentloaded")
        target_id = self._register_page(page)
        tab = self._tabs[target_id]
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.page.url,
            "title": tab.page.title(),
            "backend": "playwright",
        }

    def focus_tab(self, *, target_id: str | None = None, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "focus")
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay = self._relay_request(
                "/tabs/focus",
                method="POST",
                body={"targetId": (target_id or "").strip() or None},
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            return payload
        self._ensure_profile_active(resolved_profile)
        tab = self._resolve_tab(target_id)
        tab.page.bring_to_front()
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.page.url,
            "focused": True,
            "backend": "playwright",
        }

    def close_tab(self, *, target_id: str | None = None, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "close")
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay = self._relay_request(
                "/tabs/close",
                method="POST",
                body={"targetId": (target_id or "").strip() or None},
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            return payload
        self._ensure_profile_active(resolved_profile)
        tab = self._resolve_tab(target_id)
        tab.page.close()
        self._tabs.pop(tab.target_id, None)
        self._snapshot_ref_selectors_by_tab.pop(tab.target_id, None)
        self._last_target_id = next(reversed(self._tabs), None) if self._tabs else None
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "closed": True,
            "backend": "playwright",
        }

    def snapshot(
        self,
        *,
        target_id: str | None = None,
        snapshot_format: str = "ai",
        profile: str | None = None,
    ) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            fmt = snapshot_format.strip().lower() or "ai"
            relay = self._relay_request(
                "/snapshot",
                query={"targetId": (target_id or "").strip() or None, "format": fmt},
            )
            relay_target_id = str(relay.get("targetId", "")).strip()
            if relay_target_id:
                self._snapshot_ref_selectors_by_tab[relay_target_id] = self._extract_ref_selectors_from_snapshot(relay)
            payload = {
                "ok": bool(relay.get("ok", True)),
                "format": fmt,
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            return payload
        self._ensure_profile_active(resolved_profile)
        tab = self._resolve_tab(target_id)
        fmt = snapshot_format.strip().lower()
        if fmt not in {"ai", "aria"}:
            raise BrowserRuntimeError("snapshot_format must be 'ai' or 'aria'")

        if fmt == "aria":
            raw = tab.page.accessibility.snapshot() or {}
            nodes: list[dict[str, Any]] = []
            role = raw.get("role") if isinstance(raw, dict) else "document"
            name = raw.get("name") if isinstance(raw, dict) else ""
            nodes.append({"ref": "ax1", "role": role or "document", "name": name or ""})
            return {
                "ok": True,
                "format": "aria",
                "profile": resolved_profile,
                "targetId": tab.target_id,
                "url": tab.page.url,
                "nodes": nodes,
                "backend": "playwright",
            }

        title = tab.page.title()
        raw_refs: list[dict[str, Any]] = []
        try:
            raw_refs = tab.page.evaluate(
                """() => {
  const interactive = Array.from(
    document.querySelectorAll('a,button,input,textarea,select,[role="button"],[onclick]')
  ).slice(0, 40);
  return interactive.map((el) => {
    const role = (el.getAttribute('role') || el.tagName || '').toLowerCase();
    const name = (el.getAttribute('aria-label') || el.textContent || el.getAttribute('name') || '').trim();
    let selector = '';
    if (el.id) selector = `#${CSS.escape(el.id)}`;
    else if (el.getAttribute('data-testid')) selector = `[data-testid="${CSS.escape(el.getAttribute('data-testid'))}"]`;
    else if (el.getAttribute('name')) selector = `${el.tagName.toLowerCase()}[name="${CSS.escape(el.getAttribute('name'))}"]`;
    else {
      const tag = el.tagName.toLowerCase();
      const siblings = Array.from(el.parentElement ? el.parentElement.children : []);
      const sameTag = siblings.filter((x) => x.tagName === el.tagName);
      const idx = sameTag.indexOf(el) + 1;
      selector = `${tag}:nth-of-type(${idx > 0 ? idx : 1})`;
    }
    return { role, name, selector };
  });
}"""
            )
        except Exception:
            raw_refs = []
        refs_payload, ref_selectors = build_snapshot_refs(
            raw_refs if isinstance(raw_refs, list) else [],
            previous_ref_selectors=self._snapshot_ref_selectors_by_tab.get(tab.target_id),
        )
        self._snapshot_ref_selectors_by_tab[tab.target_id] = ref_selectors
        return {
            "ok": True,
            "format": "ai",
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.page.url,
            "snapshot": (
                f"URL: {tab.page.url}\n"
                f"Title: {title}\n"
                "Use CSS selectors as refs for act commands in this iteration."
            ),
            "refs": refs_payload,
            "backend": "playwright",
        }

    def navigate(
        self,
        *,
        url: str,
        target_id: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "navigate")
        validate_browser_url(url)
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay = self._relay_request(
                "/navigate",
                method="POST",
                body={"targetId": (target_id or "").strip() or None, "url": url},
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            return payload
        self._ensure_profile_active(resolved_profile)
        tab = self._resolve_tab(target_id)
        validate_browser_url(url)
        tab.page.goto(url, wait_until="domcontentloaded")
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.page.url,
            "title": tab.page.title(),
            "backend": "playwright",
        }

    def act(
        self,
        *,
        request: dict[str, Any],
        target_id: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "act")
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay_request = self._normalize_relay_act_request(
                request=request,
                target_id=(target_id or "").strip(),
            )
            relay = self._relay_request(
                "/act",
                method="POST",
                body={
                    "targetId": (target_id or "").strip() or None,
                    "request": relay_request,
                },
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            return payload
        self._ensure_profile_active(resolved_profile)
        tab = self._resolve_tab(target_id)
        kind = str(request.get("kind", "")).strip().lower()
        if not kind:
            raise BrowserRuntimeError("request.kind is required")
        if kind not in {"click", "type", "press", "wait", "close", "hover", "select", "evaluate", "fill", "resize", "drag"}:
            raise BrowserRuntimeError(f"unsupported act kind: {kind}")

        if kind == "click":
            selector = self._selector_from_request(request, tab.target_id)
            tab.page.locator(selector).first.click()
        elif kind == "type":
            selector = self._selector_from_request(request, tab.target_id)
            text = request.get("text")
            if not isinstance(text, str):
                raise BrowserRuntimeError("request.text is required for type")
            tab.page.locator(selector).first.fill(text)
        elif kind == "press":
            key = str(request.get("key", "")).strip()
            if not key:
                raise BrowserRuntimeError("request.key is required for press")
            tab.page.keyboard.press(key)
        elif kind == "hover":
            selector = self._selector_from_request(request, tab.target_id)
            tab.page.locator(selector).first.hover()
        elif kind == "select":
            selector = self._selector_from_request(request, tab.target_id)
            values = request.get("values")
            if not isinstance(values, list) or not values:
                raise BrowserRuntimeError("request.values is required for select")
            normalized = [str(item) for item in values if str(item).strip()]
            if not normalized:
                raise BrowserRuntimeError("request.values is required for select")
            tab.page.locator(selector).first.select_option(normalized)
        elif kind == "evaluate":
            fn = str(request.get("fn", "")).strip()
            if not fn:
                raise BrowserRuntimeError("request.fn is required for evaluate")
            tab.page.evaluate(fn)
        elif kind == "fill":
            fields = request.get("fields")
            if not isinstance(fields, list) or not fields:
                raise BrowserRuntimeError("request.fields is required for fill")
            for raw in fields:
                if not isinstance(raw, dict):
                    continue
                text = raw.get("text")
                value = raw.get("value")
                fill_text = text if isinstance(text, str) else value if isinstance(value, str) else None
                if fill_text is None:
                    continue
                selector = self._selector_from_request(raw, tab.target_id)
                tab.page.locator(selector).first.fill(fill_text)
        elif kind == "resize":
            width = request.get("width")
            height = request.get("height")
            if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
                raise BrowserRuntimeError("request.width and request.height are required for resize")
            tab.page.set_viewport_size({"width": int(width), "height": int(height)})
        elif kind == "drag":
            start_selector = self._selector_from_drag_request(request, tab.target_id, "start")
            end_selector = self._selector_from_drag_request(request, tab.target_id, "end")
            tab.page.drag_and_drop(start_selector, end_selector)
        elif kind == "wait":
            timeout_ms = request.get("timeMs")
            wait_ms = int(timeout_ms) if isinstance(timeout_ms, (int, float)) else 500
            tab.page.wait_for_timeout(wait_ms)
        elif kind == "close":
            tab.page.close()
            self._tabs.pop(tab.target_id, None)
            self._snapshot_ref_selectors_by_tab.pop(tab.target_id, None)
            self._last_target_id = next(reversed(self._tabs), None) if self._tabs else None
            return {
                "ok": True,
                "profile": resolved_profile,
                "targetId": tab.target_id,
                "closed": True,
                "backend": "playwright",
            }

        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.page.url,
            "kind": kind,
            "backend": "playwright",
        }

    def screenshot(
        self,
        *,
        target_id: str | None = None,
        profile: str | None = None,
        image_type: str = "png",
        out_path: str | None = None,
    ) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "screenshot")
        fmt = image_type.strip().lower()
        if fmt not in {"png", "jpeg"}:
            raise BrowserRuntimeError("image_type must be 'png' or 'jpeg'")
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay = self._relay_request(
                "/screenshot",
                method="POST",
                body={"targetId": (target_id or "").strip() or None, "type": fmt},
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            image_base64 = payload.get("imageBase64")
            saved_path: str | None = None
            if out_path and out_path.strip() and isinstance(image_base64, str) and image_base64:
                default_name = f"{str(payload.get('targetId', '')).strip() or 'tab'}.{'png' if fmt == 'png' else 'jpg'}"
                saved_path = resolve_browser_artifact_path(out_path, default_filename=default_name)
                os.makedirs(os.path.dirname(saved_path) or ".", exist_ok=True)
                with open(saved_path, "wb") as f:
                    f.write(base64.b64decode(image_base64))
                payload["path"] = saved_path
            payload.setdefault("type", fmt)
            payload.setdefault("contentType", "image/png" if fmt == "png" else "image/jpeg")
            return payload
        self._ensure_profile_active(resolved_profile)
        tab = self._resolve_tab(target_id)
        screenshot_kwargs: dict[str, Any] = {"type": fmt if fmt == "png" else "jpeg"}
        saved_path: str | None = None
        if out_path and out_path.strip():
            default_name = f"{tab.target_id}.{'png' if fmt == 'png' else 'jpg'}"
            saved_path = resolve_browser_artifact_path(out_path, default_filename=default_name)
            dirpath = os.path.dirname(saved_path) or "."
            os.makedirs(dirpath, exist_ok=True)
            screenshot_kwargs["path"] = saved_path
        binary = tab.page.screenshot(**screenshot_kwargs)
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.page.url,
            "type": fmt,
            "contentType": "image/png" if fmt == "png" else "image/jpeg",
            "imageBase64": base64.b64encode(binary).decode("ascii"),
            "bytes": len(binary),
            "path": saved_path,
            "backend": "playwright",
        }

    def pdf_save(
        self,
        *,
        target_id: str | None = None,
        profile: str | None = None,
        out_path: str | None = None,
    ) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "pdf")
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay = self._relay_request(
                "/pdf",
                method="POST",
                body={"targetId": (target_id or "").strip() or None},
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            pdf_base64 = payload.get("pdfBase64")
            if isinstance(pdf_base64, str):
                payload.setdefault("bytes", len(base64.b64decode(pdf_base64)))
            saved_path: str | None = None
            if out_path and out_path.strip() and isinstance(pdf_base64, str) and pdf_base64:
                default_name = f"{str(payload.get('targetId', '')).strip() or 'tab'}.pdf"
                saved_path = resolve_browser_artifact_path(out_path, default_filename=default_name)
                os.makedirs(os.path.dirname(saved_path) or ".", exist_ok=True)
                with open(saved_path, "wb") as f:
                    f.write(base64.b64decode(pdf_base64))
                payload["path"] = saved_path
            payload.setdefault("contentType", "application/pdf")
            return payload
        self._ensure_profile_active(resolved_profile)
        tab = self._resolve_tab(target_id)
        target_path = resolve_browser_artifact_path(out_path, default_filename=f"{tab.target_id}.pdf")
        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        binary = tab.page.pdf(path=target_path)
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.page.url,
            "path": target_path,
            "bytes": len(binary),
            "contentType": "application/pdf",
            "backend": "playwright",
        }

    def console_messages(
        self,
        *,
        target_id: str | None = None,
        profile: str | None = None,
        level: str | None = None,
        out_path: str | None = None,
    ) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "console")
        normalized_level = (level or "").strip().lower() or None
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay = self._relay_request(
                "/console",
                query={
                    "targetId": (target_id or "").strip() or None,
                    "level": normalized_level,
                },
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            messages = payload.get("messages")
            if not isinstance(messages, list):
                messages = []
                payload["messages"] = messages
            saved_path: str | None = None
            if out_path and out_path.strip():
                default_name = f"{str(payload.get('targetId', '')).strip() or 'tab'}.console.json"
                saved_path = resolve_browser_artifact_path(out_path, default_filename=default_name)
                os.makedirs(os.path.dirname(saved_path) or ".", exist_ok=True)
                with open(saved_path, "w", encoding="utf-8") as f:
                    json.dump({"messages": messages}, f, ensure_ascii=False, indent=2)
                payload["path"] = saved_path
            return payload
        self._ensure_profile_active(resolved_profile)
        tab = self._resolve_tab(target_id)
        self._last_target_id = tab.target_id
        messages = list(self._console_messages_by_tab.get(tab.target_id, []))
        if normalized_level:
            messages = [entry for entry in messages if entry.get("level", "").lower() == normalized_level]
        saved_path: str | None = None
        if out_path and out_path.strip():
            saved_path = resolve_browser_artifact_path(
                out_path,
                default_filename=f"{tab.target_id}.console.json",
            )
            os.makedirs(os.path.dirname(saved_path) or ".", exist_ok=True)
            with open(saved_path, "w", encoding="utf-8") as f:
                json.dump({"messages": messages}, f, ensure_ascii=False, indent=2)
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "messages": messages,
            "path": saved_path,
            "backend": "playwright",
        }

    def upload(
        self,
        *,
        paths: list[str],
        target_id: str | None = None,
        profile: str | None = None,
        ref: str | None = None,
    ) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "upload")
        resolved = validate_browser_upload_paths(paths)
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            selector = (ref or "").strip()
            if selector:
                selector = self._selector_from_request({"ref": selector}, (target_id or "").strip())
            relay = self._relay_request_with_path_fallbacks(
                ["/upload", "/hooks/file-chooser"],
                method="POST",
                body={
                    "targetId": (target_id or "").strip() or None,
                    "paths": resolved,
                    "ref": selector or None,
                },
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            payload.setdefault("targetId", (target_id or "").strip() or None)
            payload.setdefault("count", len(resolved))
            return payload
        self._ensure_profile_active(resolved_profile)
        tab = self._resolve_tab(target_id)

        selector = (ref or "").strip()
        if selector:
            tab.page.locator(selector).first.set_input_files(resolved)
        else:
            tab.page.locator('input[type="file"]').first.set_input_files(resolved)
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "uploadedPaths": resolved,
            "ref": selector or None,
            "backend": "playwright",
        }

    def dialog(
        self,
        *,
        accept: bool,
        target_id: str | None = None,
        profile: str | None = None,
        prompt_text: str | None = None,
    ) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._ensure_chrome_relay_action_supported(resolved_profile, "dialog")
        if resolved_profile == "chrome" and self._resolve_chrome_transport() == "relay":
            relay = self._relay_request_with_path_fallbacks(
                ["/dialog", "/hooks/dialog"],
                method="POST",
                body={
                    "targetId": (target_id or "").strip() or None,
                    "accept": bool(accept),
                    "promptText": prompt_text or None,
                },
            )
            payload = {
                "ok": bool(relay.get("ok", True)),
                "profile": "chrome",
                "backend": "extension-relay",
                "mode": "relay",
            }
            payload.update(relay)
            payload.setdefault("accept", bool(accept))
            return payload
        tab = self._resolve_tab(target_id)

        def _handle_dialog(dialog: Any) -> None:
            if accept:
                dialog.accept(prompt_text=prompt_text)
            else:
                dialog.dismiss()

        tab.page.once("dialog", _handle_dialog)
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "accept": bool(accept),
            "promptText": prompt_text or None,
            "armed": True,
            "backend": "playwright",
        }

    def _resolve_profile(self, profile: str | None) -> str:
        resolved = (profile or "").strip().lower() or "openpipixia"
        if resolved not in _SUPPORTED_PROFILES:
            raise BrowserRuntimeError("unknown profile; supported profiles are openpipixia, chrome")
        return resolved

    def _ensure_profile_supported(self, profile: str) -> None:
        if profile == "chrome":
            if not self._chrome_profile_enabled():
                raise BrowserRuntimeError('profile "chrome" is not implemented yet', status=501)

    def _ensure_profile_active(self, profile: str) -> None:
        if self._active_profile is None:
            return
        if self._active_profile != profile:
            raise BrowserRuntimeError(
                f"profile mismatch: active profile is {self._active_profile}; stop first to switch",
                status=409,
            )

    def _chrome_profile_enabled(self) -> bool:
        return bool(self._chrome_cdp_url()) or bool(self._chrome_relay_url())

    def _resolve_cdp_url_for_profile(self, profile: str) -> str:
        if profile == "chrome":
            return self._chrome_cdp_url()
        return os.getenv("OPENPIPIXIA_BROWSER_CDP_URL", "").strip()

    def _chrome_cdp_url(self) -> str:
        return os.getenv("OPENPIPIXIA_BROWSER_CHROME_CDP_URL", "").strip()

    def _chrome_relay_url(self) -> str:
        return os.getenv("OPENPIPIXIA_BROWSER_CHROME_RELAY_URL", "").strip()

    def _resolve_chrome_relay_type_max_chars(self) -> int:
        raw = os.getenv("OPENPIPIXIA_BROWSER_CHROME_RELAY_TYPE_MAX_CHARS", "").strip()
        if not raw:
            return 5000
        try:
            value = int(raw)
        except ValueError:
            return 5000
        return min(max(value, 1), 200_000)

    def _resolve_chrome_relay_evaluate_max_chars(self) -> int:
        raw = os.getenv("OPENPIPIXIA_BROWSER_CHROME_RELAY_EVALUATE_MAX_CHARS", "").strip()
        if not raw:
            return 10_000
        try:
            value = int(raw)
        except ValueError:
            return 10_000
        return min(max(value, 1), 200_000)

    def _relay_evaluate_enabled(self) -> bool:
        return os.getenv("OPENPIPIXIA_BROWSER_CHROME_RELAY_EVALUATE_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _resolve_chrome_relay_max_body_bytes(self) -> int:
        raw = os.getenv("OPENPIPIXIA_BROWSER_CHROME_RELAY_MAX_BODY_BYTES", "").strip()
        if not raw:
            return 256 * 1024
        try:
            value = int(raw)
        except ValueError:
            return 256 * 1024
        return min(max(value, 256), 5 * 1024 * 1024)

    def _resolve_chrome_transport(self) -> str:
        if self._chrome_relay_url():
            return "relay"
        if self._chrome_cdp_url():
            return "cdp"
        return "unsupported"

    def _ensure_chrome_relay_action_supported(self, profile: str, action: str) -> None:
        if profile != "chrome":
            return
        if self._resolve_chrome_transport() != "relay":
            return
        if action in _CHROME_RELAY_SUPPORTED_ACTIONS:
            return
        raise BrowserRuntimeError(
            "chrome relay currently supports actions: status,tabs,snapshot,navigate,act",
            status=501,
        )

    def _relay_request(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        relay_base = self._chrome_relay_url()
        if not relay_base:
            raise BrowserRuntimeError("chrome relay is not configured", status=503)
        query_values = {
            key: value
            for key, value in (query or {}).items()
            if value is not None and str(value).strip()
        }
        query_str = urlencode(query_values)
        full_url = f"{relay_base.rstrip('/')}{path}"
        if query_str:
            full_url = f"{full_url}?{query_str}"
        headers = {"Accept": "application/json"}
        relay_token = os.getenv("OPENPIPIXIA_BROWSER_CHROME_RELAY_TOKEN", "").strip()
        if relay_token:
            headers["X-OpenPipixia-Browser-Relay-Token"] = relay_token
        body_bytes: bytes | None = None
        normalized_method = method.strip().upper() or "GET"
        if body is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            max_body_bytes = self._resolve_chrome_relay_max_body_bytes()
            if len(body_bytes) > max_body_bytes:
                raise BrowserRuntimeError(
                    f"chrome relay request body is too large ({len(body_bytes)} bytes, max {max_body_bytes})",
                    code="relay_body_too_large",
                )
        timeout_seconds = 10.0
        timeout_raw = os.getenv("OPENPIPIXIA_BROWSER_CHROME_RELAY_TIMEOUT_MS", "").strip()
        if timeout_raw:
            try:
                timeout_seconds = max(0.1, min(int(timeout_raw) / 1000.0, 120.0))
            except ValueError:
                timeout_seconds = 10.0
        try:
            with urlopen(
                Request(full_url, data=body_bytes, headers=headers, method=normalized_method),
                timeout=timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8", errors="replace").strip()
            if not raw:
                return {}
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise BrowserRuntimeError(
                    "chrome relay returned non-object JSON",
                    status=502,
                    code="relay_non_object_json",
                )
            return parsed
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = detail or str(exc.reason) or str(exc)
            status = exc.code if isinstance(exc.code, int) else 502
            try:
                parsed = json.loads(detail) if detail else {}
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                parsed_message = str(parsed.get("error") or parsed.get("message") or "").strip()
                if parsed_message:
                    message = parsed_message
                parsed_status = parsed.get("status")
                if isinstance(parsed_status, int):
                    status = parsed_status
            raise BrowserRuntimeError(
                f"chrome relay HTTP error: {message}",
                status=status,
                code="relay_http_error",
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise BrowserRuntimeError("chrome relay timeout", status=504, code="relay_timeout") from exc
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise BrowserRuntimeError("chrome relay timeout", status=504, code="relay_timeout") from exc
            if isinstance(reason, ConnectionRefusedError):
                raise BrowserRuntimeError(
                    "chrome relay connection refused",
                    status=503,
                    code="relay_connection_refused",
                ) from exc
            text = str(reason or "").strip().lower()
            if "timed out" in text:
                raise BrowserRuntimeError("chrome relay timeout", status=504, code="relay_timeout") from exc
            if "connection refused" in text:
                raise BrowserRuntimeError(
                    "chrome relay connection refused",
                    status=503,
                    code="relay_connection_refused",
                ) from exc
            if "name or service not known" in text or "nodename nor servname provided" in text:
                raise BrowserRuntimeError(
                    "chrome relay dns resolution failed",
                    status=503,
                    code="relay_dns_failed",
                ) from exc
            raise BrowserRuntimeError(
                f"chrome relay unavailable: {reason}",
                status=503,
                code="relay_unavailable",
            ) from exc
        except json.JSONDecodeError as exc:
            raise BrowserRuntimeError(
                "chrome relay returned invalid JSON",
                status=502,
                code="relay_invalid_json",
            ) from exc

    def _relay_request_with_path_fallbacks(
        self,
        paths: list[str],
        *,
        query: dict[str, Any] | None = None,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: BrowserRuntimeError | None = None
        for path in paths:
            try:
                return self._relay_request(path, query=query, method=method, body=body)
            except BrowserRuntimeError as exc:
                last_error = exc
                if exc.code == "relay_http_error" and exc.status == 404:
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise BrowserRuntimeError("chrome relay path fallback is empty", status=500)

    def _ensure_running(self) -> None:
        if self._browser is None or self._context is None:
            raise BrowserRuntimeError("browser is not running; call action=start first", status=409)

    def _sync_existing_tabs(self) -> None:
        self._ensure_running()
        pages = list(self._context.pages)  # type: ignore[union-attr]
        existing_by_page = {entry.page: entry.target_id for entry in self._tabs.values()}
        next_tabs: dict[str, _PlaywrightTab] = {}
        for page in pages:
            existing_id = existing_by_page.get(page)
            target_id = existing_id or f"tab-{uuid.uuid4().hex[:8]}"
            next_tabs[target_id] = _PlaywrightTab(target_id=target_id, page=page)
            self._bind_console_listener(target_id, page)
        stale_target_ids = set(self._tabs.keys()) - set(next_tabs.keys())
        for stale_target_id in stale_target_ids:
            self._console_messages_by_tab.pop(stale_target_id, None)
            self._snapshot_ref_selectors_by_tab.pop(stale_target_id, None)
        self._bound_console_page_ids = {id(page) for page in pages}
        self._tabs = next_tabs
        if self._last_target_id not in self._tabs and self._tabs:
            self._last_target_id = next(reversed(self._tabs))

    def _register_page(self, page: Any) -> str:
        target_id = f"tab-{uuid.uuid4().hex[:8]}"
        self._tabs[target_id] = _PlaywrightTab(target_id=target_id, page=page)
        self._bind_console_listener(target_id, page)
        self._last_target_id = target_id
        return target_id

    def _bind_console_listener(self, target_id: str, page: Any) -> None:
        page_key = id(page)
        if page_key in self._bound_console_page_ids:
            return

        def _on_console(message: Any) -> None:
            level = str(getattr(message, "type", "") or "log").strip().lower() or "log"
            text = str(getattr(message, "text", "") or "").strip()
            bucket = self._console_messages_by_tab.setdefault(target_id, [])
            bucket.append({"level": level, "text": text})
            if len(bucket) > 200:
                del bucket[:-200]

        page.on("console", _on_console)
        self._bound_console_page_ids.add(page_key)

    def _resolve_tab(self, target_id: str | None) -> _PlaywrightTab:
        self._sync_existing_tabs()
        if not self._tabs:
            raise BrowserRuntimeError("no tabs available; call action=open first", status=404)
        if target_id:
            tab = self._tabs.get(target_id)
            if tab is None:
                raise BrowserRuntimeError("tab not found", status=404)
            return tab
        if self._last_target_id and self._last_target_id in self._tabs:
            return self._tabs[self._last_target_id]
        return next(reversed(self._tabs.values()))

    def _selector_from_request(self, request: dict[str, Any], target_id: str) -> str:
        selector = str(request.get("selector", "")).strip()
        ref = str(request.get("ref", "")).strip()
        if selector:
            return selector
        if not ref:
            raise BrowserRuntimeError("request.selector or request.ref is required")
        mapped = self._snapshot_ref_selectors_by_tab.get(target_id, {}).get(ref)
        if mapped:
            return mapped
        if _SNAPSHOT_REF_PATTERN.fullmatch(ref):
            raise BrowserRuntimeError("request.ref not found in current snapshot; run action=snapshot first")
        return ref

    def _selector_from_drag_request(self, request: dict[str, Any], target_id: str, side: str) -> str:
        key_selector = "startSelector" if side == "start" else "endSelector"
        key_ref = "startRef" if side == "start" else "endRef"
        selector = str(request.get(key_selector, "")).strip()
        if selector:
            return selector
        ref = str(request.get(key_ref, "")).strip()
        if not ref:
            raise BrowserRuntimeError("request.startRef/startSelector and request.endRef/endSelector are required for drag")
        mapped = self._snapshot_ref_selectors_by_tab.get(target_id, {}).get(ref)
        if mapped:
            return mapped
        if _SNAPSHOT_REF_PATTERN.fullmatch(ref):
            raise BrowserRuntimeError(f"request.{key_ref} not found in current snapshot; run action=snapshot first")
        return ref

    def _extract_ref_selectors_from_snapshot(self, payload: dict[str, Any]) -> dict[str, str]:
        selectors: dict[str, str] = {}
        explicit = payload.get("refSelectors")
        if isinstance(explicit, dict):
            for ref, selector in explicit.items():
                ref_value = str(ref).strip()
                selector_value = str(selector).strip()
                if ref_value and selector_value:
                    selectors[ref_value] = selector_value
            return selectors

        alt = payload.get("selectorByRef")
        if isinstance(alt, dict):
            for ref, selector in alt.items():
                ref_value = str(ref).strip()
                selector_value = str(selector).strip()
                if ref_value and selector_value:
                    selectors[ref_value] = selector_value
            return selectors

        refs_payload = payload.get("refs")
        if isinstance(refs_payload, dict):
            for ref, meta in refs_payload.items():
                if not isinstance(meta, dict):
                    continue
                selector_value = str(meta.get("selector", "")).strip()
                ref_value = str(ref).strip()
                if ref_value and selector_value:
                    selectors[ref_value] = selector_value
        return selectors

    def _normalize_relay_act_request(
        self,
        *,
        request: dict[str, Any],
        target_id: str,
    ) -> dict[str, Any]:
        kind = str(request.get("kind", "")).strip().lower()
        if kind not in _CHROME_RELAY_ACT_KINDS:
            raise BrowserRuntimeError(
                "chrome relay act currently supports kinds: open,click,type,wait,press,hover,select,drag,fill,resize,close,evaluate",
                status=501,
            )

        relay_request = dict(request)
        relay_request["kind"] = kind

        if kind == "type":
            text = relay_request.get("text")
            if not isinstance(text, str):
                raise BrowserRuntimeError("request.text is required for type")
            max_chars = self._resolve_chrome_relay_type_max_chars()
            if len(text) > max_chars:
                raise BrowserRuntimeError(
                    f"request.text is too long for chrome relay type (max {max_chars} chars)"
                )
        if kind == "open":
            target_url = str(relay_request.get("url", "") or relay_request.get("targetUrl", "")).strip()
            if not target_url:
                raise BrowserRuntimeError("request.url is required for open")
            validate_browser_url(target_url)
            relay_request["url"] = target_url
        if kind == "press":
            key = str(relay_request.get("key", "")).strip()
            if not key:
                raise BrowserRuntimeError("request.key is required for press")
            relay_request["key"] = key
        if kind == "wait":
            timeout_ms = relay_request.get("timeMs")
            wait_ms = int(timeout_ms) if isinstance(timeout_ms, (int, float)) else 500
            relay_request["timeMs"] = max(0, min(wait_ms, 60_000))
        if kind == "select":
            values = relay_request.get("values")
            if not isinstance(values, list) or not values:
                raise BrowserRuntimeError("request.values is required for select")
            normalized_values = [str(item).strip() for item in values if str(item).strip()]
            if not normalized_values:
                raise BrowserRuntimeError("request.values is required for select")
            relay_request["values"] = normalized_values
        if kind == "drag":
            start_selector = self._selector_from_drag_request(relay_request, target_id, "start")
            end_selector = self._selector_from_drag_request(relay_request, target_id, "end")
            relay_request["startSelector"] = start_selector
            relay_request["endSelector"] = end_selector
        if kind == "fill":
            fields = relay_request.get("fields")
            if not isinstance(fields, list) or not fields:
                raise BrowserRuntimeError("request.fields is required for fill")
            normalized_fields: list[dict[str, Any]] = []
            for raw in fields:
                if not isinstance(raw, dict):
                    continue
                fill_text = raw.get("text")
                if not isinstance(fill_text, str):
                    fill_text = raw.get("value")
                if not isinstance(fill_text, str):
                    continue
                field_selector = self._selector_from_request(raw, target_id)
                normalized_fields.append({"selector": field_selector, "text": fill_text})
            if not normalized_fields:
                raise BrowserRuntimeError("request.fields is required for fill")
            relay_request["fields"] = normalized_fields
        if kind == "resize":
            width = relay_request.get("width")
            height = relay_request.get("height")
            if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
                raise BrowserRuntimeError("request.width and request.height are required for resize")
            relay_request["width"] = int(width)
            relay_request["height"] = int(height)
        if kind == "evaluate":
            if not self._relay_evaluate_enabled():
                raise BrowserRuntimeError(
                    "chrome relay evaluate is disabled; set OPENPIPIXIA_BROWSER_CHROME_RELAY_EVALUATE_ENABLED=1 to enable",
                    status=501,
                )
            fn = relay_request.get("fn")
            if not isinstance(fn, str) or not fn.strip():
                raise BrowserRuntimeError("request.fn is required for evaluate")
            max_chars = self._resolve_chrome_relay_evaluate_max_chars()
            if len(fn) > max_chars:
                raise BrowserRuntimeError(
                    f"request.fn is too long for chrome relay evaluate (max {max_chars} chars)"
                )
            relay_request["fn"] = fn

        selector = str(relay_request.get("selector", "")).strip()
        ref = str(relay_request.get("ref", "")).strip()
        if kind in {"click", "type", "hover", "select"} and not selector and not ref:
            raise BrowserRuntimeError("request.selector or request.ref is required")
        if ref and not selector:
            mapped_selector = self._snapshot_ref_selectors_by_tab.get(target_id, {}).get(ref)
            if mapped_selector:
                selector = mapped_selector
            elif _SNAPSHOT_REF_PATTERN.fullmatch(ref):
                raise BrowserRuntimeError("request.ref not found in current snapshot; run action=snapshot first")
            else:
                selector = ref
        if selector:
            relay_request["selector"] = selector
        if ref:
            relay_request["ref"] = ref
        return relay_request

    def _tab_payload(self, tab: _PlaywrightTab) -> dict[str, Any]:
        return {
            "targetId": tab.target_id,
            "url": tab.page.url,
            "title": tab.page.title(),
            "type": "page",
        }
