"""Background command session runtime.

This module provides an in-process session manager inspired by OpenClaw's
``exec + process`` split:

- ``exec`` starts commands and can background them.
- ``process`` style operations can poll/write/log/kill/remove sessions.

The implementation is intentionally lightweight and dependency-free:
- child-process mode uses ``subprocess.Popen`` with pipes;
- PTY mode uses ``os.openpty`` on POSIX and falls back to child mode if PTY
  allocation/spawn fails.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_MAX_OUTPUT_CHARS = 200_000
DEFAULT_PENDING_MAX_OUTPUT_CHARS = 30_000
DEFAULT_FINISHED_TTL_SECONDS = 30 * 60
DEFAULT_POLL_RETRY_BASE_MS = 250
DEFAULT_POLL_RETRY_MAX_MS = 4_000


@dataclass(slots=True)
class SessionSnapshot:
    """Serializable command session state."""

    session_id: str
    command: str
    cwd: str
    scope_key: str | None
    started_at: float
    status: str
    pid: int | None
    backgrounded: bool
    exited: bool
    exit_code: int | None
    exit_signal: int | None
    tail: str
    truncated: bool


@dataclass(slots=True)
class ProcessSession:
    """Mutable process session runtime state."""

    session_id: str
    command: str
    cwd: str
    scope_key: str | None
    started_at: float
    mode: str
    process: subprocess.Popen[bytes]
    write_stdin: Callable[[bytes], None] | None
    close_stdin: Callable[[], None] | None
    cleanup_mode: Callable[[], None] | None
    pty_master_fd: int | None = None
    backgrounded: bool = False
    exited: bool = False
    exit_code: int | None = None
    exit_signal: int | None = None
    termination_reason: str | None = None
    ended_at: float | None = None
    truncated: bool = False
    aggregated: str = ""
    tail: str = ""
    pending_stdout: deque[str] = field(default_factory=deque)
    pending_stderr: deque[str] = field(default_factory=deque)
    pending_stdout_chars: int = 0
    pending_stderr_chars: int = 0
    empty_poll_count: int = 0
    output_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


class ProcessSessionManager:
    """Track running and finished command sessions.

    The manager is thread-safe and uses reader/watcher threads to keep process
    output available for later polling.
    """

    def __init__(
        self,
        *,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        pending_max_output_chars: int = DEFAULT_PENDING_MAX_OUTPUT_CHARS,
        finished_ttl_seconds: int = DEFAULT_FINISHED_TTL_SECONDS,
        poll_retry_base_ms: int = DEFAULT_POLL_RETRY_BASE_MS,
        poll_retry_max_ms: int = DEFAULT_POLL_RETRY_MAX_MS,
    ) -> None:
        self._max_output_chars = max(1_000, int(max_output_chars))
        self._pending_max_output_chars = max(1_000, int(pending_max_output_chars))
        self._finished_ttl_seconds = max(60, int(finished_ttl_seconds))
        self._poll_retry_base_ms = max(50, int(poll_retry_base_ms))
        self._poll_retry_max_ms = max(self._poll_retry_base_ms, int(poll_retry_max_ms))
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}
        self._lock = threading.Lock()

    def start_session(
        self,
        *,
        command: str,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None,
        use_pty: bool,
        scope_key: str | None,
    ) -> tuple[ProcessSession, list[str]]:
        """Spawn a new command session.

        Returns the session plus warnings (for example PTY fallback messages).
        """

        session_id = str(uuid.uuid4())
        warnings: list[str] = []
        spawned = None

        if use_pty:
            spawned = self._spawn_pty(argv=argv, cwd=cwd, env=env)
            if spawned is None:
                warnings.append("Warning: PTY spawn failed; fell back to pipe mode.")

        if spawned is None:
            spawned = self._spawn_child(argv=argv, cwd=cwd, env=env)

        process, mode, write_stdin, close_stdin, cleanup_mode, pty_master_fd = spawned
        session = ProcessSession(
            session_id=session_id,
            command=command,
            cwd=str(cwd),
            scope_key=scope_key,
            started_at=time.time(),
            mode=mode,
            process=process,
            write_stdin=write_stdin,
            close_stdin=close_stdin,
            cleanup_mode=cleanup_mode,
            pty_master_fd=pty_master_fd,
        )

        with self._lock:
            self._running[session_id] = session

        if mode == "pty":
            self._start_pty_reader(session)
        else:
            self._start_pipe_readers(session)
        self._start_exit_watcher(session)

        return session, warnings

    def mark_backgrounded(self, session_id: str, scope_key: str | None = None) -> None:
        """Mark a running session as backgrounded."""

        session = self._lookup(session_id, scope_key=scope_key)
        if session is None:
            return
        with session.lock:
            session.backgrounded = True

    def list_sessions(self, scope_key: str | None = None) -> list[SessionSnapshot]:
        """Return running and finished background sessions."""

        self._prune_finished()
        snapshots: list[SessionSnapshot] = []
        with self._lock:
            running = list(self._running.values())
            finished = list(self._finished.values())

        for session in running + finished:
            with session.lock:
                if not session.backgrounded:
                    continue
                if not self._scope_allows(session.scope_key, scope_key):
                    continue
                snapshots.append(
                    SessionSnapshot(
                        session_id=session.session_id,
                        command=session.command,
                        cwd=session.cwd,
                        scope_key=session.scope_key,
                        started_at=session.started_at,
                        status=self._status_for_session(session),
                        pid=session.process.pid,
                        backgrounded=session.backgrounded,
                        exited=session.exited,
                        exit_code=session.exit_code,
                        exit_signal=session.exit_signal,
                        tail=session.tail,
                        truncated=session.truncated,
                    )
                )

        snapshots.sort(key=lambda item: item.started_at, reverse=True)
        return snapshots

    def poll_session(
        self,
        session_id: str,
        timeout_ms: int = 0,
        scope_key: str | None = None,
    ) -> dict[str, object] | None:
        """Drain pending output and return session state.

        Args:
            session_id: Target session ID.
            timeout_ms: Optional wait time for new output when still running.
        """

        session = self._lookup(session_id, scope_key=scope_key)
        if session is None:
            return None

        wait_ms = max(0, int(timeout_ms))
        should_wait = False
        if wait_ms > 0:
            with session.lock:
                should_wait = (
                    not session.exited
                    and session.pending_stdout_chars == 0
                    and session.pending_stderr_chars == 0
                )
                if should_wait:
                    session.output_event.clear()
            if should_wait:
                session.output_event.wait(wait_ms / 1000.0)

        with session.lock:
            stdout = "".join(session.pending_stdout)
            stderr = "".join(session.pending_stderr)
            session.pending_stdout.clear()
            session.pending_stderr.clear()
            session.pending_stdout_chars = 0
            session.pending_stderr_chars = 0
            has_new_output = bool(stdout or stderr)
            retry_in_ms: int | None = None
            if not session.exited:
                if has_new_output:
                    session.empty_poll_count = 0
                else:
                    session.empty_poll_count = min(session.empty_poll_count + 1, 8)
                    retry_in_ms = min(
                        self._poll_retry_max_ms,
                        self._poll_retry_base_ms * (2 ** (session.empty_poll_count - 1)),
                    )
            else:
                session.empty_poll_count = 0
            status = self._status_for_session(session)
            payload: dict[str, object] = {
                "status": status,
                "stdout": stdout,
                "stderr": stderr,
                "aggregated": session.aggregated,
                "tail": session.tail,
                "exited": session.exited,
                "exit_code": session.exit_code,
                "exit_signal": session.exit_signal,
                "truncated": session.truncated,
                "backgrounded": session.backgrounded,
            }
            if retry_in_ms is not None:
                payload["retry_in_ms"] = retry_in_ms
            return payload

    def log_session(self, session_id: str, scope_key: str | None = None) -> dict[str, object] | None:
        """Return full retained log for a session."""

        session = self._lookup(session_id, scope_key=scope_key)
        if session is None:
            return None
        with session.lock:
            return {
                "status": self._status_for_session(session),
                "aggregated": session.aggregated,
                "tail": session.tail,
                "exited": session.exited,
                "exit_code": session.exit_code,
                "exit_signal": session.exit_signal,
                "truncated": session.truncated,
                "backgrounded": session.backgrounded,
            }

    def write_session(
        self,
        session_id: str,
        data: str,
        *,
        eof: bool = False,
        scope_key: str | None = None,
    ) -> str | None:
        """Write data to session stdin. Returns error message on failure."""

        session = self._lookup(session_id, scope_key=scope_key)
        if session is None:
            return "No session found"

        with session.lock:
            if not session.backgrounded:
                return "Session is not backgrounded"
            if session.exited:
                return "Session already exited"
            writer = session.write_stdin
            closer = session.close_stdin

        if writer is None:
            return "Session stdin is not writable"

        try:
            writer(data.encode("utf-8", errors="replace"))
            if eof and closer is not None:
                closer()
        except Exception as exc:
            return f"Failed to write to session stdin: {exc}"
        return None

    def kill_session(self, session_id: str, scope_key: str | None = None) -> str | None:
        """Terminate a running session. Returns error message on failure."""

        session = self._lookup(session_id, scope_key=scope_key)
        if session is None:
            return "No session found"

        with session.lock:
            if not session.backgrounded:
                return "Session is not backgrounded"
            if session.exited:
                return "Session already exited"

        error = self._terminate_process(session)
        if error:
            return error

        with session.lock:
            session.termination_reason = "killed"

        return None

    def remove_session(self, session_id: str, scope_key: str | None = None) -> bool:
        """Remove a session from manager. Active sessions are terminated first."""

        session = None
        with self._lock:
            running = self._running.get(session_id)
            if running is not None and self._scope_allows(running.scope_key, scope_key):
                session = self._running.pop(session_id, None)
            if session is None:
                finished = self._finished.get(session_id)
                if finished is not None and self._scope_allows(finished.scope_key, scope_key):
                    session = self._finished.pop(session_id, None)

        if session is None:
            return False

        with session.lock:
            if not session.exited:
                self._terminate_process(session)
                session.termination_reason = "removed"

        cleanup = session.cleanup_mode
        if cleanup is not None:
            try:
                cleanup()
            except Exception:
                pass
        return True

    def collect_completed_output(
        self,
        session_id: str,
        scope_key: str | None = None,
    ) -> dict[str, object] | None:
        """Read retained output for a completed session."""

        session = self._lookup(session_id, scope_key=scope_key)
        if session is None:
            return None
        with session.lock:
            if not session.exited:
                return None
            return {
                "status": self._status_for_session(session),
                "stdout": "".join(session.pending_stdout),
                "stderr": "".join(session.pending_stderr),
                "aggregated": session.aggregated,
                "tail": session.tail,
                "exit_code": session.exit_code,
                "exit_signal": session.exit_signal,
                "truncated": session.truncated,
            }

    def _lookup(self, session_id: str, *, scope_key: str | None = None) -> ProcessSession | None:
        self._prune_finished()
        with self._lock:
            session = self._running.get(session_id)
            if session is not None and self._scope_allows(session.scope_key, scope_key):
                return session
            session = self._finished.get(session_id)
            if session is not None and self._scope_allows(session.scope_key, scope_key):
                return session
            return None

    def _append_output(self, session: ProcessSession, stream: str, text: str) -> None:
        if not text:
            return

        with session.lock:
            if stream == "stdout":
                session.pending_stdout.append(text)
                session.pending_stdout_chars += len(text)
                self._trim_pending(
                    session.pending_stdout,
                    pending_chars_name="pending_stdout_chars",
                    session=session,
                )
            else:
                session.pending_stderr.append(text)
                session.pending_stderr_chars += len(text)
                self._trim_pending(
                    session.pending_stderr,
                    pending_chars_name="pending_stderr_chars",
                    session=session,
                )

            candidate = session.aggregated + text
            if len(candidate) > self._max_output_chars:
                session.truncated = True
                candidate = candidate[-self._max_output_chars :]
            session.aggregated = candidate
            session.tail = candidate[-2000:]
            session.output_event.set()

    def _trim_pending(
        self,
        buffer: deque[str],
        *,
        pending_chars_name: str,
        session: ProcessSession,
    ) -> None:
        pending_chars = getattr(session, pending_chars_name)
        while pending_chars > self._pending_max_output_chars and buffer:
            chunk = buffer.popleft()
            pending_chars -= len(chunk)
            session.truncated = True

        if pending_chars > self._pending_max_output_chars and buffer:
            overflow = pending_chars - self._pending_max_output_chars
            head = buffer[0]
            buffer[0] = head[overflow:]
            pending_chars = self._pending_max_output_chars
            session.truncated = True

        setattr(session, pending_chars_name, pending_chars)

    def _start_pipe_readers(self, session: ProcessSession) -> None:
        assert session.process.stdout is not None
        assert session.process.stderr is not None

        def _reader(stream_name: str, source: subprocess.PIPE) -> None:
            try:
                while True:
                    chunk = source.read(4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    self._append_output(session, stream_name, text)
            finally:
                session.output_event.set()

        threading.Thread(
            target=_reader,
            args=("stdout", session.process.stdout),
            daemon=True,
            name=f"openpipixia-{session.session_id[:8]}-stdout",
        ).start()
        threading.Thread(
            target=_reader,
            args=("stderr", session.process.stderr),
            daemon=True,
            name=f"openpipixia-{session.session_id[:8]}-stderr",
        ).start()

    def _start_pty_reader(self, session: ProcessSession) -> None:
        master_fd = session.pty_master_fd
        if master_fd is None:
            return

        def _reader() -> None:
            try:
                while True:
                    chunk = os.read(master_fd, 4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    self._append_output(session, "stdout", text)
            except OSError:
                # PTY can raise OSError when child exits; treat as stream end.
                pass
            finally:
                session.output_event.set()

        threading.Thread(
            target=_reader,
            daemon=True,
            name=f"openpipixia-{session.session_id[:8]}-pty",
        ).start()

    def _start_exit_watcher(self, session: ProcessSession) -> None:
        def _watch() -> None:
            code = session.process.wait()
            with session.lock:
                session.exited = True
                session.exit_code = code
                session.exit_signal = -code if isinstance(code, int) and code < 0 else None
                session.ended_at = time.time()
                session.output_event.set()
                should_archive = session.backgrounded

            if should_archive:
                self._archive_finished(session.session_id)

        threading.Thread(
            target=_watch,
            daemon=True,
            name=f"openpipixia-{session.session_id[:8]}-wait",
        ).start()

    def _archive_finished(self, session_id: str) -> None:
        with self._lock:
            session = self._running.get(session_id)
            if session is None:
                return
            with session.lock:
                if not session.exited:
                    return
            self._finished[session_id] = session
            self._running.pop(session_id, None)

    def _prune_finished(self) -> None:
        cutoff = time.time() - self._finished_ttl_seconds
        stale: list[ProcessSession] = []
        with self._lock:
            stale_ids: list[str] = []
            for session_id, session in self._finished.items():
                ended_at = session.ended_at or session.started_at
                if ended_at < cutoff:
                    stale_ids.append(session_id)
            for session_id in stale_ids:
                session = self._finished.pop(session_id, None)
                if session is not None:
                    stale.append(session)

        for session in stale:
            cleanup = session.cleanup_mode
            if cleanup is None:
                continue
            try:
                cleanup()
            except Exception:
                pass

    def _status_for_session(self, session: ProcessSession) -> str:
        if not session.exited:
            return "running"
        if session.termination_reason == "killed":
            return "killed"
        if session.exit_code == 0:
            return "completed"
        return "failed"

    def _scope_allows(self, session_scope: str | None, requested_scope: str | None) -> bool:
        """Check whether caller scope can access session scope."""

        if requested_scope is None:
            return session_scope is None
        return session_scope == requested_scope

    def _terminate_process(self, session: ProcessSession) -> str | None:
        """Terminate process tree for a session."""

        process = session.process
        try:
            if os.name == "posix" and process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            try:
                process.kill()
            except Exception as exc:
                return f"Failed to terminate session: {exc}"
        return None

    def _spawn_child(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None,
    ) -> tuple[
        subprocess.Popen[bytes],
        str,
        Callable[[bytes], None] | None,
        Callable[[], None] | None,
        Callable[[], None] | None,
        int | None,
    ]:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )

        def _write(data: bytes) -> None:
            if process.stdin is None:
                raise RuntimeError("stdin is unavailable")
            process.stdin.write(data)
            process.stdin.flush()

        def _close() -> None:
            if process.stdin is not None:
                process.stdin.close()

        return process, "child", _write, _close, None, None

    def _spawn_pty(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None,
    ) -> tuple[
        subprocess.Popen[bytes],
        str,
        Callable[[bytes], None] | None,
        Callable[[], None] | None,
        Callable[[], None] | None,
        int | None,
    ] | None:
        if os.name != "posix":
            return None

        try:
            master_fd, slave_fd = os.openpty()
        except OSError:
            return None

        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            return None

        os.close(slave_fd)

        def _write(data: bytes) -> None:
            os.write(master_fd, data)

        def _close() -> None:
            try:
                os.write(master_fd, b"\x04")
            except OSError:
                pass

        def _cleanup() -> None:
            try:
                os.close(master_fd)
            except OSError:
                pass

        return process, "pty", _write, _close, _cleanup, master_fd


_MANAGER: ProcessSessionManager | None = None


def get_process_session_manager() -> ProcessSessionManager:
    """Return the singleton process session manager."""

    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ProcessSessionManager()
    return _MANAGER
