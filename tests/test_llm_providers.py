"""Tests for the LLM providers (backend/llm_providers.py, backend/llm_anthropic.py). No network.

Claude calls are mocked at the SDK boundary (llm_anthropic._client), everything else at httpx.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("JOBHUNT_DB_PATH", os.path.join(_TMP, "test.db"))  # never the real database
os.environ.setdefault("JOBHUNT_SCHEDULER", "off")
os.environ.setdefault("JOBHUNT_ENV_PATH", os.path.join(_TMP, "test.env"))

import anthropic  # noqa: E402
import httpx  # noqa: E402
import httpx2  # noqa: E402  (the SDK's HTTP library: only used here to build SDK error objects)
from anthropic.types import Message  # noqa: E402
from anthropic.types.beta import BetaMessage  # noqa: E402

from backend import llm_anthropic, llm_providers as lp  # noqa: E402
from backend import llm_settings as ls  # noqa: E402
from backend import offices as of  # noqa: E402
from backend import research as rs  # noqa: E402
from backend import scorer as sc  # noqa: E402
from backend.db import init_db  # noqa: E402

init_db()

ROOT = Path(__file__).resolve().parent.parent
AU = {"type": "approximate", "country": "AU"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def cfg_for(base_url: str, model: str = "m", **extra) -> dict:
    return {"api_key": "k", "base_url": base_url, "model": model, "timeout_s": 30, "reasoning_effort": "", **extra}


CLAUDE = cfg_for("https://api.anthropic.com", "claude-opus-5-5")
GEMINI = cfg_for(lp.GEMINI_OPENAI_BASE, "gemini-3.8-flash")
META = cfg_for("https://api.meta.ai/v1", "muse-spark-1.3")


def http_reply(data=None, status=200, text=None, headers=None):
    body = json.dumps(data) if text is None else text
    return mock.Mock(status_code=status, text=body, headers=headers or {}, json=lambda: data)


def chat_reply(content="ok"):
    return http_reply({"choices": [{"message": {"content": content}}]})


def sdk_message(content, stop="end_turn", model="claude-opus-5-5", beta=False, **extra):
    cls = BetaMessage if beta else Message
    return cls.model_validate({"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                               "content": content, "stop_reason": stop, "stop_sequence": None,
                               "usage": {"input_tokens": 1, "output_tokens": 1}, **extra})


def text_block(text, citations=None):
    block = {"type": "text", "text": text}
    if citations is not None:
        block["citations"] = [{"type": "web_search_result_location", "url": u, "title": t, "cited_text": "...",
                               "encrypted_index": "x"} for u, t in citations]
    return block


def search_call(n=1):
    return {"type": "server_tool_use", "id": f"srv_{n}", "name": "web_search", "input": {"query": "acme"}}


def search_result(pages, n=1):
    return {"type": "web_search_tool_result", "tool_use_id": f"srv_{n}", "content": [
        {"type": "web_search_result", "url": u, "title": t, "encrypted_content": "x", "page_age": None}
        for u, t in pages]}


def search_error(code, n=1):
    return {"type": "web_search_tool_result", "tool_use_id": f"srv_{n}",
            "content": {"type": "web_search_tool_result_error", "error_code": code}}


def api_error(cls, message="boom"):
    status = {anthropic.BadRequestError: 400, anthropic.AuthenticationError: 401, anthropic.RateLimitError: 429,
              anthropic.NotFoundError: 404}[cls]
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, json={"type": "error", "error": {"type": "e", "message": message}})
    return cls(message, response=response, body=None)


class FakeClaude:
    """Stands in for anthropic.Anthropic: replies in order; each reply is a message or an exception to raise."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []
        self.messages = SimpleNamespace(create=lambda **kw: self._call("messages", kw))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: self._call("beta", kw)))

    def _call(self, endpoint, kwargs):
        self.calls.append((endpoint, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def claude(*replies):
    fake = FakeClaude(*replies)
    return fake, mock.patch.object(llm_anthropic, "_client", return_value=fake)


def no_http():
    """Claude never touches httpx (that would be the OpenAI-compatible endpoint)."""
    return mock.patch.object(httpx, "post", side_effect=AssertionError("httpx must not be used for Claude"))


class ProviderTestCase(unittest.TestCase):
    def setUp(self):
        lp._rejected.clear()
        llm_anthropic._clients.clear()


# --------------------------------------------------------------------------
# presets and host dispatch
# --------------------------------------------------------------------------

class PresetsTest(ProviderTestCase):
    def test_provider_and_style_from_the_host(self):
        cases = {
            "https://api.openai.com/v1": ("openai", "openai"),
            "HTTPS://API.OPENAI.COM/v1/": ("openai", "openai"),
            "https://api.anthropic.com": ("anthropic", "anthropic"),
            "https://api.anthropic.com/v1": ("anthropic", "anthropic"),
            "https://generativelanguage.googleapis.com/v1beta/openai": ("gemini", "gemini"),
            "https://generativelanguage.googleapis.com": ("gemini", "gemini"),
            "https://api.meta.ai/v1": ("meta", "openai"),
            "https://api.mistral.ai/v1": ("mistral", "openai"),
            "https://api.groq.com/openai/v1": ("groq", "openai"),
            "https://api.x.ai/v1": ("xai", "openai"),
            "https://api.deepseek.com": ("deepseek", "openai"),
            "https://openrouter.ai/api/v1": ("openrouter", "openai"),
            "http://localhost:11434/v1": ("ollama", "openai"),
            "http://127.0.0.1:11434": ("ollama", "openai"),
            "http://localhost:1234/v1": ("custom", "openai"),
            "https://llm.example/v1": ("custom", "openai"),
            "https://api.anthropic.com.evil.example/v1": ("custom", "openai"),
            "": ("custom", "openai"),
        }
        for url, (provider, style) in cases.items():
            self.assertEqual((lp.provider_for(url), lp.style_for(url)), (provider, style), url)

    def test_preset_shapes(self):
        ids = [p["id"] for p in lp.PRESETS]
        self.assertEqual(len(ids), len(set(ids)))
        for wanted in ("openai", "anthropic", "gemini", "meta", "mistral", "groq", "xai", "deepseek", "openrouter",
                       "ollama", "custom"):
            self.assertIn(wanted, ids)
        for p in lp.PRESETS:
            self.assertEqual(set(p), {"id", "label", "base_url", "models", "key_url", "web_search", "key_required", "note"})
            self.assertIsInstance(p["models"], list)
            self.assertEqual(lp.capabilities(cfg_for(p["base_url"]))["web_search"], p["web_search"], p["id"])
            if p["id"] != "custom":
                self.assertEqual(lp.provider_for(p["base_url"]), p["id"])
        by_id = {p["id"]: p for p in lp.PRESETS}
        self.assertEqual(by_id["anthropic"]["models"],
                         ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-fable-5-1"])
        self.assertEqual(by_id["anthropic"]["key_url"], "https://platform.claude.com/settings/keys")
        self.assertEqual(by_id["meta"]["base_url"], "https://api.meta.ai/v1")
        self.assertEqual(by_id["ollama"]["base_url"], "http://localhost:11434/v1")
        self.assertFalse(by_id["ollama"]["key_required"])
        self.assertEqual({i for i, p in by_id.items() if not p["web_search"]}, {"mistral", "groq", "deepseek", "ollama"})
        for p in lp.PRESETS:
            for model in p["models"]:
                self.assertNotRegex(model, r"-\d{8}$", "no date suffixes")

    def test_capabilities(self):
        caps = lp.capabilities(CLAUDE)
        self.assertEqual((caps["provider"], caps["style"], caps["web_search"], caps["key_required"]),
                         ("anthropic", "anthropic", True, True))
        self.assertEqual(caps["reasoning_levels"], ["low", "medium", "high", "xhigh", "max"])
        caps = lp.capabilities(cfg_for("http://localhost:11434/v1"))
        self.assertEqual((caps["provider"], caps["web_search"], caps["key_required"]), ("ollama", False, False))
        self.assertEqual(lp.capabilities(META)["reasoning_levels"], ["none", "minimal", "low", "medium", "high", "xhigh"])

    def test_requirement_declared_and_one_error_class(self):
        self.assertIn("anthropic>=1.0,<2", (ROOT / "requirements.txt").read_text().splitlines())
        self.assertIs(sc.ScorerError, lp.ScorerError)

    def test_presets_can_be_read_without_the_apps_packages(self):
        """The installer imports them before pip has installed anything."""
        code = ("import sys; sys.modules['httpx'] = None; sys.modules['anthropic'] = None; "
                "from backend.llm_providers import PRESETS, provider_for, capabilities; "
                "print(len(PRESETS), provider_for('https://api.anthropic.com'), capabilities({'base_url': 'http://localhost:11434/v1'})['key_required'])")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.stdout.split(), ["11", "anthropic", "False"], done.stderr)

    def test_ollama_needs_no_key_others_do(self):
        env = {"LLM_API_KEY": "", "LLM_BASE_URL": "http://localhost:11434/v1", "LLM_MODEL": "llama3"}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(sc.config_problem(sc.get_config()))
            sent = []
            with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append(kw) or chat_reply()):
                sc.call_model([{"role": "user", "content": "x"}], sc.get_config())
            self.assertEqual(sent[0]["headers"]["Authorization"], "Bearer ollama")
        with mock.patch.dict(os.environ, {**env, "LLM_BASE_URL": "https://api.openai.com/v1"}):
            self.assertEqual(sc.config_problem(sc.get_config()), "Set LLM_API_KEY in .env and restart the server.")


# --------------------------------------------------------------------------
# OpenAI-compatible hosts: the requests are exactly what they were
# --------------------------------------------------------------------------

def legacy_search_payload(model, instructions, input_, size, effort=""):
    """What research.py, offices.py and job_chat.py each built before providers existed."""
    payload = {"model": model, "instructions": instructions, "input": input_,
               "tools": [{"type": "web_search", "search_context_size": size,
                          "user_location": {"type": "approximate", "country": "AU"}}],
               "include": ["web_search_call.results"], "background": True}
    if effort:
        payload["reasoning"] = {"effort": effort}
    return payload


def completed(text='{"offices": []}'):
    return http_reply({"id": "resp_1", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": []}]}]})


class OpenAICompatibleRegressionTest(ProviderTestCase):
    def test_chat_request_is_unchanged(self):
        sent = []
        for base in ("https://api.meta.ai/v1", "https://llm.example/v1", "https://api.openai.com/v1"):
            with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append((url, kw)) or chat_reply("hi")):
                out = sc.call_model([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
                                    cfg_for(base, "mod", reasoning_effort="low"))
            self.assertEqual(out, "hi")
            url, kw = sent[-1]
            self.assertEqual(url, base + "/chat/completions")
            self.assertEqual(json.dumps(kw["json"]), json.dumps({
                "model": "mod", "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
                "response_format": {"type": "json_object"}, "reasoning_effort": "low"}))
            self.assertEqual(kw["headers"], {"Authorization": "Bearer k", "Content-Type": "application/json"})
            self.assertEqual(kw["timeout"], 30)

    def test_responses_request_is_unchanged_for_each_call_site(self):
        cfg = cfg_for("https://api.meta.ai/v1", "muse-spark-1.3", reasoning_effort="low")
        sent = []

        def post(url, **kw):
            sent.append((url, kw))
            return completed()
        company = {"name": "Acme", "key": "acme", "jobs": [{"title": "PM", "location": "Sydney", "url": "https://x.example/1"}]}
        turns = [{"role": "user", "content": "Any news?"}]
        with mock.patch.object(httpx, "post", side_effect=post):
            rs.run_agent(company, cfg)                                                        # company research
            of.research_offices("Acme", "Sydney", cfg)                                        # office finding
            lp.web_search(cfg, instructions="CONTEXT", input=turns, context_size="medium")    # the chat
        research_input = "Organisation: Acme\nJob ads we have from it (for disambiguation):\n- PM (Sydney) https://x.example/1"
        expected = [
            legacy_search_payload("muse-spark-1.3", rs.INSTRUCTIONS, research_input, "medium", "low"),
            legacy_search_payload("muse-spark-1.3", of.OFFICES_PROMPT, "Organisation: Acme\nCity: Sydney, Australia", "low", "low"),
            legacy_search_payload("muse-spark-1.3", "CONTEXT", turns, "medium", "low"),
        ]
        for (url, kw), want in zip(sent, expected):
            self.assertEqual(url, "https://api.meta.ai/v1/responses")
            self.assertEqual(json.dumps(kw["json"]), json.dumps(want))
            self.assertEqual(kw["headers"], {"Authorization": "Bearer k", "Content-Type": "application/json"})
            self.assertEqual(kw["timeout"], 120)
        self.assertEqual(len(sent), 3)

    def test_no_reasoning_when_no_level_and_openai_also_lists_its_sources(self):
        sent = []
        with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append(kw["json"]) or completed()):
            lp.web_search(cfg_for("https://llm.example/v1", "m"), instructions="I", input="Q")
            lp.web_search(cfg_for("https://api.openai.com/v1", "gpt-6.1-sol"), instructions="I", input="Q")
        self.assertEqual(sent[0], legacy_search_payload("m", "I", "Q", "medium"))
        self.assertNotIn("reasoning", sent[1])
        self.assertEqual(sent[1]["include"], ["web_search_call.results", "web_search_call.action.sources"])

    def test_openai_sources_become_visible_to_the_link_check(self):
        response = {"status": "completed", "output": [
            {"type": "web_search_call", "action": {"type": "search", "sources": [
                {"type": "url", "url": "https://acme.example/contact"}]}, "results": None},
            {"type": "message", "content": [{"type": "output_text", "text": "{}", "annotations": []}]}]}
        with mock.patch.object(httpx, "post", return_value=http_reply(response)):
            out = lp.web_search(cfg_for("https://api.openai.com/v1", "gpt-6.1-sol"), instructions="I", input="Q")
        _, seen = rs.extract(out)
        self.assertIn("https://acme.example/contact", seen)

    def test_providers_that_reject_response_format_or_reasoning_effort_are_handled(self):
        cfg = cfg_for("https://llm.example/v1", "picky", reasoning_effort="low")
        sent = []
        replies = [http_reply(status=400, text='{"error": "Unknown parameter: response_format"}'),
                   http_reply(status=400, text='{"error": "reasoning_effort is not supported with this model"}'),
                   chat_reply("done"), chat_reply("again")]
        with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append(dict(kw["json"])) or replies.pop(0)):
            self.assertEqual(sc.call_model([{"role": "user", "content": "x"}], cfg), "done")
            self.assertEqual(sc.call_model([{"role": "user", "content": "x"}], cfg), "again")
        self.assertEqual([sorted(p) for p in sent[:3]],
                         [["messages", "model", "reasoning_effort", "response_format"],
                          ["messages", "model", "reasoning_effort"], ["messages", "model"]])
        self.assertEqual(sorted(sent[3]), ["messages", "model"])  # remembered: not sent again

    def test_other_400s_still_fail(self):
        with mock.patch.object(httpx, "post", return_value=http_reply(status=400, text="context too long")):
            with self.assertRaisesRegex(sc.ScorerError, "HTTP 400: context too long"):
                sc.call_model([{"role": "user", "content": "x"}], cfg_for("https://llm.example/v1"))

    def test_probing_levels_sends_the_same_request(self):
        sent = []

        def post(url, **kw):
            sent.append((url, kw))
            return mock.Mock(status_code=200 if kw["json"]["reasoning_effort"] in ("low", "high") else 400, text="no")
        cfg = cfg_for("https://llm.example/v1", "m1")
        with mock.patch.object(httpx, "post", side_effect=post), \
                mock.patch.object(ls, "_get", return_value={}), mock.patch.object(ls, "_set") as save:
            out = ls.detect_levels(cfg)
        self.assertEqual(out["levels"], ["low", "high"])
        self.assertEqual(out["rejected"]["medium"], "HTTP 400: no")
        url, kw = sent[0]
        self.assertEqual(url, "https://llm.example/v1/chat/completions")
        self.assertEqual(json.dumps(kw["json"]), json.dumps({
            "model": "m1", "reasoning_effort": "none", "messages": [{"role": "user", "content": "Reply with the single word OK."}]}))
        self.assertEqual(kw["timeout"], 90)
        save.assert_called_once()


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

class ClaudeChatTest(ProviderTestCase):
    def test_request_shape_and_reply(self):
        fake, patch = claude(sdk_message([text_block("hello "), {"type": "thinking", "thinking": "", "signature": "s"},
                                          text_block("there")], beta=True))
        with patch, no_http():
            out = sc.call_model([{"role": "system", "content": "SYS"}, {"role": "user", "content": "U1"},
                                 {"role": "assistant", "content": "A1"}, {"role": "user", "content": "U2"}], CLAUDE)
        self.assertEqual(out, "hello there")  # only text blocks
        endpoint, kw = fake.calls[0]
        self.assertEqual(endpoint, "beta")
        self.assertEqual(kw, {"model": "claude-opus-5-5", "max_tokens": 16000, "system": "SYS",
                              "messages": [{"role": "user", "content": "U1"}, {"role": "assistant", "content": "A1"},
                                           {"role": "user", "content": "U2"}],
                              "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"})

    def test_client_uses_the_sdk_default_base_url_and_the_app_timeout(self):
        with mock.patch.object(llm_anthropic.anthropic, "Anthropic") as factory:
            first = llm_anthropic._client({**CLAUDE, "timeout_s": 45})
            again = llm_anthropic._client({**CLAUDE, "timeout_s": 45})
        factory.assert_called_once_with(api_key="k", timeout=45.0)  # no base_url: the OpenAI-style /v1 isn't passed
        self.assertIs(first, again)

    def test_effort_only_when_a_level_is_set(self):
        for level, sent in (("", None), ("none", None), ("minimal", None), ("low", "low"), ("medium", "medium"),
                            ("high", "high"), ("xhigh", "xhigh"), ("max", "max")):
            fake, patch = claude(sdk_message([text_block("x")], beta=True))
            with patch, no_http():
                lp.chat([{"role": "user", "content": "U"}], {**CLAUDE, "reasoning_effort": level})
            kw = fake.calls[0][1]
            self.assertEqual(kw.get("output_config"), {"effort": sent} if sent else None, level)
            self.assertNotIn("thinking", kw)  # budget_tokens and "disabled" are 400s on current models

    def test_refusal_fallback_only_for_the_listed_models(self):
        for model, beta in (("claude-fable-5-1", True), ("claude-opus-5-5", True), ("claude-opus-5", True),
                            ("claude-sonnet-5-5", True), ("claude-haiku-4-5", False), ("claude-sonnet-4-6", False),
                            ("claude-opus-4-8", False), ("claude-fable-5", False)):
            fake, patch = claude(sdk_message([text_block("x")], beta=beta))
            with patch, no_http():
                lp.chat([{"role": "user", "content": "U"}], {**CLAUDE, "model": model})
            endpoint, kw = fake.calls[0]
            self.assertEqual(endpoint, "beta" if beta else "messages", model)
            self.assertEqual(kw.get("betas"), ["server-side-fallback-2026-07-01"] if beta else None, model)
            self.assertEqual(kw.get("fallbacks"), "default" if beta else None, model)

    def test_refusal_and_truncation_are_errors(self):
        refused = sdk_message([], stop="refusal", beta=True,
                              stop_details={"type": "refusal", "category": "cyber", "explanation": "no exploits"})
        fake, patch = claude(refused)
        with patch, no_http(), self.assertRaises(sc.ScorerError) as ctx:
            lp.chat([{"role": "user", "content": "U"}], CLAUDE)
        self.assertIn("declined", str(ctx.exception))
        self.assertIn("cyber", str(ctx.exception))
        self.assertIn("no exploits", str(ctx.exception))
        self.assertFalse(ctx.exception.auth)
        fake, patch = claude(sdk_message([text_block("{\"score\": 7")], stop="max_tokens", beta=True))
        with patch, no_http(), self.assertRaisesRegex(sc.ScorerError, "cut off"):
            lp.chat([{"role": "user", "content": "U"}], CLAUDE)

    def test_a_model_without_effort_is_retried_without_it_and_remembered(self):
        cfg = {**CLAUDE, "model": "claude-haiku-4-5", "reasoning_effort": "low"}
        fake, patch = claude(api_error(anthropic.BadRequestError, "effort: this model does not support effort"),
                             sdk_message([text_block("a")]), sdk_message([text_block("b")]))
        with patch, no_http():
            self.assertEqual(lp.chat([{"role": "user", "content": "U"}], cfg), "a")
            self.assertEqual(lp.chat([{"role": "user", "content": "U"}], cfg), "b")
        self.assertEqual(["output_config" in kw for _, kw in fake.calls], [True, False, False])
        fake, patch = claude(api_error(anthropic.BadRequestError, "messages: text content blocks must be non-empty"))
        with patch, no_http(), self.assertRaisesRegex(sc.ScorerError, "HTTP 400: messages"):
            lp.chat([{"role": "user", "content": "U"}], {**CLAUDE, "model": "claude-sonnet-4-6"})

    def test_errors_become_scorer_errors(self):
        cases = [
            (api_error(anthropic.AuthenticationError, "invalid x-api-key"), True, "API key rejected"),
            (api_error(anthropic.RateLimitError, "slow down"), False, "HTTP 429"),
            (api_error(anthropic.BadRequestError, "Your credit balance is too low to access the API"), True, "credit balance"),
            (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
             False, "connection error"),
        ]
        for error, auth, text in cases:
            fake, patch = claude(error)
            with patch, no_http(), self.assertRaisesRegex(sc.ScorerError, text) as ctx:
                lp.chat([{"role": "user", "content": "U"}], {**CLAUDE, "model": "claude-haiku-4-5"})
            self.assertEqual(ctx.exception.auth, auth, text)

    def test_reasoning_level_detection_probes_the_five_efforts(self):
        def reply(level):
            return api_error(anthropic.BadRequestError, "effort not supported") if level in ("xhigh", "max") else sdk_message([text_block("OK")], beta=True)
        fake, patch = claude(*[reply(level) for level in ("low", "medium", "high", "xhigh", "max")])
        with patch, no_http(), mock.patch.object(ls, "_get", return_value={}), mock.patch.object(ls, "_set") as save:
            out = ls.detect_levels(CLAUDE)
        self.assertEqual(out["levels"], ["low", "medium", "high"])
        self.assertEqual(sorted(out["rejected"]), ["max", "xhigh"])
        self.assertEqual([kw["output_config"]["effort"] for _, kw in fake.calls], ["low", "medium", "high", "xhigh", "max"])
        self.assertTrue(all(kw["max_tokens"] <= 1024 for _, kw in fake.calls))
        self.assertEqual(save.call_args.args[1]["https://api.anthropic.com|claude-opus-5-5"], ["low", "medium", "high"])
        # Haiku 4.5 takes no effort at all.
        fake, patch = claude(*[api_error(anthropic.BadRequestError, "effort not supported")] * 5)
        with patch, no_http(), mock.patch.object(ls, "_get", return_value={}), mock.patch.object(ls, "_set"):
            self.assertEqual(ls.detect_levels({**CLAUDE, "model": "claude-haiku-4-5"})["levels"], [])
        fake, patch = claude(api_error(anthropic.AuthenticationError, "bad key"))
        with patch, no_http(), self.assertRaises(ls.HTTPException) as ctx:
            ls.detect_levels(CLAUDE)
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (502, "API key rejected (HTTP 401)."))

    def test_max_is_a_level_the_settings_accept(self):
        with mock.patch.object(ls, "_get", return_value=None), mock.patch.object(ls, "_set") as save:
            ls.update_reasoning(ls.ReasoningUpdate(scoring="max"))
            self.assertEqual(save.call_args.args[1]["scoring"], "max")
            with self.assertRaises(ls.HTTPException):
                ls.update_reasoning(ls.ReasoningUpdate(scoring="bogus"))

    def test_the_connection_test_goes_through_the_sdk(self):
        fake, patch = claude(sdk_message([text_block('{"ok": true}')], beta=True))
        env = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://api.anthropic.com/v1", "LLM_MODEL": "claude-opus-5-5"}
        with patch, no_http(), mock.patch.dict(os.environ, env):
            out = sc.test_connection()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["provider"], "anthropic")
        self.assertEqual(fake.calls[0][1]["messages"], [{"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}])


class ClaudeOnTheWireTest(ProviderTestCase):
    """The real SDK, with its HTTP transport replaced: what actually goes to api.anthropic.com."""

    def test_requests_as_the_sdk_sends_them(self):
        seen = []

        def handler(request):
            seen.append(request)
            body = json.loads(request.content)
            if len(seen) == 1:
                content, stop = [text_block("Searching."), search_call()], "pause_turn"
            else:
                content, stop = [search_result(ClaudeWebSearchTest.PAGES), text_block('{"ok": true}', citations=[ClaudeWebSearchTest.PAGES[0]])], "end_turn"
            return httpx2.Response(200, json={"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                              "content": content, "stop_reason": stop, "stop_sequence": None,
                                              "usage": {"input_tokens": 1, "output_tokens": 1}})
        client = anthropic.Anthropic(api_key="k", timeout=30, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
        llm_anthropic._clients[("k", 300.0)] = client  # web searches get at least 300 s
        self.addCleanup(llm_anthropic._clients.clear)
        with no_http():
            out = lp.web_search({**CLAUDE, "reasoning_effort": "high"}, instructions="SYS", input="Q")
        first, second = seen
        self.assertEqual(str(first.url), "https://api.anthropic.com/v1/messages?beta=true")  # the SDK's own base URL
        self.assertEqual(first.headers["x-api-key"], "k")
        self.assertEqual(first.headers["anthropic-beta"], "server-side-fallback-2026-07-01")
        body = json.loads(first.content)
        self.assertEqual((body["fallbacks"], body["output_config"], body["max_tokens"], body["system"]), ("default", {"effort": "high"}, 16000, "SYS"))
        self.assertNotIn("thinking", body)
        self.assertEqual(body["tools"], [{"type": "web_search_20260209", "name": "web_search", "max_uses": 10, "user_location": AU}])
        resumed = json.loads(second.content)["messages"]
        self.assertEqual([m["role"] for m in resumed], ["user", "assistant"])  # no "continue" message
        self.assertEqual([b["type"] for b in resumed[1]["content"]], ["text", "server_tool_use"])
        self.assertEqual(rs.extract(out)[0], '{"ok": true}')

    def test_models_without_the_refusal_fallback_use_the_plain_endpoint(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
                                              "content": [text_block("hi")], "stop_reason": "end_turn", "stop_sequence": None,
                                              "usage": {"input_tokens": 1, "output_tokens": 1}})
        llm_anthropic._clients[("k", 30.0)] = anthropic.Anthropic(
            api_key="k", timeout=30, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
        self.addCleanup(llm_anthropic._clients.clear)
        with no_http():
            self.assertEqual(sc.call_model([{"role": "user", "content": "U"}], {**CLAUDE, "model": "claude-haiku-4-5"}), "hi")
        self.assertEqual(str(seen[0].url), "https://api.anthropic.com/v1/messages")
        self.assertNotIn("anthropic-beta", seen[0].headers)
        self.assertNotIn("fallbacks", json.loads(seen[0].content))


class ClaudeWebSearchTest(ProviderTestCase):
    PAGES = [("https://www.acme.example/about", "About Acme"), ("https://news.example/acme-fine", "Acme fined")]
    ANSWER = json.dumps({"official_name": "Acme", "is_company": True, "controversies": [
        {"title": "Fine", "year": "2024", "summary": "Fined.", "sources": [{"title": "n", "url": "https://news.example/acme-fine/"}]},
        {"title": "Made up", "year": "2023", "summary": "x", "sources": [{"title": "f", "url": "https://made-up.example/story"}]}],
        "sources": [{"title": "About", "url": "https://acme.example/about"}]})

    def search(self, *replies, cfg=CLAUDE, input="Organisation: Acme", context="medium"):
        fake, patch = claude(*replies)
        with patch, no_http():
            out = lp.web_search(cfg, instructions="INSTRUCTIONS", input=input, context_size=context, what="research agent",
                                feature="Company research")
        return fake, out

    def test_sources_come_from_the_result_blocks(self):
        reply = sdk_message([text_block("Let me search for Acme {not the answer}."), search_call(),
                             search_result(self.PAGES), text_block(self.ANSWER, citations=[self.PAGES[1]])], beta=True)
        fake, out = self.search(reply)
        endpoint, kw = fake.calls[0]
        self.assertEqual(endpoint, "beta")
        self.assertEqual(kw["system"], "INSTRUCTIONS")
        self.assertEqual(kw["messages"], [{"role": "user", "content": "Organisation: Acme"}])
        self.assertEqual(kw["tools"], [{"type": "web_search_20260209", "name": "web_search", "max_uses": 10, "user_location": AU}])
        text, seen = rs.extract(out)
        self.assertEqual(text, self.ANSWER)  # not the narration before the search
        self.assertEqual(set(seen), {"https://acme.example/about", "https://news.example/acme-fine"})
        message = out["output"][1]["content"][0]
        self.assertEqual(message["annotations"], [{"type": "url_citation", "url": self.PAGES[1][0], "title": "Acme fined"}])
        profile, dropped = rs.parse_profile(text, seen)  # the app's link check, unchanged
        self.assertEqual([c["title"] for c in profile["controversies"]], ["Fine"])
        self.assertEqual(dropped, 1)

    def test_older_models_use_the_basic_search_tool(self):
        for model, tool in (("claude-haiku-4-5", "web_search_20250305"), ("claude-sonnet-4-5", "web_search_20250305"),
                            ("claude-opus-5-5", "web_search_20260209"), ("claude-opus-4-6", "web_search_20260209"),
                            ("claude-sonnet-5-5", "web_search_20260209"), ("claude-sonnet-4-6", "web_search_20260209"),
                            ("claude-fable-5-1", "web_search_20260209")):
            self.assertEqual(llm_anthropic.search_tool_type(model), tool, model)
        fake, _ = self.search(sdk_message([search_call(), search_result(self.PAGES), text_block("{}")]),
                              cfg={**CLAUDE, "model": "claude-haiku-4-5"}, context="low")
        endpoint, kw = fake.calls[0]
        self.assertEqual(endpoint, "messages")
        self.assertEqual(kw["tools"][0]["type"], "web_search_20250305")
        self.assertEqual(kw["tools"][0]["max_uses"], 5)

    def test_a_search_error_is_an_error_object_not_a_list(self):
        # Every search failed: the answer is ungrounded, so it's an error the chat retries once.
        with self.assertRaisesRegex(sc.ScorerError, r"research agent ended with status failed: web search error \(max_uses_exceeded\)"):
            self.search(sdk_message([search_call(), search_error("max_uses_exceeded"), text_block("{}")]))
        # One search worked, another failed: use what was found.
        _, out = self.search(sdk_message([search_call(1), search_error("unavailable", 1), search_call(2),
                                          search_result(self.PAGES[:1], 2), text_block("{}")]))
        self.assertEqual(out["output"][0]["results"], [{"url": self.PAGES[0][0], "title": "About Acme"}])

    def test_pause_turn_resumes_with_the_paused_turn_and_no_extra_message(self):
        paused = sdk_message([text_block("Searching."), search_call()], stop="pause_turn")
        done = sdk_message([search_result(self.PAGES), text_block("{}")])
        fake, out = self.search(paused, done)
        self.assertEqual(len(fake.calls), 2)
        first, second = fake.calls[0][1], fake.calls[1][1]
        self.assertEqual(second["messages"], [{"role": "user", "content": "Organisation: Acme"},
                                              {"role": "assistant", "content": list(paused.content)}])
        self.assertEqual({k: v for k, v in second.items() if k != "messages"}, {k: v for k, v in first.items() if k != "messages"})
        self.assertEqual(len(out["output"][0]["results"]), 2)  # results from the resumed part count

    def test_a_search_that_never_finishes_stops_after_five_continuations(self):
        paused = [sdk_message([search_call(i)], stop="pause_turn") for i in range(1, 7)]
        fake, patch = claude(*paused)
        with patch, no_http(), self.assertRaisesRegex(sc.ScorerError, "no answer"):
            lp.web_search(CLAUDE, instructions="I", input="Q")
        self.assertEqual(len(fake.calls), 1 + llm_anthropic.MAX_CONTINUATIONS)
        last = fake.calls[-1][1]["messages"]
        self.assertEqual([m["role"] for m in last], ["user", "assistant"])
        self.assertEqual(len(last[1]["content"]), 5)  # every paused part is carried along

    def test_after_a_fallback_the_echoed_turn_drops_what_only_the_first_model_could_read(self):
        def block(kind, **kw):
            return SimpleNamespace(type=kind, **kw)
        blocks = [block("thinking"), block("server_tool_use", id="a"), block("server_tool_use", id="b"),
                  block("web_search_tool_result", tool_use_id="a"), block("text", text="t"), block("fallback"),
                  block("thinking"), block("text", text="after")]
        kept = llm_anthropic._echo(blocks)
        self.assertEqual([(b.type, getattr(b, "id", None)) for b in kept],
                         [("server_tool_use", "a"), ("web_search_tool_result", None), ("text", None), ("fallback", None),
                          ("thinking", None), ("text", None)])
        self.assertEqual(llm_anthropic._echo(blocks[:5]), blocks[:5])  # no fallback: unchanged

    def test_the_chat_sends_its_turns_and_a_refusal_is_reported(self):
        turns = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]
        fake, _ = self.search(sdk_message([search_call(), search_result(self.PAGES), text_block("Answer.")]), input=turns)
        self.assertEqual(fake.calls[0][1]["messages"], turns)
        with self.assertRaisesRegex(sc.ScorerError, "declined"):
            self.search(sdk_message([], stop="refusal", stop_details={"type": "refusal", "category": "bio", "explanation": None}))


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"


def grounded(text="The answer.", **meta):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP",
                            "groundingMetadata": {
                                "webSearchQueries": ["acme"],
                                "groundingChunks": [{"web": {"uri": REDIRECT + "AAA", "title": "acme.example"}},
                                                    {"web": {"uri": "https://plain.example/page", "title": "Plain page"}},
                                                    {"web": {"uri": REDIRECT + "BBB", "title": "news.example"}}],
                                "groundingSupports": [{"segment": {"text": "x"}, "groundingChunkIndices": [0, 2]}],
                                **meta}}]}


class GeminiTest(ProviderTestCase):
    def test_chat_goes_through_the_openai_compatible_endpoint(self):
        sent = []
        with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append((url, kw)) or chat_reply("hi")):
            out = sc.call_model([{"role": "user", "content": "U"}], {**GEMINI, "reasoning_effort": "low"})
        self.assertEqual(out, "hi")
        url, kw = sent[0]
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer k")
        self.assertEqual(kw["json"], {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "U"}],
                                      "response_format": {"type": "json_object"}, "reasoning_effort": "low"})
        # a host typed without the /v1beta/openai path still reaches the right endpoint
        with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append((url, kw)) or chat_reply()):
            sc.call_model([{"role": "user", "content": "U"}], {**GEMINI, "base_url": "https://generativelanguage.googleapis.com"})
        self.assertEqual(sent[-1][0], "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")

    def test_search_request_and_grounding_parsing(self):
        sent, resolved = [], {REDIRECT + "AAA": "https://www.acme.example/about", REDIRECT + "BBB": "https://news.example/acme-fine"}

        def get(url, **kw):
            self.assertFalse(kw["follow_redirects"])
            return mock.Mock(status_code=302, headers={"location": resolved[url]})
        turns = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]
        with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append((url, kw)) or http_reply(grounded())), \
                mock.patch.object(httpx, "get", side_effect=get):
            out = lp.web_search({**GEMINI, "model": "models/gemini-3.8-flash", "reasoning_effort": "low"},
                                instructions="SYS", input=turns, what="web search")
        url, kw = sent[0]
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent")
        self.assertEqual(kw["headers"], {"x-goog-api-key": "k", "Content-Type": "application/json"})
        self.assertEqual(kw["json"], {
            "contents": [{"role": "user", "parts": [{"text": "q1"}]}, {"role": "model", "parts": [{"text": "a1"}]},
                         {"role": "user", "parts": [{"text": "q2"}]}],
            "tools": [{"google_search": {}}], "systemInstruction": {"parts": [{"text": "SYS"}]},
            "generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}}})
        text, seen = rs.extract(out)
        self.assertEqual(text, "The answer.")
        self.assertEqual(seen, {"https://acme.example/about": "acme.example", "https://news.example/acme-fine": "news.example",
                                "https://plain.example/page": "Plain page"})
        from backend.job_chat import web_sources
        self.assertEqual([(s["url"], s["cited"]) for s in web_sources(out, "The answer.", seen)],
                         [("https://www.acme.example/about", True), ("https://news.example/acme-fine", True)])

    def test_redirects_that_cannot_be_followed_are_kept_and_no_grounding_is_fine(self):
        with mock.patch.object(httpx, "post", return_value=http_reply(grounded())), \
                mock.patch.object(httpx, "get", side_effect=httpx.ConnectError("down")):
            out = lp.web_search(GEMINI, instructions="I", input="Q")
        self.assertIn(REDIRECT + "AAA", [r["url"] for r in out["output"][0]["results"]])
        plain = {"candidates": [{"content": {"parts": [{"text": "No search needed."}]}, "finishReason": "STOP"}]}
        with mock.patch.object(httpx, "post", return_value=http_reply(plain)):
            out = lp.web_search(GEMINI, instructions="I", input="Q")
        self.assertEqual(rs.extract(out), ("No search needed.", {}))

    def test_no_thinking_level_for_older_models_or_unlisted_levels(self):
        sent = []
        with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append(kw["json"]) or http_reply(grounded())), \
                mock.patch.object(httpx, "get", return_value=mock.Mock(status_code=200, headers={})):
            lp.web_search({**GEMINI, "model": "gemini-2.5-flash", "reasoning_effort": "low"}, instructions="I", input="Q")
            lp.web_search({**GEMINI, "reasoning_effort": "xhigh"}, instructions="I", input="Q")
        self.assertTrue(all("generationConfig" not in body for body in sent))

    def test_a_thinking_config_the_model_rejects_is_dropped(self):
        sent = []
        replies = [http_reply(status=400, text='{"error": {"message": "thinking_level is not supported"}}'),
                   http_reply(grounded())]
        with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append(dict(kw["json"])) or replies.pop(0)), \
                mock.patch.object(httpx, "get", return_value=mock.Mock(status_code=200, headers={})):
            lp.web_search({**GEMINI, "reasoning_effort": "high"}, instructions="I", input="Q")
        self.assertEqual(["generationConfig" in body for body in sent], [True, False])

    def test_a_wrong_key_comes_back_as_http_400_and_is_an_auth_error(self):
        wrong = http_reply(status=400, text='{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}')
        for call in (lambda: sc.call_model([{"role": "user", "content": "U"}], GEMINI),
                     lambda: lp.web_search(GEMINI, instructions="I", input="Q"),
                     lambda: lp.probe_level(GEMINI, "low")):
            with mock.patch.object(httpx, "post", return_value=wrong), self.assertRaises(sc.ScorerError) as ctx:
                call()
            self.assertTrue(ctx.exception.auth)
        # Another provider's 400 with similar words is not an auth failure.
        with mock.patch.object(httpx, "post", return_value=wrong), self.assertRaises(sc.ScorerError) as ctx:
            sc.call_model([{"role": "user", "content": "U"}], cfg_for("https://llm.example/v1"))
        self.assertFalse(ctx.exception.auth)

    def test_blocked_cut_off_and_empty_answers_are_errors(self):
        for data, text in (({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}, "blocked: SAFETY"),
                           ({"candidates": [{"content": {"parts": [{"text": "{\"a\":"}]}, "finishReason": "MAX_TOKENS"}]}, "cut off"),
                           ({"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}, "finish reason SAFETY")):
            with mock.patch.object(httpx, "post", return_value=http_reply(data)), self.assertRaisesRegex(sc.ScorerError, text):
                lp.web_search(GEMINI, instructions="I", input="Q")


# --------------------------------------------------------------------------
# xAI and OpenRouter
# --------------------------------------------------------------------------

class XaiAndOpenRouterTest(ProviderTestCase):
    def test_xai_gets_only_what_it_documents(self):
        sent = []
        response = {"status": "completed", "citations": ["https://x.example/a", {"url": "https://x.example/b", "title": "B"}],
                    "output": [{"type": "web_search_call", "action": {"sources": [{"url": "https://x.example/c"}]}},
                               {"type": "message", "content": [{"type": "output_text", "text": "Done.", "annotations": [
                                   {"type": "url_citation", "url": "https://x.example/a", "title": "A"}]}]}]}
        cfg = cfg_for("https://api.x.ai/v1", "grok-4.7", reasoning_effort="low")
        with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append((url, kw)) or http_reply(response)):
            out = lp.web_search(cfg, instructions="I", input="Q", context_size="low")
        url, kw = sent[0]
        self.assertEqual(url, "https://api.x.ai/v1/responses")
        self.assertEqual(kw["json"], {"model": "grok-4.7", "instructions": "I", "input": "Q",
                                      "tools": [{"type": "web_search", "user_location": AU}],
                                      "include": ["web_search_call.action.sources"]})  # no background, no search_context_size
        self.assertEqual(kw["timeout"], 300.0)
        text, seen = rs.extract(out)
        self.assertEqual((text, sorted(seen)), ("Done.", ["https://x.example/a", "https://x.example/b", "https://x.example/c"]))

    def test_openrouter_uses_the_web_plugin_on_chat_completions(self):
        sent = []
        data = {"choices": [{"message": {"role": "assistant", "content": "Found it.", "annotations": [
            {"type": "url_citation", "url_citation": {"url": "https://o.example/1", "title": "One", "content": "...", "start_index": 0, "end_index": 3}},
            {"type": "url_citation", "url_citation": {"url": "https://o.example/2", "title": "Two"}}]}}]}
        cfg = cfg_for("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-5.5", reasoning_effort="low")
        turns = [{"role": "user", "content": "q"}]
        with mock.patch.object(httpx, "post", side_effect=lambda url, **kw: sent.append((url, kw)) or http_reply(data)):
            out = lp.web_search(cfg, instructions="SYS", input=turns, context_size="low")
        url, kw = sent[0]
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(kw["json"], {"model": "anthropic/claude-sonnet-5.5",
                                      "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "q"}],
                                      "plugins": [{"id": "web", "max_results": 5}]})
        self.assertEqual(kw["headers"]["Authorization"], "Bearer k")
        text, seen = rs.extract(out)
        self.assertEqual((text, seen), ("Found it.", {"https://o.example/1": "One", "https://o.example/2": "Two"}))


# --------------------------------------------------------------------------
# Providers without web search fail fast
# --------------------------------------------------------------------------

NO_SEARCH = {"https://api.mistral.ai/v1": "Mistral", "https://api.groq.com/openai/v1": "Groq",
             "https://api.deepseek.com": "DeepSeek", "http://localhost:11434/v1": "Ollama (local)"}


class NoWebSearchTest(ProviderTestCase):
    def test_web_search_fails_before_any_request(self):
        for base, label in NO_SEARCH.items():
            with mock.patch.object(httpx, "post", side_effect=AssertionError("doomed request")), \
                    mock.patch.object(httpx, "get", side_effect=AssertionError("doomed request")):
                with self.assertRaises(sc.ScorerError) as ctx:
                    lp.web_search(cfg_for(base), instructions="I", input="Q", feature="Company research")
            message = str(ctx.exception)
            self.assertTrue(message.startswith(f"Company research needs web search, which {label} doesn't offer through this app"), message)
            self.assertIn("Settings > LLM", message)
            for ok in ("OpenAI", "Anthropic (Claude)", "Google Gemini", "xAI (Grok)", "OpenRouter"):
                self.assertIn(ok, message)
            self.assertFalse(ctx.exception.auth)
        for base in ("https://api.openai.com/v1", "https://api.anthropic.com", lp.GEMINI_OPENAI_BASE, "https://api.meta.ai/v1",
                     "https://api.x.ai/v1", "https://openrouter.ai/api/v1", "https://llm.example/v1"):
            self.assertIsNone(lp.web_search_problem(cfg_for(base)), base)

    def test_company_research_stops_at_once_and_marks_nothing_failed(self):
        companies = [{"name": f"C{i}", "key": f"nosearch{i}", "jobs": []} for i in range(3)]
        for base in NO_SEARCH:
            with mock.patch.object(rs, "get_config", return_value=cfg_for(base, concurrency=2)), \
                    mock.patch.object(rs, "run_agent", side_effect=AssertionError("doomed request")):
                out = rs.research_companies(companies)
            self.assertEqual((out["requested"], out["researched"], out["errors"], out["aborted"]), (3, 0, 0, None))
            self.assertIn("Company research needs web search", out["first_error"])
        from backend.db import CompanyProfile, SessionLocal
        db = SessionLocal()
        self.assertEqual(db.query(CompanyProfile).filter(CompanyProfile.key.like("nosearch%")).count(), 0)
        db.close()

    def test_finding_offices_still_reads_ads_but_skips_the_web_lookups(self):
        from backend.db import Job, SessionLocal, Workspace
        db = SessionLocal()
        ws = Workspace(name="No search offices")
        db.add(ws)
        db.commit()
        db.add(Job(workspace_id=ws.id, url="https://nosearch.example/1", title="PM", company="Lookup Co", status="to_review",
                   detail_status="summary", location_text="Sydney NSW", country="AU"))
        db.commit()
        ws_id = ws.id
        db.close()
        env = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://api.groq.com/openai/v1", "LLM_MODEL": "m"}
        with mock.patch.dict(os.environ, env), mock.patch.object(of, "research_offices", side_effect=AssertionError("doomed")):
            out = of.find_offices(ws_id)
        self.assertEqual((out["errors"], out["from_companies"]), (0, 0))
        self.assertEqual(out["unknown"], 1)
        db = SessionLocal()
        db.delete(db.get(Workspace, ws_id))
        db.commit()
        db.close()

    def test_the_chat_refuses_web_search_but_plain_chat_works_on_every_preset(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import Job, SessionLocal

        with TestClient(app) as c:
            ws = c.post("/api/workspaces", json={"name": "Chat no search"}).json()["id"]
            h = {"X-Workspace": str(ws)}
            db = SessionLocal()
            job = Job(workspace_id=ws, url="https://nosearch.example/chat", title="PM", company="Chatless", status="shortlisted",
                      detail_status="full", description="Run things.")
            db.add(job)
            db.commit()
            job_id = job.id
            db.close()
            try:
                for preset in lp.PRESETS:
                    env = {"LLM_API_KEY": "k", "LLM_BASE_URL": preset["base_url"] or "https://llm.example/v1", "LLM_MODEL": "m"}
                    with mock.patch.dict(os.environ, env), mock.patch.object(sc, "call_model", return_value="Plain answer."), \
                            mock.patch.object(httpx, "post", side_effect=AssertionError("doomed request")):
                        plain = c.post("/api/chats", headers=h, json={"job_ids": [job_id], "message": "Fit?"})
                        self.assertEqual(plain.status_code, 200, (preset["id"], plain.text))
                        if preset["web_search"]:
                            continue  # (its search path is covered by the tests above; here it would need a network)
                        web = c.post("/api/chats", headers=h, json={"job_ids": [job_id], "message": "News?", "web_search": True})
                    self.assertEqual(web.status_code, 400, preset["id"])
                    detail = web.json()["detail"]
                    self.assertIn("The chat's \"Search the web\" option needs web search", detail)
                    self.assertIn(preset["label"], detail)
            finally:
                c.delete(f"/api/workspaces/{ws}", headers=h)


if __name__ == "__main__":
    unittest.main()
