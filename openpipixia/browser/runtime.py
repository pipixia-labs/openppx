"""Minimal browser runtime abstraction for openpipixia native browser tool.

This module provides an in-memory runtime used by Iteration 0 so the browser
tool has a deterministic, testable execution path before wiring real
Playwright/CDP backends in later iterations.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import ipaddress
import json
import os
import socket
from typing import Any, Protocol
from urllib.parse import urlparse
import uuid

from .schema import apply_status_metadata, make_profile_entry, make_runtime_capability
from ..core.env_utils import env_enabled


_SUPPORTED_SCHEMES = {"http", "https", "about"}
_LOCAL_HOSTS = {"localhost", "localhost.localdomain"}
_SUPPORTED_PROFILES = {"openpipixia", "chrome"}
_OPENPIPIXIA_ACTIONS = [
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
_CHROME_ACTIONS = ["status", "profiles"]


@dataclass(slots=True)
class BrowserTab:
    """A lightweight tab record returned by the browser runtime."""

    target_id: str
    url: str
    title: str
    tab_type: str = "page"


class BrowserRuntimeError(RuntimeError):
    """Browser runtime error with an HTTP-like status code."""

    def __init__(self, message: str, *, status: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class BrowserRuntime(Protocol):
    """Protocol for browser runtime backends."""

    def status(self, *, profile: str | None = None) -> dict[str, Any]:
        """Return browser runtime status."""

    def start(self, *, profile: str | None = None) -> dict[str, Any]:
        """Start browser runtime."""

    def stop(self, *, profile: str | None = None) -> dict[str, Any]:
        """Stop browser runtime."""

    def profiles(self) -> dict[str, Any]:
        """Return available browser profiles."""

    def tabs(self, *, profile: str | None = None) -> dict[str, Any]:
        """Return opened tabs."""

    def focus_tab(self, *, target_id: str | None = None, profile: str | None = None) -> dict[str, Any]:
        """Focus one tab."""

    def close_tab(self, *, target_id: str | None = None, profile: str | None = None) -> dict[str, Any]:
        """Close one tab."""

    def open_tab(self, *, url: str, profile: str | None = None) -> dict[str, Any]:
        """Open a new tab."""

    def navigate(
        self,
        *,
        url: str,
        target_id: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Navigate an existing tab to a URL."""

    def snapshot(
        self,
        *,
        target_id: str | None = None,
        snapshot_format: str = "ai",
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Return snapshot for one tab."""

    def act(
        self,
        *,
        request: dict[str, Any],
        target_id: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Run one browser action on the selected tab."""

    def screenshot(
        self,
        *,
        target_id: str | None = None,
        profile: str | None = None,
        image_type: str = "png",
        out_path: str | None = None,
    ) -> dict[str, Any]:
        """Capture tab screenshot."""

    def pdf_save(
        self,
        *,
        target_id: str | None = None,
        profile: str | None = None,
        out_path: str | None = None,
    ) -> dict[str, Any]:
        """Save current page to PDF."""

    def console_messages(
        self,
        *,
        target_id: str | None = None,
        profile: str | None = None,
        level: str | None = None,
        out_path: str | None = None,
    ) -> dict[str, Any]:
        """Read console messages for a tab."""

    def upload(
        self,
        *,
        paths: list[str],
        target_id: str | None = None,
        profile: str | None = None,
        ref: str | None = None,
    ) -> dict[str, Any]:
        """Upload files to the current page context."""

    def dialog(
        self,
        *,
        accept: bool,
        target_id: str | None = None,
        profile: str | None = None,
        prompt_text: str | None = None,
    ) -> dict[str, Any]:
        """Arm dialog handling for the current page."""


def _is_private_or_local_ip(raw_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_private_network_policy(hostname: str) -> None:
    if not env_enabled("OPENPIPIXIA_BROWSER_BLOCK_PRIVATE_NETWORKS", default=True):
        return

    normalized = hostname.strip().lower()
    if not normalized:
        return
    if normalized in _LOCAL_HOSTS or normalized.endswith(".localhost"):
        raise BrowserRuntimeError("navigation to private host is blocked by policy")
    if _is_private_or_local_ip(normalized):
        raise BrowserRuntimeError("navigation to private host is blocked by policy")

    if not env_enabled("OPENPIPIXIA_BROWSER_BLOCK_PRIVATE_DNS", default=False):
        return

    try:
        infos = socket.getaddrinfo(normalized, None)
    except OSError:
        # Keep navigation available when DNS cannot be resolved in current environment.
        return
    for info in infos:
        sockaddr = info[4]
        ip_text = str(sockaddr[0]) if isinstance(sockaddr, tuple) and sockaddr else ""
        if ip_text and _is_private_or_local_ip(ip_text):
            raise BrowserRuntimeError("navigation to private host is blocked by policy")


def validate_browser_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in _SUPPORTED_SCHEMES:
        raise BrowserRuntimeError("target_url must use http/https/about scheme")
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise BrowserRuntimeError("target_url must include host for http/https")
    if parsed.scheme in {"http", "https"}:
        _validate_private_network_policy(parsed.hostname or "")


def _resolve_upload_root() -> str:
    configured = os.getenv("OPENPIPIXIA_BROWSER_UPLOAD_ROOT", "").strip()
    if configured:
        return os.path.realpath(os.path.abspath(os.path.expanduser(configured)))
    workspace = os.getenv("OPENPIPIXIA_WORKSPACE", "").strip()
    base = workspace or os.getcwd()
    return os.path.realpath(os.path.abspath(base))


def _is_within_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def _resolve_artifact_root() -> str:
    configured = os.getenv("OPENPIPIXIA_BROWSER_ARTIFACT_ROOT", "").strip()
    if configured:
        return os.path.realpath(os.path.abspath(os.path.expanduser(configured)))
    workspace = os.getenv("OPENPIPIXIA_WORKSPACE", "").strip()
    base = workspace or os.getcwd()
    return os.path.realpath(os.path.abspath(os.path.join(base, ".openpipixia", "browser_artifacts")))


def resolve_browser_artifact_path(out_path: str | None, *, default_filename: str) -> str:
    """Resolve artifact output path with optional root restriction."""

    enforce_root = env_enabled("OPENPIPIXIA_BROWSER_ENFORCE_ARTIFACT_ROOT", default=True)
    artifact_root = _resolve_artifact_root()
    target_raw = out_path.strip() if out_path else ""
    if not target_raw:
        target_path = os.path.join(artifact_root, default_filename)
    else:
        target_path = os.path.abspath(os.path.expanduser(target_raw))
    resolved = os.path.realpath(target_path)
    if enforce_root and not _is_within_root(resolved, artifact_root):
        raise BrowserRuntimeError(f"artifact path is outside artifact root: {target_raw or target_path}")
    return resolved


def validate_browser_upload_paths(paths: list[str]) -> list[str]:
    """Validate and normalize upload paths with optional root restriction."""

    enforce_root = env_enabled("OPENPIPIXIA_BROWSER_ENFORCE_UPLOAD_ROOT", default=True)
    upload_root = _resolve_upload_root() if enforce_root else ""

    if not paths:
        raise BrowserRuntimeError("paths are required for upload")

    resolved: list[str] = []
    for raw in paths:
        path_value = str(raw).strip()
        if not path_value:
            continue
        abs_path = os.path.abspath(path_value)
        if not os.path.isfile(abs_path):
            raise BrowserRuntimeError(f"upload file not found: {path_value}", status=404)
        real_path = os.path.realpath(abs_path)
        if enforce_root and not _is_within_root(real_path, upload_root):
            raise BrowserRuntimeError(f"upload path is outside upload root: {path_value}")
        resolved.append(real_path)

    if not resolved:
        raise BrowserRuntimeError("paths are required for upload")
    return resolved


class InMemoryBrowserRuntime:
    """In-memory browser runtime for Iteration 0.

    The runtime emulates OpenClaw-like action flow (`open -> snapshot -> act`)
    while keeping behavior deterministic for unit tests.
    """

    def __init__(self) -> None:
        self._running = False
        self._tabs: list[BrowserTab] = []
        self._last_target_id: str | None = None
        self._console_messages_by_tab: dict[str, list[dict[str, str]]] = {}

    def status(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        if resolved_profile == "chrome":
            return apply_status_metadata(
                {
                    "enabled": True,
                    "running": False,
                    "profile": "chrome",
                    "tabCount": 0,
                    "lastTargetId": None,
                    "driver": "extension-relay",
                    "available": False,
                    "capability": make_runtime_capability(
                        backend="extension-relay",
                        driver="extension-relay",
                        mode="unsupported",
                        attach_mode="cdp-required",
                        supported_actions=_CHROME_ACTIONS,
                    ),
                },
                attach_mode="cdp-required",
                browser_owned=False,
                context_owned=False,
            )
        return apply_status_metadata(
            {
                "enabled": True,
                "running": self._running,
                "profile": resolved_profile,
                "tabCount": len(self._tabs),
                "lastTargetId": self._last_target_id,
                "capability": make_runtime_capability(
                    backend="memory",
                    driver="memory",
                    mode="simulated",
                    attach_mode="memory-simulated",
                    supported_actions=_OPENPIPIXIA_ACTIONS,
                ),
            },
            attach_mode="memory-simulated",
            browser_owned=False,
            context_owned=False,
        )

    def start(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._running = True
        return self.status(profile=resolved_profile)

    def stop(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        self._running = False
        self._tabs = []
        self._last_target_id = None
        self._console_messages_by_tab = {}
        return self.status(profile=resolved_profile)

    def profiles(self) -> dict[str, Any]:
        return {
            "profiles": [
                make_profile_entry(
                    name="openpipixia",
                    driver="memory",
                    description="Iteration 0 in-memory browser profile",
                    available=True,
                    attach_mode="memory-simulated",
                    ownership_model={
                        "browser": "not-applicable",
                        "context": "not-applicable",
                    },
                    capability=make_runtime_capability(
                        backend="memory",
                        driver="memory",
                        mode="simulated",
                        attach_mode="memory-simulated",
                        supported_actions=_OPENPIPIXIA_ACTIONS,
                    ),
                ),
                make_profile_entry(
                    name="chrome",
                    driver="extension-relay",
                    description="Chrome extension relay profile (not implemented yet)",
                    available=False,
                    attach_mode="cdp-required",
                    requires={"OPENPIPIXIA_BROWSER_CHROME_CDP_URL": True},
                    ownership_model={
                        "browser": "borrowed",
                        "context": "borrowed-or-owned-if-created",
                    },
                    capability=make_runtime_capability(
                        backend="extension-relay",
                        driver="extension-relay",
                        mode="unsupported",
                        attach_mode="cdp-required",
                        supported_actions=_CHROME_ACTIONS,
                    ),
                ),
            ]
        }

    def tabs(self, *, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        if resolved_profile == "chrome":
            return {
                "running": False,
                "profile": "chrome",
                "tabs": [],
            }
        return {
            "running": self._running,
            "profile": resolved_profile,
            "tabs": [
                {
                    "targetId": tab.target_id,
                    "url": tab.url,
                    "title": tab.title,
                    "type": tab.tab_type,
                }
                for tab in self._tabs
            ],
        }

    def open_tab(self, *, url: str, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        if not self._running:
            raise BrowserRuntimeError("browser is not running; call action=start first", status=409)
        validate_browser_url(url)
        tab = BrowserTab(
            target_id=f"tab-{uuid.uuid4().hex[:8]}",
            url=url,
            title=f"OpenPipixia: {url}",
        )
        self._tabs.append(tab)
        self._last_target_id = tab.target_id
        self._record_console_message(tab.target_id, "info", f"opened {url}")
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.url,
            "title": tab.title,
        }

    def focus_tab(self, *, target_id: str | None = None, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        tab = self._resolve_tab(target_id)
        self._last_target_id = tab.target_id
        self._record_console_message(tab.target_id, "debug", "tab focused")
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.url,
            "focused": True,
        }

    def close_tab(self, *, target_id: str | None = None, profile: str | None = None) -> dict[str, Any]:
        resolved_profile = self._resolve_profile(profile)
        self._ensure_profile_supported(resolved_profile)
        tab = self._resolve_tab(target_id)
        self._tabs = [entry for entry in self._tabs if entry.target_id != tab.target_id]
        self._console_messages_by_tab.pop(tab.target_id, None)
        self._last_target_id = self._tabs[-1].target_id if self._tabs else None
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "closed": True,
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
        if snapshot_format not in {"ai", "aria"}:
            raise BrowserRuntimeError("snapshot_format must be 'ai' or 'aria'")
        self._last_target_id = tab.target_id
        if snapshot_format == "aria":
            return {
                "ok": True,
                "format": "aria",
                "profile": resolved_profile,
                "targetId": tab.target_id,
                "url": tab.url,
                "nodes": [
                    {"ref": "ax1", "role": "document", "name": tab.title},
                    {"ref": "ax2", "role": "textbox", "name": "Search"},
                ],
            }
        return {
            "ok": True,
            "format": "ai",
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.url,
            "snapshot": f"URL: {tab.url}\nTitle: {tab.title}\nInteractive refs: e1(button), e2(input)",
            "refs": {
                "e1": {"role": "button", "name": "Submit"},
                "e2": {"role": "textbox", "name": "Input"},
            },
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
        tab.url = url
        tab.title = f"OpenPipixia: {url}"
        self._last_target_id = tab.target_id
        self._record_console_message(tab.target_id, "info", f"navigated to {url}")
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.url,
            "title": tab.title,
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
        kind = str(request.get("kind", "")).strip()
        if not kind:
            raise BrowserRuntimeError("request.kind is required")
        if kind not in {"click", "type", "press", "wait", "close", "hover", "select", "evaluate", "fill", "resize", "drag"}:
            raise BrowserRuntimeError(f"unsupported act kind: {kind}")
        selector = str(request.get("selector", "")).strip()
        ref = str(request.get("ref", "")).strip()
        if kind in {"click", "type", "hover", "select"} and not (selector or ref):
            raise BrowserRuntimeError("request.selector or request.ref is required for click/type/hover/select")
        if kind == "type" and not isinstance(request.get("text"), str):
            raise BrowserRuntimeError("request.text is required for type")
        if kind == "press" and not str(request.get("key", "")).strip():
            raise BrowserRuntimeError("request.key is required for press")
        if kind == "select":
            values = request.get("values")
            if not isinstance(values, list) or not values:
                raise BrowserRuntimeError("request.values is required for select")
        if kind == "evaluate" and not str(request.get("fn", "")).strip():
            raise BrowserRuntimeError("request.fn is required for evaluate")
        if kind == "fill":
            fields = request.get("fields")
            if not isinstance(fields, list) or not fields:
                raise BrowserRuntimeError("request.fields is required for fill")
        if kind == "resize":
            if not isinstance(request.get("width"), (int, float)) or not isinstance(request.get("height"), (int, float)):
                raise BrowserRuntimeError("request.width and request.height are required for resize")
        if kind == "drag":
            start_ref = str(request.get("startRef", "")).strip() or str(request.get("startSelector", "")).strip()
            end_ref = str(request.get("endRef", "")).strip() or str(request.get("endSelector", "")).strip()
            if not start_ref or not end_ref:
                raise BrowserRuntimeError("request.startRef/startSelector and request.endRef/endSelector are required for drag")

        if kind == "close":
            self._tabs = [entry for entry in self._tabs if entry.target_id != tab.target_id]
            self._console_messages_by_tab.pop(tab.target_id, None)
            self._last_target_id = self._tabs[-1].target_id if self._tabs else None
            return {
                "ok": True,
                "profile": resolved_profile,
                "targetId": tab.target_id,
                "closed": True,
            }
        self._last_target_id = tab.target_id
        self._record_console_message(tab.target_id, "info", f"act:{kind}")
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.url,
            "kind": kind,
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
        # 1x1 transparent PNG placeholder so contract is stable in memory mode.
        png_base64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y1koS8AAAAASUVORK5CYII="
        )
        data = png_base64 if fmt == "png" else png_base64
        content_type = "image/png" if fmt == "png" else "image/jpeg"
        binary = base64.b64decode(data)
        saved_path: str | None = None
        if out_path and out_path.strip():
            default_name = f"{tab.target_id}.{'png' if fmt == 'png' else 'jpg'}"
            target_path = resolve_browser_artifact_path(out_path, default_filename=default_name)
            dirpath = os.path.dirname(target_path) or "."
            os.makedirs(dirpath, exist_ok=True)
            with open(target_path, "wb") as f:
                f.write(binary)
            saved_path = target_path
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.url,
            "type": fmt,
            "contentType": content_type,
            "imageBase64": data,
            "bytes": len(binary),
            "path": saved_path,
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
        tab = self._resolve_tab(target_id)
        pdf_bytes = (
            b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
        )
        target_path = resolve_browser_artifact_path(out_path, default_filename=f"{tab.target_id}.pdf")
        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        with open(target_path, "wb") as f:
            f.write(pdf_bytes)
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "url": tab.url,
            "path": target_path,
            "bytes": len(pdf_bytes),
            "contentType": "application/pdf",
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
        tab = self._resolve_tab(target_id)
        normalized_level = (level or "").strip().lower() or "info"
        self._last_target_id = tab.target_id
        messages = list(self._console_messages_by_tab.get(tab.target_id, []))
        level_filter = (level or "").strip().lower()
        if level_filter:
            messages = [entry for entry in messages if entry.get("level", "").lower() == level_filter]
        if not messages:
            messages = [
                {
                    "level": normalized_level,
                    "text": f"console capture is synthetic in memory runtime ({tab.url})",
                }
            ]
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
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "uploadedPaths": resolved,
            "ref": ref or None,
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
        self._last_target_id = tab.target_id
        return {
            "ok": True,
            "profile": resolved_profile,
            "targetId": tab.target_id,
            "accept": bool(accept),
            "promptText": prompt_text or None,
            "armed": True,
        }

    def _resolve_profile(self, profile: str | None) -> str:
        resolved = (profile or "").strip().lower() or "openpipixia"
        if resolved not in _SUPPORTED_PROFILES:
            raise BrowserRuntimeError("unknown profile; supported profiles are openpipixia, chrome")
        return resolved

    def _ensure_profile_supported(self, profile: str) -> None:
        if profile == "chrome":
            raise BrowserRuntimeError('profile "chrome" is not implemented yet', status=501)

    def _record_console_message(self, target_id: str, level: str, text: str) -> None:
        bucket = self._console_messages_by_tab.setdefault(target_id, [])
        bucket.append({"level": level.strip().lower() or "info", "text": text})
        if len(bucket) > 100:
            del bucket[:-100]

    def _resolve_tab(self, target_id: str | None) -> BrowserTab:
        if not self._running:
            raise BrowserRuntimeError("browser is not running; call action=start first", status=409)
        if not self._tabs:
            raise BrowserRuntimeError("no tabs available; call action=open first", status=404)
        if target_id:
            for tab in self._tabs:
                if tab.target_id == target_id:
                    return tab
            raise BrowserRuntimeError("tab not found", status=404)
        if self._last_target_id:
            for tab in self._tabs:
                if tab.target_id == self._last_target_id:
                    return tab
        return self._tabs[-1]


_runtime: BrowserRuntime | None = None


def _create_playwright_runtime() -> BrowserRuntime:
    """Create Playwright runtime lazily to avoid hard dependency at import time."""

    from .playwright_runtime import PlaywrightBrowserRuntime

    return PlaywrightBrowserRuntime()


def _create_runtime_from_env() -> BrowserRuntime:
    """Resolve runtime implementation from environment with safe fallback.

    `OPENPIPIXIA_BROWSER_RUNTIME=playwright` enables Playwright backend. If
    Playwright backend cannot be created, we gracefully fallback to in-memory.
    """

    mode = os.getenv("OPENPIPIXIA_BROWSER_RUNTIME", "").strip().lower()
    if mode == "playwright":
        try:
            return _create_playwright_runtime()
        except Exception:
            if env_enabled("OPENPIPIXIA_BROWSER_RUNTIME_STRICT", default=False):
                raise
            return InMemoryBrowserRuntime()
    return InMemoryBrowserRuntime()


def get_browser_runtime() -> BrowserRuntime:
    """Return the active browser runtime."""

    global _runtime
    if _runtime is None:
        _runtime = _create_runtime_from_env()
    return _runtime


def configure_browser_runtime(runtime: BrowserRuntime | None) -> None:
    """Set browser runtime for tests or adapters.

    Passing ``None`` resets to the default in-memory runtime.
    """

    global _runtime
    _runtime = runtime if runtime is not None else _create_runtime_from_env()
    # Keep dispatcher routes bound to the newest runtime instance in tests.
    try:
        from .service import reset_browser_control_service

        reset_browser_control_service()
    except Exception:
        # Avoid hard dependency cycles during early imports.
        pass
