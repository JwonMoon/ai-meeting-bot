"""화면 시험. 폼 해석은 FastAPI 없이, 화면 흐름은 FastAPI 가 설치된 경우에만 돈다."""

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
from meetingbot.integrations import confluence  # noqa: E402
from meetingbot.models import MinutesFormatError  # noqa: E402
from meetingbot.web import forms  # noqa: E402
from meetingbot.web.worker import Worker  # noqa: E402

from test_core import MINUTES_DICT, SETTINGS  # noqa: E402
from test_pipeline import TRANSCRIPT, fake_summary  # noqa: E402

logging.getLogger("meetingbot").setLevel(logging.CRITICAL)

FORM = {
    "title": " 주간 회의 ", "summary": "요약",
    "topic_1_topic": "안건 A", "topic_1_discussion": "논의", "topic_1_decisions": "결정 1\n\n결정 2\n",
    "topic_2_topic": "", "topic_2_discussion": "버려짐",
    "action_1_task": "정리", "action_1_assignee": "김", "action_1_due": "",
    "action_2_task": "", "action_2_assignee": "버려짐",
    "action_3_task": "둘째 일",
    "parent_page_id": "999", "project_key": "", "create_jira": "1",
}


class FormsTest(unittest.TestCase):
    def test_parse_review(self):
        minutes, choices = forms.parse_review(FORM)
        self.assertEqual(minutes.title, "주간 회의")
        self.assertEqual([t.topic for t in minutes.topics], ["안건 A"])
        self.assertEqual(minutes.topics[0].decisions, ["결정 1", "결정 2"])
        self.assertEqual([a.task for a in minutes.action_items], ["정리", "둘째 일"])
        self.assertEqual((minutes.action_items[0].assignee, minutes.action_items[0].due_date), ("김", None))
        self.assertEqual(choices.parent_page_id, "999")
        self.assertTrue(choices.create_jira)
        self.assertFalse(choices.send_teams)  # 체크 안 함

    def test_empty_title_rejected(self):
        with self.assertRaises(MinutesFormatError):
            forms.parse_minutes(dict(FORM, title=" "))


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = Settings(**dict(SETTINGS.__dict__, data_dir=str(self.tmp), stt_command="x {audio}"))
        self.db = pipeline.open_database(self.settings)
        src = self.tmp / "a.mp3"
        src.write_bytes(b"x")
        self.job = pipeline.new_job(self.settings, self.db, src)
        self.worker = Worker(self.settings)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_enqueue_deduplicates(self):
        self.assertTrue(self.worker.enqueue(self.job.id))
        self.assertFalse(self.worker.enqueue(self.job.id))
        self.assertTrue(self.worker.is_pending(self.job.id))

    def test_process_runs_to_review(self):
        with mock.patch.object(gpu, "query", return_value=gpu.GpuStatus(available=False)), \
             mock.patch.object(stt, "transcribe", return_value=TRANSCRIPT), \
             mock.patch.object(summarize, "summarize", return_value=fake_summary()):
            self.worker.process(self.job.id)
        self.assertEqual(self.db.get(self.job.id).status, jobs_db.REVIEW)

    def test_recover_requeues_interrupted_jobs(self):
        self.db.update(self.job.id, status=jobs_db.TRANSCRIBING)
        self.assertEqual(self.worker.recover(), 1)
        self.db.update(self.job.id, status=jobs_db.FAILED)
        self.assertEqual(Worker(self.settings).recover(), 0)


try:
    from fastapi.testclient import TestClient

    from meetingbot.web.app import create_app
except ImportError:  # 화면 의존성이 없는 컴퓨터에서는 아래 시험을 건너뛴다
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi 미설치")
class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = Settings(**dict(SETTINGS.__dict__, data_dir=str(self.tmp), stt_command="x {audio}"))
        self.worker = Worker(self.settings)
        self.worker.enqueue = mock.Mock(return_value=True)  # 뒷단은 돌리지 않고 호출만 확인
        self.worker.start = lambda: None
        self.client = TestClient(create_app(self.settings, self.worker))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp)

    def test_index_empty(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("아직 없습니다", page.text)

    def test_upload_creates_job_and_enqueues(self):
        reply = self.client.post("/jobs", files={"file": ("회의.m4a", b"audio-bytes", "audio/mp4")}, data={"title": "주간"}, follow_redirects=False)
        self.assertEqual(reply.status_code, 303)
        job_id = reply.headers["location"].rsplit("/", 1)[1]
        self.worker.enqueue.assert_called_once_with(job_id)
        db = pipeline.open_database(self.settings)
        job = db.get(job_id)
        db.close()
        self.assertEqual(job.title, "주간")
        self.assertTrue(Path(job.audio_path).is_file())
        self.assertFalse((self.tmp / "uploads" / "회의.m4a").exists())  # 옮겨졌다
        page = self.client.get("/jobs/%s" % job_id)
        self.assertIn("대기", page.text)

    def test_upload_rejects_unknown_suffix(self):
        reply = self.client.post("/jobs", files={"file": ("x.txt", b"a", "text/plain")}, follow_redirects=False)
        self.assertIn("error=", reply.headers["location"])
        self.worker.enqueue.assert_not_called()

    def _job_in_review(self):
        db = pipeline.open_database(self.settings)
        src = self.tmp / "a.mp3"
        src.write_bytes(b"x")
        job = pipeline.new_job(self.settings, db, src)
        with mock.patch.object(gpu, "query", return_value=gpu.GpuStatus(available=False)), \
             mock.patch.object(stt, "transcribe", return_value=TRANSCRIPT), \
             mock.patch.object(summarize, "summarize", return_value=fake_summary()):
            pipeline.run_to_review(self.settings, db, job.id)
        db.close()
        return job.id

    def test_review_page_and_approve(self):
        job_id = self._job_in_review()
        page = self.client.get("/jobs/%s" % job_id)
        self.assertIn("회의록 검토", page.text)
        self.assertIn('value="주간 회의"', page.text)
        self.assertIn("전사 보기", page.text)
        self.assertEqual(self.client.get("/jobs/%s/transcript" % job_id).text.strip(), TRANSCRIPT.text)

        reply = self.client.post("/jobs/%s/approve" % job_id, data=dict(FORM, action="publish"), follow_redirects=False)
        self.assertEqual(reply.status_code, 303)
        self.worker.enqueue.assert_called_once_with(job_id)
        db = pipeline.open_database(self.settings)
        job = db.get(job_id)
        db.close()
        self.assertEqual(job.status, jobs_db.APPROVED)
        self.assertEqual(job.minutes_dict["title"], "주간 회의")
        self.assertEqual(job.publish["parent_page_id"], "999")
        self.assertFalse(job.publish["send_teams"])

    def test_save_only_does_not_enqueue(self):
        job_id = self._job_in_review()
        self.client.post("/jobs/%s/approve" % job_id, data=dict(FORM, action="save"), follow_redirects=False)
        self.worker.enqueue.assert_not_called()

    def test_bad_form_redirects_with_error(self):
        job_id = self._job_in_review()
        reply = self.client.post("/jobs/%s/approve" % job_id, data=dict(FORM, title=""), follow_redirects=False)
        self.assertIn("error=", reply.headers["location"])

    def test_published_page_shows_links(self):
        job_id = self._job_in_review()
        db = pipeline.open_database(self.settings)
        pipeline.approve(db, job_id, forms.parse_minutes(FORM), pipeline.PublishChoices(create_jira=False, send_teams=False))
        with mock.patch.object(confluence, "create_page", return_value=confluence.Page("7", "http://w/7")):
            pipeline.publish(self.settings, db, job_id)
        db.close()
        page = self.client.get("/jobs/%s" % job_id)
        self.assertIn("게시 완료", page.text)
        self.assertIn("http://w/7", page.text)
        self.assertNotIn("회의록 검토", page.text)

    def test_delete_removes_job_and_folder(self):
        job_id = self._job_in_review()
        folder = pipeline.job_dir(self.settings, job_id)
        self.assertTrue(folder.is_dir())
        reply = self.client.post("/jobs/%s/delete" % job_id, follow_redirects=False)
        self.assertEqual(reply.headers["location"], "/")
        self.assertFalse(folder.exists())
        self.assertEqual(self.client.get("/jobs/%s" % job_id).status_code, 404)

    def test_retry_failed_job(self):
        job_id = self._job_in_review()
        db = pipeline.open_database(self.settings)
        db.update(job_id, status=jobs_db.FAILED, step="summarize", error="x")
        db.close()
        self.client.post("/jobs/%s/retry" % job_id, follow_redirects=False)
        self.worker.enqueue.assert_called_once_with(job_id)


if __name__ == "__main__":
    unittest.main()
