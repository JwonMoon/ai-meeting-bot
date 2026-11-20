"""네트워크 없이 도는 단위 시험.  실행: python3 -m unittest discover -s tests"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot import httpclient, summarize  # noqa: E402
from meetingbot.config import ConfigError, Settings  # noqa: E402
from meetingbot.integrations import confluence, jira, teams  # noqa: E402
from meetingbot.models import ActionItem, Minutes, MinutesFormatError, Topic  # noqa: E402

SETTINGS = Settings(
    confluence_base_url="https://wiki.example/ctx",
    confluence_token="ctoken",
    confluence_space_key="SPACE",
    confluence_parent_page_id="111",
    jira_base_url="https://jira.example/ctx",
    jira_token="jtoken",
    jira_project_key="PROJ",
    jira_issue_type_id="3",
    teams_webhook_url="https://hook.example/secret-path?sig=SECRET",
    ollama_base_url="http://localhost:11435",
    ollama_model="m:1",
    ollama_num_ctx=1000,
)

MINUTES_DICT = {
    "title": "주간 회의",
    "summary": "요약",
    "topics": [{"topic": "안건", "discussion": "논의", "decisions": ["A안 <확정>"]}],
    "action_items": [{"task": "정리", "assignee": "", "due_date": None}],
}


class ModelsTest(unittest.TestCase):
    def test_round_trip_and_blank_becomes_none(self):
        minutes = Minutes.from_dict(MINUTES_DICT)
        self.assertIsNone(minutes.action_items[0].assignee)
        self.assertEqual(minutes.to_dict()["topics"][0]["decisions"], ["A안 <확정>"])

    def test_missing_field_is_rejected(self):
        with self.assertRaises(MinutesFormatError):
            Minutes.from_dict({"title": "t", "summary": "s", "topics": []})

    def test_wrong_type_is_rejected(self):
        with self.assertRaises(MinutesFormatError):
            Minutes.from_dict(dict(MINUTES_DICT, topics="안건"))


class ConfluenceTest(unittest.TestCase):
    def test_storage_escapes_text(self):
        html = confluence.minutes_to_storage(Minutes.from_dict(MINUTES_DICT))
        self.assertIn("A안 &lt;확정&gt;", html)
        self.assertNotIn("<확정>", html)
        self.assertIn("<td>-</td>", html)

    def test_create_page_request_and_result(self):
        reply = json.dumps({"id": 42, "_links": {"base": "http://w", "webui": "/p/42"}})
        with mock.patch.object(confluence, "request", return_value=(200, reply)) as req:
            page = confluence.create_page(SETTINGS, "제목", "<p>x</p>")
        method, url, token, body = req.call_args[0]
        self.assertEqual((method, url, token), ("POST", "https://wiki.example/ctx/rest/api/content/", "ctoken"))
        self.assertEqual(body["ancestors"], [{"id": "111"}])
        self.assertEqual(body["space"], {"key": "SPACE"})
        self.assertEqual((page.id, page.url), ("42", "http://w/p/42"))

    def test_create_page_failure_raises(self):
        with mock.patch.object(confluence, "request", return_value=(403, "no")):
            with self.assertRaises(httpclient.ApiError):
                confluence.create_page(SETTINGS, "제목", "<p>x</p>")


class JiraTest(unittest.TestCase):
    def test_create_issue_flattens_and_truncates_summary(self):
        with mock.patch.object(jira, "request", return_value=(201, '{"key": "PROJ-7"}')) as req:
            issue = jira.create_issue(SETTINGS, "첫 줄\n둘째 줄 " + "가" * 400, "설명")
        fields = req.call_args[0][3]["fields"]
        self.assertNotIn("\n", fields["summary"])
        self.assertEqual(len(fields["summary"]), jira.SUMMARY_MAX_CHARS)
        self.assertEqual(fields["issuetype"], {"id": "3"})
        self.assertEqual(issue.url, "https://jira.example/ctx/browse/PROJ-7")

    def test_description_marks_unknown_assignee(self):
        text = jira.action_item_description(ActionItem(task="t"), "회의", "http://p")
        self.assertIn("담당자(회의에서 언급): 미정", text)
        self.assertIn("http://p", text)


class TeamsTest(unittest.TestCase):
    def test_card_shape(self):
        card = teams.build_card("제목", ["한 줄"])
        content = card["attachments"][0]["content"]
        self.assertEqual(content["version"], "1.2")
        self.assertEqual([b["text"] for b in content["body"]], ["제목", "한 줄"])

    def test_missing_webhook_is_config_error(self):
        empty = Settings(**dict(SETTINGS.__dict__, teams_webhook_url=""))
        with self.assertRaises(ConfigError):
            teams.send_card(empty, "제목", [])


class HttpClientTest(unittest.TestCase):
    def test_connection_error_hides_secret_url(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertRaises(httpclient.ApiError) as caught:
                httpclient.request("POST", SETTINGS.teams_webhook_url, body={})
        self.assertIn("hook.example", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("secret-path", str(caught.exception))


class SummarizeTest(unittest.TestCase):
    def _reply(self, content, prompt_tokens=10):
        return json.dumps(
            {
                "message": {"role": "assistant", "content": content},
                "prompt_eval_count": prompt_tokens,
                "eval_count": 5,
                "eval_duration": 2_000_000_000,
            }
        )

    def test_request_and_result(self):
        reply = self._reply(json.dumps(MINUTES_DICT))
        with mock.patch.object(summarize, "request", return_value=(200, reply)) as req:
            result = summarize.summarize(SETTINGS, "  전사  ")
        body = req.call_args[1]["body"]
        self.assertEqual(body["model"], "m:1")
        self.assertIs(body["stream"], False)
        self.assertEqual(body["keep_alive"], 0)
        self.assertEqual(body["options"]["num_ctx"], 1000)
        self.assertTrue(body["messages"][1]["content"].endswith("전사"))
        self.assertEqual(result.minutes.title, "주간 회의")
        self.assertEqual(result.write_seconds, 2.0)
        self.assertFalse(result.near_context_limit)

    def test_near_context_limit(self):
        reply = self._reply(json.dumps(MINUTES_DICT), prompt_tokens=960)
        with mock.patch.object(summarize, "request", return_value=(200, reply)):
            self.assertTrue(summarize.summarize(SETTINGS, "전사").near_context_limit)

    def test_bad_output_raises(self):
        for content in ("글자일 뿐", json.dumps({"title": "t"})):
            with mock.patch.object(summarize, "request", return_value=(200, self._reply(content))):
                with self.assertRaises(summarize.SummarizeError):
                    summarize.summarize(SETTINGS, "전사")

    def test_empty_transcript_raises_without_calling(self):
        with mock.patch.object(summarize, "request") as req:
            with self.assertRaises(summarize.SummarizeError):
                summarize.summarize(SETTINGS, "   ")
        req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
