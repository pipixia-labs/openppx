"""Browser route registration for openheron control service.

Iteration 1 introduces a route layer between the tool-facing API and browser
runtime so future adapters (Playwright/CDP/remote proxy) can plug in without
changing tool contracts.
"""

from __future__ import annotations

from typing import Any, Protocol

from .browser_runtime import BrowserRuntime, BrowserRuntimeError


class BrowserRouteRequest(Protocol):
    """Lightweight request protocol consumed by route handlers."""

    method: str
    path: str
    query: dict[str, Any]
    body: dict[str, Any]


class BrowserRouteResponse(Protocol):
    """Lightweight response protocol consumed by route handlers."""

    def status(self, code: int) -> "BrowserRouteResponse":
        """Set response status code."""

    def json(self, payload: Any) -> None:
        """Set JSON payload."""


class BrowserRouteRegistrar(Protocol):
    """Route registry protocol used by `register_browser_routes`."""

    def get(self, path: str, handler: Any) -> None:
        """Register GET route."""

    def post(self, path: str, handler: Any) -> None:
        """Register POST route."""


_SUPPORTED_TARGETS = {"host", "sandbox", "node"}


def _ensure_supported_target(req: BrowserRouteRequest, res: BrowserRouteResponse) -> bool:
    """Validate target routing hint from query.

    Iteration keeps host runtime as the only executable backend while making
    `target` semantics explicit at route layer for compatibility.
    """

    raw_target = str(req.query.get("target") or "").strip().lower()
    target = raw_target or "host"
    if target not in _SUPPORTED_TARGETS:
        res.status(400).json({"ok": False, "error": "target must be host, sandbox, or node"})
        return False
    if target != "host":
        res.status(501).json({"ok": False, "error": f'target "{target}" is not implemented yet'})
        return False
    return True


def _runtime_error_payload(exc: BrowserRuntimeError) -> dict[str, Any]:
    return {"ok": False, "error": str(exc), "status": exc.status}


def _handle_runtime_error(res: BrowserRouteResponse, exc: BrowserRuntimeError) -> None:
    res.status(exc.status).json(_runtime_error_payload(exc))


def register_browser_basic_routes(registrar: BrowserRouteRegistrar, runtime: BrowserRuntime) -> None:
    """Register browser basic routes (status/lifecycle/profile/tab list)."""

    def status_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        try:
            res.json(runtime.status(profile=str(req.query.get("profile") or "").strip() or None))
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def start_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        try:
            res.json(runtime.start(profile=str(req.query.get("profile") or "").strip() or None))
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def stop_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        try:
            res.json(runtime.stop(profile=str(req.query.get("profile") or "").strip() or None))
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def profiles_route(_req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        # profiles are runtime level; keep host-only target compatibility.
        if not _ensure_supported_target(_req, res):
            return
        try:
            res.json(runtime.profiles())
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def tabs_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        try:
            res.json(runtime.tabs(profile=str(req.query.get("profile") or "").strip() or None))
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    registrar.get("/", status_route)
    registrar.post("/start", start_route)
    registrar.post("/stop", stop_route)
    registrar.get("/profiles", profiles_route)
    registrar.get("/tabs", tabs_route)


def register_browser_agent_routes(registrar: BrowserRouteRegistrar, runtime: BrowserRuntime) -> None:
    """Register browser agent routes (open/navigate/snapshot/screenshot/upload/dialog/act)."""

    def open_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        url = str(req.body.get("url") or "").strip()
        if not url:
            res.status(400).json({"ok": False, "error": "url is required"})
            return
        profile = str(req.query.get("profile") or "").strip() or None
        try:
            res.json(runtime.open_tab(url=url, profile=profile))
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def focus_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        target_id = str(req.body.get("targetId") or "").strip() or None
        profile = str(req.query.get("profile") or "").strip() or None
        try:
            res.json(runtime.focus_tab(target_id=target_id, profile=profile))
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def close_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        target_id = str(req.body.get("targetId") or "").strip() or None
        profile = str(req.query.get("profile") or "").strip() or None
        try:
            res.json(runtime.close_tab(target_id=target_id, profile=profile))
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def snapshot_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        query = req.query
        target_id = str(query.get("targetId") or "").strip() or None
        snapshot_format = str(query.get("format") or "ai").strip().lower() or "ai"
        profile = str(query.get("profile") or "").strip() or None
        try:
            res.json(
                runtime.snapshot(
                    target_id=target_id,
                    snapshot_format=snapshot_format,
                    profile=profile,
                )
            )
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def navigate_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        body = req.body
        url = str(body.get("url") or "").strip()
        if not url:
            res.status(400).json({"ok": False, "error": "url is required"})
            return
        target_id = str(body.get("targetId") or "").strip() or None
        profile = str(req.query.get("profile") or "").strip() or None
        try:
            res.json(runtime.navigate(url=url, target_id=target_id, profile=profile))
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def screenshot_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        body = req.body
        target_id = str(body.get("targetId") or "").strip() or None
        image_type = str(body.get("type") or "png").strip().lower() or "png"
        out_path = str(body.get("path") or "").strip() or None
        profile = str(req.query.get("profile") or "").strip() or None
        try:
            res.json(
                runtime.screenshot(
                    target_id=target_id,
                    profile=profile,
                    image_type=image_type,
                    out_path=out_path,
                )
            )
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def upload_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        body = req.body
        raw_paths = body.get("paths")
        paths = [str(item) for item in raw_paths] if isinstance(raw_paths, list) else []
        target_id = str(body.get("targetId") or "").strip() or None
        ref = str(body.get("ref") or "").strip() or None
        profile = str(req.query.get("profile") or "").strip() or None
        if not paths:
            res.status(400).json({"ok": False, "error": "paths are required"})
            return
        try:
            res.json(
                runtime.upload(
                    paths=paths,
                    target_id=target_id,
                    profile=profile,
                    ref=ref,
                )
            )
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def dialog_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        body = req.body
        accept_raw = body.get("accept")
        if not isinstance(accept_raw, bool):
            res.status(400).json({"ok": False, "error": "accept is required and must be boolean"})
            return
        target_id = str(body.get("targetId") or "").strip() or None
        prompt_text = str(body.get("promptText") or "").strip() or None
        profile = str(req.query.get("profile") or "").strip() or None
        try:
            res.json(
                runtime.dialog(
                    accept=accept_raw,
                    target_id=target_id,
                    profile=profile,
                    prompt_text=prompt_text,
                )
            )
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    def act_route(req: BrowserRouteRequest, res: BrowserRouteResponse) -> None:
        if not _ensure_supported_target(req, res):
            return
        body = req.body
        request = body.get("request")
        if not isinstance(request, dict):
            # Compatibility path: accept flat /act payloads where action fields
            # are passed directly in request body.
            request = body
        if not isinstance(request, dict) or not str(request.get("kind") or "").strip():
            res.status(400).json({"ok": False, "error": "request.kind is required"})
            return
        target_id = str(body.get("targetId") or "").strip() or None
        profile = str(req.query.get("profile") or "").strip() or None
        try:
            res.json(runtime.act(request=request, target_id=target_id, profile=profile))
        except BrowserRuntimeError as exc:
            _handle_runtime_error(res, exc)

    registrar.post("/tabs/open", open_route)
    registrar.post("/tabs/focus", focus_route)
    registrar.post("/tabs/close", close_route)
    registrar.post("/navigate", navigate_route)
    registrar.get("/snapshot", snapshot_route)
    registrar.post("/screenshot", screenshot_route)
    registrar.post("/hooks/file-chooser", upload_route)
    registrar.post("/hooks/dialog", dialog_route)
    registrar.post("/act", act_route)


def register_browser_routes(registrar: BrowserRouteRegistrar, runtime: BrowserRuntime) -> None:
    """Register all browser routes for the control service."""

    register_browser_basic_routes(registrar, runtime)
    register_browser_agent_routes(registrar, runtime)
