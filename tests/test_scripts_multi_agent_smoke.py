"""Tests for scripts/multi_agent_smoke.py entrypoint behavior."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


def _load_module() -> types.ModuleType:
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "multi_agent_smoke.py"
    spec = importlib.util.spec_from_file_location("multi_agent_smoke_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load multi_agent_smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MultiAgentSmokeScriptTests(unittest.TestCase):
    def test_main_returns_zero_on_success_and_cleans_temp_dir(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = str(Path(tmp) / "home")
            mocked_rmtree = Mock()
            def _fake_run(coro):
                coro.close()
                return module.SmokeResult(ok=True, report={"ok": True})
            with patch.object(module.tempfile, "mkdtemp", return_value=temp_home):
                with patch.object(module.asyncio, "run", side_effect=_fake_run):
                    with patch.object(module.shutil, "rmtree", mocked_rmtree):
                        with patch.object(sys, "argv", ["multi_agent_smoke.py"]):
                            with patch("builtins.print"):
                                code = module.main()
        self.assertEqual(code, 0)
        mocked_rmtree.assert_called_once()

    def test_main_returns_one_on_failure_and_keep_temp_skips_cleanup(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = str(Path(tmp) / "home")
            mocked_rmtree = Mock()
            def _fake_run(coro):
                coro.close()
                return module.SmokeResult(ok=False, report={"ok": False})
            with patch.object(module.tempfile, "mkdtemp", return_value=temp_home):
                with patch.object(module.asyncio, "run", side_effect=_fake_run):
                    with patch.object(module.shutil, "rmtree", mocked_rmtree):
                        with patch.object(sys, "argv", ["multi_agent_smoke.py", "--keep-temp"]):
                            with patch("builtins.print"):
                                code = module.main()
        self.assertEqual(code, 1)
        mocked_rmtree.assert_not_called()
