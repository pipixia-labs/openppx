"""Playwright-backed browser runtime for openheron (Iteration 2)."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from typing import Any
import uuid

from .browser_runtime import (
    BrowserRuntimeError,
    validate_browser_upload_paths,
    validate_browser_url,
)

_SUPPORTED_PROFILES = {"openheron", "chrome"}


@dataclass(slots=True)
class _PlaywrightTab:
    target_id: str
    page: Any


class PlaywrightBrowserRuntime:
    """Minimal Playwright runtime with optional CDP attach mode.

    Environment flags:
    - ``OPENHERON_BROWSER_CDP_URL``: if set, use connect-over-CDP mode.
    - ``OPENHERON_BROWSER_HEADLESS``: used for local launch mode (default: true).
    """

    def __init__(self) -> None:
        self._pw_context_manager: Any | None = None
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._tabs: dict[str, _PlaywrightTab] = {}
        self._last_target_id: str | None = None
        self._mode: str = "idle"
        self._cdp_url: str | None = None

    def status(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        if resolved_profile == "chrome":
            return {
                "enabled": True,
                "running": False,
                "profile": "chrome",
                "tabCount": 0,
                "lastTargetId": None,
                "backend": "extension-relay",
                "mode": "unsupported",
                "available": False,
            }
        return {
            "enabled": True,
            "running": self._browser is not None,
            "profile": resolved_profile,
            "tabCount": len(self._tabs),
            "lastTargetId": self._last_target_id,
            "backend": "playwright",
            "mode": self._mode,
        }

    def start(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        if self._browser is not None:
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
        cdp_url = os.getenv("OPENHERON_BROWSER_CDP_URL", "").strip()
        self._cdp_url = cdp_url or None
        if cdp_url:
            self._browser = chromium.connect_over_cdp(cdp_url)
            contexts = list(self._browser.contexts)
            self._context = contexts[0] if contexts else self._browser.new_context()
            self._mode = "cdp"
        else:
            headless = os.getenv("OPENHERON_BROWSER_HEADLESS", "1").strip().lower() not in {
                "0",
                "false",
                "off",
                "no",
            }
            self._browser = chromium.launch(headless=headless)
            self._context = self._browser.new_context()
            self._mode = "launch"
        self._sync_existing_tabs()
        return self.status(profile=resolved_profile)

    def stop(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser is not None:
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
        self._tabs = {}
        self._last_target_id = None
        self._mode = "idle"
        return self.status(profile=resolved_profile)

    def profiles(self) -> dict[str, Any]:
        return {
            "profiles": [
                {
                    "name": "openheron",
                    "driver": "playwright",
                    "description": "Playwright runtime profile",
                    "available": True,
                },
                {
                    "name": "chrome",
                    "driver": "extension-relay",
                    "description": "Chrome extension relay profile (not implemented yet)",
                    "available": False,
                },
            ]
        }

    def tabs(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        if resolved_profile == "chrome":
            return {
                "running": False,
                "profile": "chrome",
                "tabs": [],
                "backend": "extension-relay",
                "mode": "unsupported",
            }
        self._ensure_profile_supported(resolved_profile)
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
        self._ensure_running()
        validate_browser_url(url)
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

    def snapshot(
        self,
        *,
        target_id: str | None = None,
        snapshot_format: str = "ai",
        profile: str | None = None,
    ) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
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
            "refs": {},
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
        tab = self._resolve_tab(target_id)
        kind = str(request.get("kind", "")).strip().lower()
        if not kind:
            raise BrowserRuntimeError("request.kind is required")
        if kind not in {"click", "type", "press", "wait", "close"}:
            raise BrowserRuntimeError(f"unsupported act kind: {kind}")

        if kind == "click":
            selector = self._selector_from_request(request)
            tab.page.locator(selector).first.click()
        elif kind == "type":
            selector = self._selector_from_request(request)
            text = request.get("text")
            if not isinstance(text, str):
                raise BrowserRuntimeError("request.text is required for type")
            tab.page.locator(selector).first.fill(text)
        elif kind == "press":
            key = str(request.get("key", "")).strip()
            if not key:
                raise BrowserRuntimeError("request.key is required for press")
            tab.page.keyboard.press(key)
        elif kind == "wait":
            timeout_ms = request.get("timeMs")
            wait_ms = int(timeout_ms) if isinstance(timeout_ms, (int, float)) else 500
            tab.page.wait_for_timeout(wait_ms)
        elif kind == "close":
            tab.page.close()
            self._tabs.pop(tab.target_id, None)
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
        tab = self._resolve_tab(target_id)
        fmt = image_type.strip().lower()
        if fmt not in {"png", "jpeg"}:
            raise BrowserRuntimeError("image_type must be 'png' or 'jpeg'")
        screenshot_kwargs: dict[str, Any] = {"type": fmt if fmt == "png" else "jpeg"}
        saved_path: str | None = None
        if out_path and out_path.strip():
            saved_path = os.path.abspath(out_path.strip())
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
        tab = self._resolve_tab(target_id)
        resolved = validate_browser_upload_paths(paths)

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
        resolved = (profile or "").strip().lower() or "openheron"
        if resolved not in _SUPPORTED_PROFILES:
            raise BrowserRuntimeError("unknown profile; supported profiles are openheron, chrome")
        return resolved

    def _ensure_profile_supported(self, profile: str) -> None:
        if profile == "chrome":
            raise BrowserRuntimeError('profile "chrome" is not implemented yet', status=501)

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
        self._tabs = next_tabs
        if self._last_target_id not in self._tabs and self._tabs:
            self._last_target_id = next(reversed(self._tabs))

    def _register_page(self, page: Any) -> str:
        target_id = f"tab-{uuid.uuid4().hex[:8]}"
        self._tabs[target_id] = _PlaywrightTab(target_id=target_id, page=page)
        self._last_target_id = target_id
        return target_id

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

    def _selector_from_request(self, request: dict[str, Any]) -> str:
        selector = str(request.get("selector", "")).strip()
        ref = str(request.get("ref", "")).strip()
        chosen = selector or ref
        if not chosen:
            raise BrowserRuntimeError("request.selector or request.ref is required")
        return chosen

    def _tab_payload(self, tab: _PlaywrightTab) -> dict[str, Any]:
        return {
            "targetId": tab.target_id,
            "url": tab.page.url,
            "title": tab.page.title(),
            "type": "page",
        }
