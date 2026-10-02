"""Tests for the setup helpers (no network, no package installs)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend.llm_providers, to compare the menu with the app
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
        said = []
        with mock.patch.object(installer, "ROOT", self.dir), \
                mock.patch("builtins.input", side_effect=answers), mock.patch.object(installer, "say", side_effect=said.append):
            installer.configure_llm(interactive=True, reconfigure=reconfigure)
        self.said = "\n".join(said)
        return installer.read_env(self.env)[1]

    def _number(self, preset_id):
        return str([p["id"] for p in installer.load_presets()].index(preset_id) + 1)

    def test_menu_offers_every_provider_of_the_app(self):
        from backend.llm_providers import PRESETS
        self._configure(["s"])
        for preset in PRESETS:
            self.assertIn(preset["label"], self.said)
        self.assertEqual([p["id"] for p in installer.load_presets()], [p["id"] for p in PRESETS])

    def test_interactive_preset(self):
        # The address is filled in, the first suggested model is the default, the key is asked for.
        values = self._configure([self._number("meta"), "", "secret-key"])
        self.assertEqual(values["LLM_BASE_URL"], "https://api.meta.ai/v1")
        self.assertEqual(values["LLM_MODEL"], "muse-spark-1.3")
        self.assertEqual(values["LLM_API_KEY"], "secret-key")
        self.assertIn("https://dev.meta.ai/", self.said)

    def test_menu_choice_sets_provider_address_and_model(self):
        for preset_id, base, model in (("openai", "https://api.openai.com/v1", "gpt-6.1-sol"),
                                       ("anthropic", "https://api.anthropic.com", "claude-opus-5-5"),
                                       ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash"),
                                       ("groq", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
                                       ("openrouter", "https://openrouter.ai/api/v1", "anthropic/claude-sonnet-5.5")):
            self.env.unlink(missing_ok=True)
            values = self._configure([self._number(preset_id), "", "k-" + preset_id])
            self.assertEqual((values["LLM_BASE_URL"], values["LLM_MODEL"], values["LLM_API_KEY"]), (base, model, "k-" + preset_id))
        # Another model can be typed, also for a provider with no suggestions.
        self.env.unlink()
        values = self._configure([self._number("mistral"), "mistral-small-latest", "mk"])
        self.assertEqual((values["LLM_BASE_URL"], values["LLM_MODEL"]), ("https://api.mistral.ai/v1", "mistral-small-latest"))

    def test_custom_asks_for_the_address(self):
        values = self._configure([self._number("custom"), "https://llm.example.org/v1/", "my-model", "k"])
        self.assertEqual((values["LLM_BASE_URL"], values["LLM_MODEL"], values["LLM_API_KEY"]), ("https://llm.example.org/v1", "my-model", "k"))
        self.env.unlink()
        values = self._configure(["99", "https://x.example/v1", "m", "k"])  # not on the menu: treated as Custom
        self.assertEqual(values["LLM_BASE_URL"], "https://x.example/v1")

    def test_keyless_provider_is_not_asked_for_a_key(self):
        values = self._configure([self._number("ollama"), "llama3.2:3b"])  # a third question would run out of answers
        self.assertEqual((values["LLM_BASE_URL"], values["LLM_MODEL"], values["LLM_API_KEY"]), ("http://localhost:11434/v1", "llama3.2:3b", ""))
        self.assertIn("No API key is needed", self.said)
        self._configure([])  # configured: Ollama needs no key, so setup doesn't ask again
        self.assertIn("already in .env", self.said)

    def test_reconfigure_keeps_the_saved_model_and_key_for_the_same_provider(self):
        self.env.write_text("LLM_API_KEY=saved-key\nLLM_BASE_URL=https://api.openai.com/v1\nLLM_MODEL=gpt-6-astra\n")
        values = self._configure([self._number("openai"), "", ""], reconfigure=True)
        self.assertEqual((values["LLM_MODEL"], values["LLM_API_KEY"]), ("gpt-6-astra", "saved-key"))
        values = self._configure([self._number("anthropic"), "", ""], reconfigure=True)  # another provider: nothing carried over
        self.assertEqual((values["LLM_MODEL"], values["LLM_API_KEY"]), ("claude-opus-5-5", ""))

    def test_built_in_list_if_the_app_list_cannot_be_read(self):
        with mock.patch.object(installer, "PRESETS_FILE", self.dir / "missing" / "llm_providers.py"):
            presets = installer.load_presets()
            self.assertEqual(presets, installer.FALLBACK_PRESETS)
            self.assertIn("ollama", [p["id"] for p in presets])
            values = self._configure([str([p["id"] for p in presets].index("anthropic") + 1), "", "k"])
        self.assertEqual((values["LLM_BASE_URL"], values["LLM_API_KEY"]), ("https://api.anthropic.com", "k"))
        broken = self.dir / "broken.py"
        broken.write_text("PRESETS = [{'label': 'x'}]\n")  # wrong shape
        with mock.patch.object(installer, "PRESETS_FILE", broken):
            self.assertEqual(installer.load_presets(), installer.FALLBACK_PRESETS)

    def test_non_interactive_takes_the_environment(self):  # the path the CI workflow runs
        env = {"LLM_API_KEY": "test-key", "LLM_BASE_URL": "https://example.invalid/v1", "LLM_MODEL": "test-model"}
        with mock.patch.object(installer, "ROOT", self.dir), mock.patch.dict("os.environ", env), \
                mock.patch.object(installer, "say"), mock.patch("builtins.input", side_effect=AssertionError("no prompts")):
            installer.configure_llm(interactive=False, reconfigure=False)
        self.assertEqual(installer.read_env(self.env)[1], env)

    def test_existing_settings_kept(self):
        self.env.write_text("LLM_API_KEY=k\nLLM_BASE_URL=b\nLLM_MODEL=m\n")
        values = self._configure([])  # must not prompt
        self.assertEqual(values["LLM_API_KEY"], "k")

    def test_skip(self):
        values = self._configure(["s"])
        self.assertEqual(values.get("LLM_API_KEY"), "")

    def test_commute_keys_asked_once(self):
        def run(answers, reconfigure=False):
            with mock.patch.object(installer, "ROOT", self.dir), \
                    mock.patch("builtins.input", side_effect=answers), mock.patch.object(installer, "say"):
                installer.configure_commute(interactive=True, reconfigure=reconfigure)
            return installer.read_env(self.env)[1]
        self.assertEqual(run(["tfnsw-key", ""]), {"TFNSW_API_KEY": "tfnsw-key", "TOMTOM_API_KEY": ""})
        self.assertEqual(run([])["TFNSW_API_KEY"], "tfnsw-key")  # not asked again
        self.assertEqual(run(["", "tt"], reconfigure=True)["TOMTOM_API_KEY"], "tt")


if __name__ == "__main__":
    unittest.main()


import uninstaller  # noqa: E402


class UninstallerTest(unittest.TestCase):
    def _app(self) -> Path:
        root = Path(tempfile.mkdtemp()) / "JobHuntPA"
        (root / "installer").mkdir(parents=True)
        (root / "installer" / "launch.py").write_text("")
        (root / "backend").mkdir()
        (root / "data" / "uploads").mkdir(parents=True)
        (root / "data" / "uploads" / "cv.pdf").write_text("cv")
        (root / ".env").write_text("LLM_API_KEY=k\n")
        return root

    def test_only_real_app_folders(self):
        self.assertTrue(uninstaller.is_app_folder(self._app()))
        self.assertFalse(uninstaller.is_app_folder(Path(tempfile.mkdtemp())))

    def test_refuses_non_app_folder(self):
        other = Path(tempfile.mkdtemp())
        (other / "precious.txt").write_text("keep me")
        with mock.patch.object(sys, "argv", ["uninstaller", "--yes", "--folder", str(other)]), \
                mock.patch.object(uninstaller, "say"):
            self.assertEqual(uninstaller.main(), 1)
        self.assertTrue((other / "precious.txt").exists())

    def test_backup_and_remove(self):
        app = self._app()
        home = Path(tempfile.mkdtemp())
        (home / "Documents").mkdir()
        with mock.patch.object(sys, "argv", ["uninstaller", "--yes", "--folder", str(app)]), \
                mock.patch.object(uninstaller, "say"), mock.patch.object(uninstaller, "running_ports", return_value=[]), \
                mock.patch.object(uninstaller, "playwright_cache", return_value=home / "no-cache"), \
                mock.patch.object(Path, "home", return_value=home):
            self.assertEqual(uninstaller.main(), 0)
        self.assertFalse(app.exists())
        backups = list((home / "Documents").glob("JobHuntPA-backup-*.zip"))
        self.assertEqual(len(backups), 1)
        import zipfile
        self.assertEqual(sorted(zipfile.ZipFile(backups[0]).namelist()), [".env", "data/uploads/cv.pdf"])

    def test_refuses_while_running(self):
        app = self._app()
        with mock.patch.object(sys, "argv", ["uninstaller", "--yes", "--folder", str(app)]), \
                mock.patch.object(uninstaller, "say"), mock.patch.object(uninstaller, "running_ports", return_value=[8000]):
            self.assertEqual(uninstaller.main(), 1)
        self.assertTrue(app.exists())
