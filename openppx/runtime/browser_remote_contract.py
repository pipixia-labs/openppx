"""Contract harness for provider-declared browser remote job protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .browser_remote_job_protocol import (
    BrowserRemoteJobProtocolConfig,
    browser_remote_job_protocol_from_payload,
    call_browser_remote_job_cancel,
    call_browser_remote_job_output,
    call_browser_remote_job_pause,
    call_browser_remote_job_resume,
    call_browser_remote_job_status,
    normalize_browser_remote_job_checkpoint_payload,
    normalize_browser_remote_job_snapshot,
)


@dataclass(frozen=True, slots=True)
class BrowserRemoteContractStep:
    """Result of one browser remote provider contract step."""

    name: str
    ok: bool
    skipped: bool = False
    error: str = ""
    payload: Any = None

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable step payload."""
        return {
            "name": self.name,
            "ok": self.ok,
            "skipped": self.skipped,
            "error": self.error,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class BrowserRemoteContractReport:
    """Structured report for a browser remote provider protocol check."""

    ok: bool
    proxy_url: str
    job_id: str
    protocol: dict[str, Any]
    steps: tuple[BrowserRemoteContractStep, ...] = field(default_factory=tuple)

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable report payload."""
        return {
            "ok": self.ok,
            "proxy_url": self.proxy_url,
            "job_id": self.job_id,
            "protocol": self.protocol,
            "steps": [step.to_payload() for step in self.steps],
        }


def run_browser_remote_job_contract(
    *,
    proxy_url: str,
    job_id: str,
    protocol_payload: dict[str, Any],
    token: str = "",
    include_control_steps: bool = False,
    checkpoint_payload: dict[str, Any] | None = None,
) -> BrowserRemoteContractReport:
    """Exercise a provider-declared browser remote job protocol.

    Status, output, and checkpoint normalization are read/validation steps.
    Pause, resume, and cancel are side-effecting provider controls and are
    executed only when ``include_control_steps`` is true.
    """
    protocol = browser_remote_job_protocol_from_payload(protocol_payload)
    if protocol is None:
        return BrowserRemoteContractReport(
            ok=False,
            proxy_url=proxy_url,
            job_id=job_id,
            protocol={},
            steps=(
                BrowserRemoteContractStep(
                    name="parse_protocol",
                    ok=False,
                    error="Browser remote job protocol is not configured.",
                ),
            ),
        )

    steps: list[BrowserRemoteContractStep] = [
        BrowserRemoteContractStep(name="parse_protocol", ok=True, payload=protocol.to_payload())
    ]
    steps.append(_status_step(proxy_url=proxy_url, protocol=protocol, job_id=job_id, token=token))
    steps.append(_output_step(proxy_url=proxy_url, protocol=protocol, job_id=job_id, token=token))
    steps.append(_checkpoint_step(protocol=protocol, checkpoint_payload=checkpoint_payload))

    if include_control_steps:
        steps.append(_pause_step(proxy_url=proxy_url, protocol=protocol, job_id=job_id, token=token))
        steps.append(
            _resume_step(
                proxy_url=proxy_url,
                protocol=protocol,
                job_id=job_id,
                token=token,
                checkpoint_payload=checkpoint_payload,
            )
        )
        steps.append(_cancel_step(proxy_url=proxy_url, protocol=protocol, job_id=job_id, token=token))
    else:
        for name in ("pause", "resume", "cancel"):
            steps.append(
                BrowserRemoteContractStep(
                    name=name,
                    ok=True,
                    skipped=True,
                    payload={"reason": "side-effecting controls disabled"},
                )
            )

    return BrowserRemoteContractReport(
        ok=all(step.ok for step in steps),
        proxy_url=proxy_url,
        job_id=job_id,
        protocol=protocol.to_payload(),
        steps=tuple(steps),
    )


def _status_step(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str,
) -> BrowserRemoteContractStep:
    if not protocol.status_path:
        return BrowserRemoteContractStep(name="status", ok=True, skipped=True)
    result = call_browser_remote_job_status(
        proxy_url=proxy_url,
        protocol=protocol,
        job_id=job_id,
        token=token,
    )
    if not result.ok:
        return BrowserRemoteContractStep(name="status", ok=False, error=result.error, payload=result.raw_payload)
    snapshot = normalize_browser_remote_job_snapshot(result.payload)
    return BrowserRemoteContractStep(name="status", ok=True, payload=snapshot)


def _output_step(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str,
) -> BrowserRemoteContractStep:
    if not protocol.output_path:
        return BrowserRemoteContractStep(name="output", ok=True, skipped=True)
    result = call_browser_remote_job_output(
        proxy_url=proxy_url,
        protocol=protocol,
        job_id=job_id,
        token=token,
    )
    if not result.ok:
        return BrowserRemoteContractStep(name="output", ok=False, error=result.error, payload=result.raw_payload)
    return BrowserRemoteContractStep(name="output", ok=True, payload=result.payload)


def _checkpoint_step(
    *,
    protocol: BrowserRemoteJobProtocolConfig,
    checkpoint_payload: dict[str, Any] | None,
) -> BrowserRemoteContractStep:
    if not checkpoint_payload:
        return BrowserRemoteContractStep(name="checkpoint", ok=True, skipped=True)
    try:
        normalized = normalize_browser_remote_job_checkpoint_payload(
            protocol=protocol,
            payload=checkpoint_payload,
        )
    except ValueError as exc:
        return BrowserRemoteContractStep(name="checkpoint", ok=False, error=str(exc), payload=checkpoint_payload)
    return BrowserRemoteContractStep(name="checkpoint", ok=True, payload=normalized)


def _pause_step(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str,
) -> BrowserRemoteContractStep:
    if not protocol.pause_path:
        return BrowserRemoteContractStep(name="pause", ok=True, skipped=True)
    result = call_browser_remote_job_pause(proxy_url=proxy_url, protocol=protocol, job_id=job_id, token=token)
    return BrowserRemoteContractStep(
        name="pause",
        ok=result.ok,
        error="" if result.ok else result.error,
        payload=result.payload,
    )


def _resume_step(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str,
    checkpoint_payload: dict[str, Any] | None,
) -> BrowserRemoteContractStep:
    if not protocol.resume_path:
        return BrowserRemoteContractStep(name="resume", ok=True, skipped=True)
    result = call_browser_remote_job_resume(
        proxy_url=proxy_url,
        protocol=protocol,
        job_id=job_id,
        token=token,
        checkpoint_payload=checkpoint_payload,
    )
    return BrowserRemoteContractStep(
        name="resume",
        ok=result.ok,
        error="" if result.ok else result.error,
        payload=result.payload,
    )


def _cancel_step(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str,
) -> BrowserRemoteContractStep:
    if not protocol.cancel_path:
        return BrowserRemoteContractStep(name="cancel", ok=True, skipped=True)
    result = call_browser_remote_job_cancel(proxy_url=proxy_url, protocol=protocol, job_id=job_id, token=token)
    return BrowserRemoteContractStep(
        name="cancel",
        ok=result.ok,
        error="" if result.ok else result.error,
        payload=result.payload,
    )


__all__ = [
    "BrowserRemoteContractReport",
    "BrowserRemoteContractStep",
    "run_browser_remote_job_contract",
]
