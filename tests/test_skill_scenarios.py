"""Scenario-style tests based on available skills."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from openpipixia.tooling.skills_adapter import read_skill
from openpipixia.tooling.registry import read_file, web_fetch, write_file


def _create_simple_docx(path: Path, text: str) -> None:
    """Create a minimal .docx file with one paragraph."""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)


class ScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_saas_ui_page_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPIPIXIA_WORKSPACE"] = tmp
            skill_text = read_skill("ui-ux-pro-max")
            self.assertIn("UI/UX", skill_text)

            html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SaaS Starter</title>
</head>
<body>
  <header><h1>CloudPilot</h1><p>SaaS growth dashboard</p></header>
  <main>
    <section><h2>Features</h2><ul><li>Usage analytics</li><li>Team roles</li></ul></section>
    <section><h2>Pricing</h2><p>Pro plan for startups</p></section>
  </main>
</body>
</html>
"""
            out = write_file("workspace/saas_ui.html", html)
            self.assertIn("Successfully wrote", out)
            content = read_file("workspace/saas_ui.html")
            self.assertIn("CloudPilot", content)
            self.assertIn("SaaS growth dashboard", content)

    def test_weihai_weather_fetch(self) -> None:
        readme = read_skill("weather")
        self.assertIn("weather", readme.lower())

        fake_payload = {
            "latitude": 37.5,
            "longitude": 122.1,
            "current": {"temperature_2m": 6.2, "weather_code": 0},
        }

        class _FakeResponse:
            def __init__(self) -> None:
                self.status = 200
                self.url = "https://api.open-meteo.com/v1/forecast"
                self.headers = {"Content-Type": "application/json"}

            def read(self) -> bytes:
                return json.dumps(fake_payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("openpipixia.tooling.registry.urlopen", return_value=_FakeResponse()):
            data = json.loads(
                web_fetch(
                    "https://api.open-meteo.com/v1/forecast?latitude=37.513&longitude=122.12&current=temperature_2m,weather_code"
                )
            )
        self.assertEqual(data["status"], 200)
        self.assertEqual(data["extractor"], "json")
        self.assertIn("temperature_2m", data["text"])

    def test_birthday_wish_written_to_word_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPIPIXIA_WORKSPACE"] = tmp
            skill_text = read_skill("docx")
            self.assertIn("docx", skill_text.lower())

            bless = "生日快乐，愿你平安健康，万事顺意。"
            output = Path(tmp) / "workspace" / "birthday_wish.docx"
            _create_simple_docx(output, bless)

            self.assertTrue(output.exists())
            with zipfile.ZipFile(output, "r") as zf:
                xml = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("生日快乐", xml)


if __name__ == "__main__":
    unittest.main()
