"""Interface language selection and safe, non-mutating catalogue delivery."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile

from django.http import JsonResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path
from django.utils import translation

from .interface_language import (
    _catalogue,
    catalogue,
    response_language_instruction,
)


SOURCE_RECORD = {
    "title": "保存",
    "description": "维护记录：原始中文内容，不得改写。",
    "fields": {"工单号": "WO-001", "状态": "已完成"},
    "source_url": "https://example.test/资料?id=EN-CN",
}


def source_record(request):
    return JsonResponse(SOURCE_RECORD)


urlpatterns = [
    path("i18n/catalog.js", catalogue),
    path("source-record/", source_record),
]


@override_settings(
    ROOT_URLCONF=__name__,
    MIDDLEWARE=["fos_rag.interface_language.InterfaceLocaleMiddleware"],
)
class InterfaceLanguageTests(SimpleTestCase):
    def setUp(self):
        self.previous_language = translation.get_language()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.directory = self.root / "static" / "i18n"
        self.directory.mkdir(parents=True)
        self.settings_override = override_settings(BASE_DIR=self.root)
        self.settings_override.enable()
        _catalogue.cache_clear()
        self.write_catalogue("common.json", {"messages": {"保存": "Save"}})

    def tearDown(self):
        _catalogue.cache_clear()
        self.settings_override.disable()
        self.temporary.cleanup()
        if self.previous_language:
            translation.activate(self.previous_language)
        else:
            translation.deactivate_all()

    def write_catalogue(self, filename, payload):
        (self.directory / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def payload(self, response):
        source = response.content.decode("utf-8")
        self.assertTrue(source.startswith("window.FOS_I18N_CATALOG="))
        return json.loads(source.removeprefix("window.FOS_I18N_CATALOG=").removesuffix(";\n"))

    def test_default_is_chinese_even_when_browser_prefers_english(self):
        response = self.client.get("/i18n/catalog.js", HTTP_ACCEPT_LANGUAGE="en-US,en;q=0.9")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Language"], "zh-hans")
        self.assertIn("cookie", response.headers["Vary"].lower())

    def test_english_cookie_is_used_across_requests(self):
        self.client.cookies["fos_language"] = "en"
        for url in ("/i18n/catalog.js", "/source-record/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Content-Language"], "en")

    def test_chinese_and_invalid_cookies_fall_back_safely(self):
        for language in ("zh-hans", "CN", "fr", "en-US", "<script>alert(1)</script>"):
            with self.subTest(language=language):
                self.client.cookies["fos_language"] = language
                response = self.client.get("/i18n/catalog.js")
                self.assertEqual(response.headers["Content-Language"], "zh-hans")

    def test_catalogues_merge_and_common_terms_take_precedence(self):
        self.write_catalogue("a.json", {
            "messages": {"保存": "Store", "上传": "Upload"},
            "patterns": [{"source": "共 {count} 条", "target": "{count} records"}],
            "englishSource": {"Overview": "概览"},
        })
        self.write_catalogue("z.json", {"messages": {"删除": "Delete"}})
        payload = self.payload(self.client.get("/i18n/catalog.js"))
        self.assertEqual(payload["messages"], {"保存": "Save", "上传": "Upload", "删除": "Delete"})
        self.assertEqual(payload["patterns"], [{"source": "共 {count} 条", "target": "{count} records"}])
        self.assertEqual(payload["englishSource"], {"Overview": "概览"})

    def test_unchanged_catalogue_returns_304_with_the_same_etag(self):
        first = self.client.get("/i18n/catalog.js")
        etag = first.headers["ETag"]
        second = self.client.get("/i18n/catalog.js", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.content, b"")
        self.assertEqual(second.headers["ETag"], etag)
        self.assertEqual(second.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("must-revalidate", second.headers["Cache-Control"])

    def test_editing_a_catalogue_invalidates_etag_and_cached_payload(self):
        first = self.client.get("/i18n/catalog.js")
        self.write_catalogue("common.json", {"messages": {"保存": "Save changes"}})
        second = self.client.get("/i18n/catalog.js", HTTP_IF_NONE_MATCH=first.headers["ETag"])
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.headers["ETag"], first.headers["ETag"])
        self.assertEqual(self.payload(second)["messages"]["保存"], "Save changes")

    def test_script_closing_sequences_are_escaped_without_changing_values(self):
        value = '</script><script>alert("unsafe")</script>'
        self.write_catalogue("common.json", {"messages": {"测试": value}})
        response = self.client.get("/i18n/catalog.js")
        self.assertNotIn(b"</script>", response.content)
        self.assertNotIn(b"<script>", response.content)
        self.assertIn(b"\\u003c/script>", response.content)
        self.assertEqual(self.payload(response)["messages"]["测试"], value)
        self.assertEqual(response.headers["Content-Type"], "application/javascript; charset=utf-8")

    def test_language_selection_does_not_translate_or_mutate_source_data(self):
        original = deepcopy(SOURCE_RECORD)
        for language in ("en", "zh-hans", "en"):
            self.client.cookies["fos_language"] = language
            self.client.get("/i18n/catalog.js")
            response = self.client.get("/source-record/")
            self.assertEqual(response.json(), original)
            self.assertEqual(SOURCE_RECORD, original)
        # SimpleTestCase also rejects any attempted database access in these requests.

    def test_model_language_instruction_preserves_data_and_explicit_user_choice(self):
        for language, expected in (("en", "English"), ("zh-hans", "Simplified Chinese")):
            with self.subTest(language=language):
                instruction = response_language_instruction(language)
                self.assertIn(f"interface language is {expected}", instruction)
                self.assertIn("unless the user explicitly requests another language", instruction)
                self.assertIn("collected data in their original form", instruction)
                self.assertIn("permission or confirmation requirement", instruction)


class ModelInterfaceLanguageTests(SimpleTestCase):
    def test_chat_payload_adds_language_without_rewriting_messages_or_tools(self):
        from .interface_model import InterfaceChatOpenAI

        source_quote = '原文：“保存”是设备标签；工单号 WO-001，https://example.test/资料'
        messages = [
            {"role": "system", "content": "只允许只读查询；引用必须与原文一致。"},
            {"role": "user", "content": source_quote},
        ]
        tools = [{
            "type": "function",
            "function": {
                "name": "kb_source_lookup",
                "description": "读取原始资料",
                "parameters": {
                    "type": "object",
                    "properties": {"drawing_id": {"type": "string", "description": "图号"}},
                    "required": ["drawing_id"],
                },
            },
        }]
        original_messages = deepcopy(messages)
        original_tools = deepcopy(tools)
        for language, expected in (("en", "English"), ("zh-hans", "Simplified Chinese")):
            with self.subTest(language=language):
                model = InterfaceChatOpenAI(
                    interface_language=language,
                    api_key="fixture",
                    base_url="http://127.0.0.1:9/v1",
                    model="fixture-model",
                    max_retries=0,
                )
                try:
                    payload = model._get_request_payload(messages, tools=tools)
                    self.assertEqual(payload["messages"][0]["role"], "system")
                    self.assertIn(f"interface language is {expected}", payload["messages"][0]["content"])
                    self.assertEqual(payload["messages"][1:], original_messages)
                    self.assertEqual(payload["tools"], original_tools)
                    self.assertEqual(payload["model"], "fixture-model")
                    self.assertNotIn("interface_language", payload)
                    # Building a payload repeatedly must not accumulate language prompts.
                    repeated = model._get_request_payload(messages, tools=tools)
                    self.assertEqual(repeated["messages"], payload["messages"])
                finally:
                    model.root_client.close()
        self.assertEqual(messages, original_messages)
        self.assertEqual(tools, original_tools)

    def test_llm_settings_captures_request_language_in_its_configuration(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from kb.config import llm_settings

        config = SimpleNamespace(
            llm_base_url="http://127.0.0.1:8080/v1",
            llm_api_key="fixture",
            llm_model="fixture-model",
            llm_temperature=0,
            llm_vision=False,
            qa_enhance=False,
            llm_remote_enabled=False,
            all_remote_apis_enabled=False,
        )
        with patch("kb.config.get_config", return_value=config):
            with translation.override("en"):
                english = llm_settings()
            with translation.override("zh-hans"):
                chinese = llm_settings()
        self.assertEqual(english["response_language"], "en")
        self.assertEqual(chinese["response_language"], "zh-hans")
        self.assertEqual(
            {key: value for key, value in english.items() if key != "response_language"},
            {key: value for key, value in chinese.items() if key != "response_language"},
        )
        self.assertEqual(english["model"], "fixture-model")

    def test_connector_background_worker_keeps_the_original_request_language(self):
        from unittest.mock import patch
        from kb.connector_agent import start_async

        languages = []
        with patch("kb.connector_agent.threading.Thread") as thread, \
                patch("kb.connector_agent.run", side_effect=lambda job: languages.append((job, translation.get_language()))) as run, \
                patch("kb.connector_agent.close_old_connections") as close:
            with translation.override("en"):
                start_async("fixture-job")
            thread.return_value.start.assert_called_once_with()
            kwargs = thread.call_args.kwargs
            # Execute the captured worker after the request context has reverted.
            with translation.override("zh-hans"):
                kwargs["target"](*kwargs.get("args", ()))
                self.assertEqual(translation.get_language(), "zh-hans")
            run.assert_called_once_with("fixture-job")
            self.assertEqual(languages, [("fixture-job", "en")])
            self.assertEqual(close.call_count, 2)

    def test_isolated_worker_applies_language_only_when_it_is_supplied(self):
        from unittest.mock import Mock, patch
        from .isolated_model_worker import complete

        messages = [{"role": "user", "content": "原文：阀门 V-01；字段名：工单号"}]
        original = deepcopy(messages)
        base_config = {
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "fixture",
            "model": "fixture-model",
            "temperature": 0,
            "max_tokens": 128,
        }
        for language in (None, "en", "zh-hans"):
            with self.subTest(language=language):
                config = dict(base_config)
                if language:
                    config["language_instruction"] = response_language_instruction(language)
                response = Mock(status_code=200)
                response.json.return_value = {"choices": [{"message": {"content": "Fixture answer"}}]}
                with patch("fos_rag.isolated_model_worker.httpx.Client") as client:
                    sender = client.return_value.__enter__.return_value
                    sender.post.return_value = response
                    result = complete({"config": config, "messages": messages})
                    payload = sender.post.call_args.kwargs["json"]
                self.assertEqual(result, {"ok": True, "content": "Fixture answer"})
                expected_messages = ([{"role": "system", "content": config["language_instruction"]}] if language else []) + original
                self.assertEqual(payload["messages"], expected_messages)
                self.assertEqual(payload["model"], "fixture-model")
                self.assertEqual(payload["max_tokens"], 128)
                self.assertFalse(payload["stream"])
                self.assertNotIn("language_instruction", payload)
                self.assertEqual(messages, original)
