"""뒷단 일꾼: 작업을 한 번에 하나씩 순서대로 처리하는 스레드.

음성 인식과 요약은 GPU 를 쓰므로 동시에 두 건을 돌리지 않는다. 화면은 queue 에 작업 ID 만 넣고 바로 돌아온다.
처리 함수는 pipeline 의 것을 그대로 쓴다: 검토 전 단계는 run_to_review, 승인 뒤는 publish.
"""

import logging
import queue
import threading
from typing import Callable, Optional, Set

from .. import db as jobs_db
from .. import pipeline
from ..config import Settings

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, open_db: Optional[Callable[[], jobs_db.Database]] = None):
        self.settings = settings
        self._open_db = open_db or (lambda: pipeline.open_database(settings))
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._queued: Set[str] = set()
        self._lock = threading.Lock()
        self.current: Optional[str] = None
        self._thread = threading.Thread(target=self._loop, name="meetingbot-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)

    def enqueue(self, job_id: str) -> bool:
        """줄에 세운다. 이미 줄에 있거나 처리 중이면 False."""
        with self._lock:
            if job_id in self._queued or job_id == self.current:
                return False
            self._queued.add(job_id)
        self._queue.put(job_id)
        return True

    def is_pending(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._queued or job_id == self.current

    def recover(self) -> int:
        """켜질 때 한 번: 전에 처리 중이던 작업(끄는 사이 멈춘 것)을 다시 줄에 세운다. 실패한 것은 사람이 재시도를 누른다."""
        db = self._open_db()
        try:
            count = 0
            for job in db.list_jobs(limit=500):
                if job.status in (jobs_db.TRANSCRIBING, jobs_db.SUMMARIZING, jobs_db.PUBLISHING, jobs_db.UPLOADED, jobs_db.APPROVED):
                    if self.enqueue(job.id):
                        count += 1
            return count
        finally:
            db.close()

    def process(self, job_id: str) -> None:
        """한 작업을 다음 단계로 끝까지 밀어 준다 (검토 대기나 게시 완료, 또는 실패에서 멈춤)."""
        db = self._open_db()
        try:
            job = db.get(job_id)
            step = pipeline.next_step(job)
            if step == pipeline.STEP_PUBLISH:
                pipeline.publish(self.settings, db, job_id)
            elif step is not None:
                pipeline.run_to_review(self.settings, db, job_id)
        finally:
            db.close()

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            with self._lock:
                self._queued.discard(job_id)
                self.current = job_id
            try:
                self.process(job_id)
            except Exception:  # noqa: BLE001 - 일꾼 스레드는 죽지 않아야 한다
                log.exception("[%s] 처리 중 예상 못 한 오류", job_id)
                try:
                    db = self._open_db()
                    db.update(job_id, status=jobs_db.FAILED, error="처리 중 예상 못 한 오류. 서버 로그 확인.")
                    db.close()
                except Exception:  # noqa: BLE001
                    log.exception("[%s] 실패 기록도 실패", job_id)
            finally:
                with self._lock:
                    self.current = None
