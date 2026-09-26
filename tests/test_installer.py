"""Tests for the setup helpers (no network, no package installs)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
import installer  # noqa: E402


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"

    def test_update_keeps_other_lines(self):
        self.env.write_text("# my note\nLLM_API_KEY=old\nJOBHUNT_USER_AGENT=custom\n")
        lines, values = installer.read_env(self.env)
        installer.write_env(self.env, lines, {"LLM_API_KEY": "new", "LLM_MODEL": "m"})
        text = self.env.read_text()
        self.assertIn("# my note", text)
        self.assertIn("LLM_API_KEY=new", text)
        self.assertIn("JOBHUNT_USER_AGENT=custom", text)
        self.assertIn("LLM_MODEL=m", text)
        self.assertNotIn("old", text)

    def _configure(self, answers, reconfigure=False):
        with mock.patch.object(installer, "ROOT", self.dir), \
                mock.patch("builtins.input", side_effect=answers), mock.patch.object(installer, "say"):
            installer.configure_llm(interactive=True, reconfigure=reconfigure)
        return installer.read_env(self.env)[1]

    def test_interactive_preset(self):
        values = self._configure(["1", "", "", "secret-key"])
        self.assertEqual(values["LLM_BASE_URL"], "https://api.meta.ai/v1")
        self.assertEqual(values["LLM_MODEL"], "muse-spark-1.3")
        self.assertEqual(values["LLM_API_KEY"], "secret-key")

    def test_existing_settings_kept(self):
        self.env.write_text("LLM_API_KEY=k\nLLM_BASE_URL=b\nLLM_MODEL=m\n")
        values = self._configure([])  # must not prompt
        self.assertEqual(values["LLM_API_KEY"], "k")

    def test_skip(self):
        values = self._configure(["s"])
        self.assertEqual(values.get("LLM_API_KEY"), "")


if __name__ == "__main__":
    unittest.main()
