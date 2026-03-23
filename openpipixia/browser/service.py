"""Browser control service and in-process route dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable

from .routes import register_browser_routes
from .runtime import get_browser_runtime
from .schema import normalize_profile_payload_aliases


@dataclass(slots=True)
class BrowserDispatchRequest:
    """Input request for browser route dispatcher."""

    method: str
    path: str
    query: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    auth_token: str | None = None
    mutation_token: str | None = None


@dataclass(slots=True)
class BrowserDispatchResponse:
    """Output response for browser route dispatcher."""

    status: int
    body: Any


RouteHandler = Callable[["_DispatchRequest", "_DispatchResponse"], None]


@dataclass(slots=True)
class _Route:
    method: str
    path: str
    handler: RouteHandler


@dataclass(slots=True)
class _DispatchRequest:
    method: str
    path: str
    query: dict[str, Any]
    body: dict[str, Any]


class _DispatchResponse:
    """Mutable response passed to route handlers."""

    def __init__(self) -> None:
        self.status_code = 200
        self.payload: Any = None

    def status(self, code: int) -> "_DispatchResponse":
        self.status_code = code
        return self

    def json(self, payload: Any) -> None:
        self.payload = payload


class _Registry:
    def __init__(self) -> None:
        self.routes: list[_Route] = []

    def get(self, path: str, handler: RouteHandler) -> None:
        self.routes.append(_Route(method="GET", path=path, handler=handler))

    def post(self, path: str, handler: RouteHandler) -> None:
        self.routes.append(_Route(method="POST", path=path, handler=handler))


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    return path if path.startswith("/") else f"/{path}"


class BrowserControlService:
    """In-process browser control service with route dispatch."""

    def __init__(self) -> None:
        self._registry = _Registry()
        self._auth_token = os.getenv("OPENPIPIXIA_BROWSER_CONTROL_TOKEN", "").strip() or None
        mutation_token = os.getenv("OPENPIPIXIA_BROWSER_MUTATION_TOKEN", "").strip() or None
        # Reuse auth token as mutation token by default so enabling auth is enough
        # to protect mutating actions.
        self._mutation_token = mutation_token if mutation_token is not None else self._auth_token
        register_browser_routes(self._registry, get_browser_runtime())

    def dispatch(self, request: BrowserDispatchRequest) -> BrowserDispatchResponse:
        method = (request.method or "").strip().upper()
        path = _normalize_path(request.path or "")
        query = request.query or {}
        body = request.body or {}
        auth_token = (request.auth_token or "").strip()
        mutation_token = (request.mutation_token or "").strip()

        if self._auth_token and auth_token != self._auth_token:
            return BrowserDispatchResponse(status=401, body={"ok": False, "error": "unauthorized"})

        if method in {"POST", "PUT", "PATCH", "DELETE"} and self._mutation_token:
            if mutation_token != self._mutation_token:
                return BrowserDispatchResponse(status=403, body={"ok": False, "error": "forbidden"})

        route = next(
            (entry for entry in self._registry.routes if entry.method == method and entry.path == path),
            None,
        )
        if route is None:
            return BrowserDispatchResponse(status=404, body={"ok": False, "error": "not found"})

        req = _DispatchRequest(method=method, path=path, query=query, body=body)
        res = _DispatchResponse()
        try:
            route.handler(req, res)
        except Exception as exc:  # pragma: no cover - defensive safety net
            return BrowserDispatchResponse(
                status=500,
                body={"ok": False, "error": f"browser service error: {exc}"},
            )
        return BrowserDispatchResponse(
            status=res.status_code,
            body=normalize_profile_payload_aliases(res.payload),
        )


_service: BrowserControlService | None = None


def get_browser_control_service() -> BrowserControlService:
    """Return singleton browser control service."""

    global _service
    if _service is None:
        _service = BrowserControlService()
    return _service


def reset_browser_control_service() -> None:
    """Reset singleton browser control service (used by tests)."""

    global _service
    _service = None
