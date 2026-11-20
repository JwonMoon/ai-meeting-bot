"""작업 기록(db), GPU 양보(gpu), 작업 흐름(pipeline) 시험. 네트워크, ffmpeg, GPU 없이 돈다."""

import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot import db as jobs_db  # noqa: E402
from meetingbot import gpu, pipeline, stt, summarize  # noqa: E402
from meetingbot.config import Settings  # noqa: E402
from meetingbot.httpclient import ApiError  # noqa: E402
from meetingbot.integrations import confluence, jira  # noqa: E402
from meetingbot.models import Minutes  # noqa: E402

from test_core import MINUTES_DICT, SETTINGS  # noqa: E402

logging.getLogger("meetingbot").setLevel(logging.CRITICAL)  # 의도된 실패 메시지를 숨긴다

TRANSCRIPT = stt.Transcript("a.mp3", 100.0, 80.0, [stt.Segment(0.0, 60.0, "안녕하세요 회의 시작합니다", 3.0)])


def fake_summary(minutes_dict=MINUTES_DICT, prompt_tokens=10):
    return summarize.SummaryResult(Minutes.from_dict(minutes_dict), 1.0, prompt_tokens, 5, None, None, None, 1000)


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = jobs_db.Database(self.tmp / "x.sqlite3")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_create_update_events(self):
        job = self.db.create_job("회의.mp3", "/a.mp3")
        self.assertEqual(job.status, jobs_db.UPLOADED)
        job = self.db.update(job.id, status=jobs_db.REVIEW, publish={"jira_keys": ["P-1"]})
        self.assertEqual(job.publish["jira_keys"], ["P-1"])
        self.assertEqual(job.status_label, "검토 대기")
        self.assertEqual([e["message"] for e in self.db.events(job.id)], ["녹음 받음: 회의.mp3"])
        self.assertEqual([j.id for j in self.db.list_jobs()], [job.id])

    def test_unknown_field_and_missing_job(self):
        with self.assertRaises(ValueError):
            self.db.update("nope", bogus=1)
        with self.assertRaises(KeyError):
            self.db.update("nope", status="x")
        with self.assertRaises(KeyError):
            self.db.get("nope")


class GpuTest(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(gpu.parse_utilization("37 %\n"), 37)
        self.assertIsNone(gpu.parse_utilization("[N/A]\n"))
        self.assertEqual(gpu.parse_processes("1234, /usr/bin/ollama\n[N/A], x\n"), ["/usr/bin/ollama(pid 1234)"])

    def test_busy_rules(self):
        self.assertFalse(gpu.GpuStatus(available=False).busy(20))
        self.assertTrue(gpu.GpuStatus(True, 50, []).busy(20))
        self.assertTrue(gpu.GpuStatus(True, 0, ["x(pid 1)"]).busy(20))
        self.assertFalse(gpu.GpuStatus(True, 5, []).busy(20))

    def test_wait_then_free(self):
        statuses = iter([gpu.GpuStatus(True, 90, []), gpu.GpuStatus(True, 90, []), gpu.GpuStatus(True, 3, [])])
        slept = []
        settings = Settings(**dict(SETTINGS.__dict__, gpu_poll_seconds=7))
        status = gpu.wait_until_free(settings, lambda m: None, query_fn=lambda: next(statuses), sleep=slept.append)
        self.assertEqual(status.utilization, 3)
        self.assertEqual(slept, [7, 7])

    def test_gives_up_after_limit(self):
        settings = Settings(**dict(SETTINGS.__dict__, gpu_wait_minutes=0))
        with self.assertRaises(gpu.GpuBusy):
            gpu.wait_until_free(settings, lambda m: None, query_fn=lambda: gpu.GpuStatus(True, 99, []), sleep=lambda s: None)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = Settings(**dict(SETTINGS.__dict__, data_dir=str(self.tmp / "data"), stt_command="x {audio}"))
        self.db = pipeline.open_database(self.settings)
        source = self.tmp / "회의.mp3"
        source.write_bytes(b"not really audio")
        self.job = pipeline.new_job(self.settings, self.db, source)
        self.free_gpu = mock.patch.object(gpu, "query", return_value=gpu.GpuStatus(available=False))
        self.free_gpu.start()

    def tearDown(self):
        self.free_gpu.stop()
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_new_job_copies_audio_into_job_folder(self):
        self.assertTrue(Path(self.job.audio_path).is_file())
        self.assertEqual(Path(self.job.audio_path).parent, pipeline.job_dir(self.settings, self.job.id))
        self.assertEqual(self.job.title, "회의.mp3")

    def test_happy_path_to_review(self):
        with mock.patch.object(stt, "transcribe", return_value=TRANSCRIPT) as t, \
             mock.patch.object(summarize, "summarize", return_value=fake_summary()) as s:
            job = pipeline.run_to_review(self.settings, self.db, self.job.id)
        self.assertEqual(job.status, jobs_db.REVIEW)
        self.assertEqual(job.minutes_dict["title"], "주간 회의")
        self.assertTrue(s.call_args[0][1].startswith("[00:00:00] 안녕하세요"))
        folder = pipeline.job_dir(self.settings, job.id)
        self.assertTrue((folder / "transcript.txt").is_file())
        self.assertTrue((folder / "minutes.json").is_file())
        self.assertEqual(pipeline.load_transcript(job).segments[0].text, "안녕하세요 회의 시작합니다")
        t.assert_called_once()

    def test_failure_at_summarize_then_resume_skips_transcribe(self):
        with mock.patch.object(stt, "transcribe", return_value=TRANSCRIPT) as t, \
             mock.patch.object(summarize, "summarize", side_effect=summarize.SummarizeError("모델 응답 이상")):
            job = pipeline.run_to_review(self.settings, self.db, self.job.id)
        self.assertEqual((job.status, job.step), (jobs_db.FAILED, pipeline.STEP_SUMMARIZE))
        self.assertIn("모델 응답 이상", job.error)
        self.assertEqual(pipeline.next_step(job), pipeline.STEP_SUMMARIZE)

        with mock.patch.object(stt, "transcribe") as t2, \
             mock.patch.object(summarize, "summarize", return_value=fake_summary()):
            job = pipeline.run_to_review(self.settings, self.db, self.job.id)
        self.assertEqual(job.status, jobs_db.REVIEW)
        self.assertEqual(job.error, "")
        t.assert_called_once()
        t2.assert_not_called()

    def test_gpu_busy_is_a_failure_not_a_crash(self):
        busy = Settings(**dict(self.settings.__dict__, gpu_wait_minutes=0))
        with mock.patch.object(gpu, "query", return_value=gpu.GpuStatus(True, 95, ["other(pid 1)"])), \
             mock.patch.object(gpu, "time") as fake_time:
            fake_time.monotonic.side_effect = [0, 1]
            job = pipeline.run_to_review(busy, self.db, self.job.id)
        self.assertEqual((job.status, job.step), (jobs_db.FAILED, pipeline.STEP_TRANSCRIBE))
        self.assertIn("GPU", job.error)

    def test_empty_transcript_fails(self):
        empty = stt.Transcript("a.mp3", 10.0, 0.0, [])
        with mock.patch.object(stt, "transcribe", return_value=empty):
            job = pipeline.run_to_review(self.settings, self.db, self.job.id)
        self.assertEqual(job.status, jobs_db.FAILED)
        self.assertIn("비어", job.error)

    def _to_review(self):
        with mock.patch.object(stt, "transcribe", return_value=TRANSCRIPT), \
             mock.patch.object(summarize, "summarize", return_value=fake_summary()):
            return pipeline.run_to_review(self.settings, self.db, self.job.id)

    def test_publish_requires_approval_and_sends_nothing_before(self):
        self._to_review()
        with mock.patch.object(confluence, "request") as req:
            with self.assertRaises(pipeline.PipelineError):
                pipeline.publish(self.settings, self.db, self.job.id)
        req.assert_not_called()

    def test_approve_then_publish_with_choices_and_edited_minutes(self):
        self._to_review()
        edited = dict(MINUTES_DICT, title="고친 제목", action_items=[
            {"task": "첫 일", "assignee": None, "due_date": None},
            {"task": "둘째 일", "assignee": "김", "due_date": "금요일"},
        ])
        job = pipeline.approve(self.db, self.job.id, Minutes.from_dict(edited), pipeline.PublishChoices(parent_page_id="999", project_key="OTHER"))
        self.assertEqual(job.status, jobs_db.APPROVED)
        self.assertEqual(job.minutes_dict["title"], "고친 제목")

        page = confluence.Page("77", "http://w/p/77")
        issues = [jira.Issue("OTHER-1", "u1"), jira.Issue("OTHER-2", "u2")]
        with mock.patch.object(confluence, "create_page", return_value=page) as cp, \
             mock.patch.object(jira, "create_issue", side_effect=issues) as ci, \
             mock.patch.object(pipeline.teams, "send_card", return_value=202) as sc:
            job = pipeline.publish(self.settings, self.db, self.job.id)
        self.assertEqual(job.status, jobs_db.PUBLISHED)
        self.assertEqual(cp.call_args[1]["parent_page_id"], "999")
        self.assertEqual(cp.call_args[0][1], "고친 제목")
        self.assertEqual(ci.call_args_list[0][1]["project_key"], "OTHER")
        self.assertIn("http://w/p/77", ci.call_args_list[0][0][2])
        self.assertEqual(job.publish["jira_keys"], ["OTHER-1", "OTHER-2"])
        lines = sc.call_args[0][2]
        self.assertTrue(any("OTHER-2" in line for line in lines))
        self.assertTrue(job.publish["teams_sent"])

    def test_publish_resumes_without_duplicates(self):
        self._to_review()
        edited = dict(MINUTES_DICT, action_items=[
            {"task": "첫 일", "assignee": None, "due_date": None},
            {"task": "둘째 일", "assignee": None, "due_date": None},
        ])
        pipeline.approve(self.db, self.job.id, Minutes.from_dict(edited), pipeline.PublishChoices())

        page = confluence.Page("77", "http://w/p/77")
        with mock.patch.object(confluence, "create_page", return_value=page), \
             mock.patch.object(jira, "create_issue", side_effect=[jira.Issue("P-1", "u"), ApiError("Jira 죽음")]), \
             mock.patch.object(pipeline.teams, "send_card") as sc:
            job = pipeline.publish(self.settings, self.db, self.job.id)
        self.assertEqual((job.status, job.step), (jobs_db.FAILED, pipeline.STEP_PUBLISH))
        self.assertEqual(job.publish["jira_keys"], ["P-1"])
        sc.assert_not_called()

        with mock.patch.object(confluence, "create_page") as cp, \
             mock.patch.object(jira, "create_issue", return_value=jira.Issue("P-2", "u")) as ci, \
             mock.patch.object(pipeline.teams, "send_card", return_value=200):
            job = pipeline.publish(self.settings, self.db, self.job.id)
        self.assertEqual(job.status, jobs_db.PUBLISHED)
        cp.assert_not_called()
        self.assertEqual(ci.call_count, 1)
        self.assertEqual(ci.call_args[0][1], "둘째 일")
        self.assertEqual(job.publish["jira_keys"], ["P-1", "P-2"])

    def test_choices_can_skip_jira_and_teams(self):
        self._to_review()
        pipeline.approve(self.db, self.job.id, Minutes.from_dict(MINUTES_DICT), pipeline.PublishChoices(create_jira=False, send_teams=False))
        with mock.patch.object(confluence, "create_page", return_value=confluence.Page("1", "u")), \
             mock.patch.object(jira, "create_issue") as ci, \
             mock.patch.object(pipeline.teams, "send_card") as sc:
            job = pipeline.publish(self.settings, self.db, self.job.id)
        self.assertEqual(job.status, jobs_db.PUBLISHED)
        ci.assert_not_called()
        sc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
