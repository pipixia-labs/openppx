"""Tests for ADK memory service factory."""

from __future__ import annotations

import os
import unittest

from google.adk.memory import InMemoryMemoryService

from openpipixia.runtime.markdown_memory_service import MarkdownMemoryService
from openpipixia.runtime.memory_service import (
    MemoryConfig,
    create_memory_service,
    load_memory_config,
)


class MemoryServiceFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_load_memory_config_defaults_to_enabled_markdown(self) -> None:
        os.environ.pop("OPENPIPIXIA_MEMORY_ENABLED", None)
        os.environ.pop("OPENPIPIXIA_MEMORY_BACKEND", None)
        os.environ.pop("OPENPIPIXIA_WORKSPACE", None)
        os.environ.pop("OPENPIPIXIA_AGENT_HOME", None)

        cfg = load_memory_config()

        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.backend, "markdown")
        self.assertIn(".openpipixia/memory", cfg.markdown_dir)

    def test_load_memory_config_prefers_agent_home_memory_dir_when_available(self) -> None:
        os.environ["OPENPIPIXIA_AGENT_HOME"] = "/tmp/openpipixia-agent-home"
        os.environ.pop("OPENPIPIXIA_MEMORY_MARKDOWN_DIR", None)

        cfg = load_memory_config()

        self.assertEqual(cfg.markdown_dir, "/tmp/openpipixia-agent-home/memory")

    def test_load_memory_config_falls_back_to_data_dir_when_agent_home_is_missing(self) -> None:
        os.environ.pop("OPENPIPIXIA_WORKSPACE", None)
        os.environ.pop("OPENPIPIXIA_AGENT_HOME", None)
        os.environ["OPENPIPIXIA_DATA_DIR"] = "/tmp/openpipixia-agent-a"
        os.environ.pop("OPENPIPIXIA_MEMORY_MARKDOWN_DIR", None)

        cfg = load_memory_config()

        self.assertEqual(cfg.markdown_dir, "/tmp/openpipixia-agent-a/memory")

    def test_create_memory_service_can_be_disabled(self) -> None:
        service = create_memory_service(MemoryConfig(False, "in_memory", ""))
        self.assertIsNone(service)

    def test_create_memory_service_uses_in_memory_when_requested(self) -> None:
        service = create_memory_service(MemoryConfig(True, "in_memory", "/tmp/memory"))
        self.assertIsInstance(service, InMemoryMemoryService)

    def test_create_memory_service_falls_back_to_in_memory_for_unknown_backend(self) -> None:
        service = create_memory_service(
            MemoryConfig(
                enabled=True,
                backend="unknown_backend",
                markdown_dir="/tmp/memory",
            )
        )
        self.assertIsInstance(service, InMemoryMemoryService)

    def test_create_memory_service_uses_markdown_backend(self) -> None:
        service = create_memory_service(
            MemoryConfig(
                enabled=True,
                backend="markdown",
                markdown_dir="/tmp/openpipixia_md_memory",
            )
        )
        self.assertIsInstance(service, MarkdownMemoryService)


if __name__ == "__main__":
    unittest.main()
