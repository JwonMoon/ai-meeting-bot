"""작업 흐름: 녹음 → 전사 → 회의록 초안 → (사람 검토·승인) → Confluence → Jira → Teams.

- 각 단계의 결과는 db 와 작업 폴더(DATA_DIR/jobs/<작업ID>/)에 남는다.
- 실패하면 상태를 failed 로 두고 어느 단계인지 적는다. 다시 돌리면 그 단계부터 이어서 한다.
  (전사가 끝난 뒤 요약이 실패했으면 전사는 다시 하지 않는다.)
- 승인 전에는 외부로 아무것도 보내지 않는다. publish() 는 승인된 회의록과 게시 위치를 받아야 돈다.
- 게시도 단계별로 기록한다. Jira 티켓 3개 중 2개를 만든 뒤 실패했으면 재시도 때 3번째만 만든다.
- 음성 인식과 요약 전에는 GPU 가 비기를 기다린다 (gpu.py).
"""

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import db as jobs_db
from . import gpu, stt, summarize
from .audio import AudioError, format_timestamp
from .config import ConfigError, Settings
from .db import Database, Job
from .httpclient import ApiError
from .integrations import confluence, jira, teams
from .models import Minutes, MinutesFormatError

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

STEP_TRANSCRIBE = "transcribe"
STEP_SUMMARIZE = "summarize"
STEP_PUBLISH = "publish"

# 어느 단계에서든 "작업 실패"로 묶어 처리하는 오류들.
STEP_ERRORS = (ConfigError, AudioError, stt.SttError, summarize.SummarizeError, ApiError, gpu.GpuBusy, MinutesFormatError, OSError)


class PipelineError(Exception):
    pass


@dataclass(frozen=True)
class PublishChoices:
    """검토 화면에서 고른 게시 위치."""

    parent_page_id: str = ""   # 비우면 .env 기본값
    space_key: str = ""
    project_key: str = ""      # 비우면 .env 기본값
    create_jira: bool = True
    send_teams: bool = True


def open_database(settings: Settings) -> Database:
    return Database(Path(settings.data_dir) / "meetingbot.sqlite3")


def job_dir(settings: Settings, job_id: str) -> Path:
    return Path(settings.data_dir) / "jobs" / job_id


# ---------------------------------------------------------------- 작업 만들기


def new_job(settings: Settings, db: Database, source: Path, title: Optional[str] = None, move: bool = False) -> Job:
    """녹음 파일을 작업 폴더로 옮겨(복사해) 두고 작업을 만든다."""
    source = Path(source)
    if not source.is_file():
        raise PipelineError("녹음 파일이 없음: %s" % source)
    job = db.create_job(title or source.name, str(source))
    folder = job_dir(settings, job.id)
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / ("original" + source.suffix.lower())
    if move:
        shutil.move(str(source), str(stored))
    else:
        shutil.copy2(str(source), str(stored))
    return db.update(job.id, audio_path=str(stored))


# ---------------------------------------------------------------- 검토 전 단계


def next_step(job: Job) -> Optional[str]:
    """지금 상태에서 다음에 할 단계. 사람 손을 기다리거나 끝났으면 None."""
    if job.status in (jobs_db.UPLOADED, jobs_db.TRANSCRIBING):
        return STEP_TRANSCRIBE
    if job.status in (jobs_db.TRANSCRIBED, jobs_db.SUMMARIZING):
        return STEP_SUMMARIZE
    if job.status in (jobs_db.APPROVED, jobs_db.PUBLISHING):
        return STEP_PUBLISH
    if job.status == jobs_db.FAILED:
        return job.step or STEP_TRANSCRIBE
    return None


def _fail(db: Database, job_id: str, step: str, error: Exception) -> Job:
    message = "%s 실패: %s" % (step, error)
    log.error("[%s] %s", job_id, message)
    db.add_event(job_id, message)
    return db.update(job_id, status=jobs_db.FAILED, step=step, error=str(error)[:2000])


def run_to_review(settings: Settings, db: Database, job_id: str, progress: Optional[Progress] = None) -> Job:
    """검토 대기(review)까지 진행한다. 실패하면 failed 상태의 작업을 돌려준다 (예외를 내지 않음)."""
    job = db.get(job_id)

    def note(message: str) -> None:
        db.add_event(job_id, message)
        log.info("[%s] %s", job_id, message)
        if progress:
            progress(message)

    step = next_step(job)
    while step in (STEP_TRANSCRIBE, STEP_SUMMARIZE):
        try:
            if step == STEP_TRANSCRIBE:
                job = _transcribe(settings, db, job, note)
            else:
                job = _summarize(settings, db, job, note)
        except STEP_ERRORS as error:
            return _fail(db, job_id, step, error)
        step = next_step(job)
    return job


def _transcribe(settings: Settings, db: Database, job: Job, note: Progress) -> Job:
    db.update(job.id, status=jobs_db.TRANSCRIBING, step=STEP_TRANSCRIBE, error="")
    note("GPU 확인 중")
    gpu.wait_until_free(settings, note)
    note("음성 인식 시작")
    folder = job_dir(settings, job.id)
    transcript = stt.transcribe(settings, Path(job.audio_path), folder, note)
    (folder / "transcript.txt").write_text(transcript.text + "\n", encoding="utf-8")
    transcript_path = folder / "transcript.json"
    transcript_path.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    note(
        "음성 인식 완료: 전체 %s, 말소리 %s, 구간 %d개, %.0f초 걸림"
        % (format_timestamp(transcript.duration_seconds), format_timestamp(transcript.speech_seconds),
           len(transcript.segments), transcript.elapsed_seconds)
    )
    if not transcript.text.strip():
        raise stt.SttError("전사 결과가 비어 있음. 녹음에 말소리가 없거나 STT_SILENCE_DB 가 너무 높음.")
    return db.update(job.id, status=jobs_db.TRANSCRIBED, transcript_path=str(transcript_path))


def load_transcript(job: Job) -> stt.Transcript:
    if not job.transcript_path:
        raise PipelineError("전사 결과가 아직 없음")
    return stt.Transcript.from_dict(json.loads(Path(job.transcript_path).read_text(encoding="utf-8")))


def _summarize(settings: Settings, db: Database, job: Job, note: Progress) -> Job:
    db.update(job.id, status=jobs_db.SUMMARIZING, step=STEP_SUMMARIZE, error="")
    transcript = load_transcript(job)
    note("GPU 확인 중")
    gpu.wait_until_free(settings, note)
    note("회의록 초안 작성 시작 (전사 %d자)" % len(transcript.text))
    result = summarize.summarize(settings, transcript.text)
    note(
        "회의록 초안 완료: %.0f초, 입력 %s 토큰, 안건 %d개, 액션아이템 %d개"
        % (result.elapsed_seconds, result.prompt_tokens, len(result.minutes.topics), len(result.minutes.action_items))
    )
    if result.near_context_limit:
        note("경고: 전사가 요약 입력 한도(OLLAMA_NUM_CTX=%d)에 닿음. 뒷부분이 빠졌을 수 있음." % result.num_ctx)
    minutes_json = json.dumps(result.minutes.to_dict(), ensure_ascii=False, indent=2)
    (job_dir(settings, job.id) / "minutes.json").write_text(minutes_json + "\n", encoding="utf-8")
    return db.update(job.id, status=jobs_db.REVIEW, minutes_json=minutes_json)


# ---------------------------------------------------------------- 승인 뒤 게시


def approve(db: Database, job_id: str, minutes: Minutes, choices: PublishChoices) -> Job:
    """검토 화면에서 고친 회의록과 게시 위치를 저장하고 승인 상태로 둔다. 아직 보내지는 않는다."""
    job = db.get(job_id)
    if job.status not in (jobs_db.REVIEW, jobs_db.FAILED, jobs_db.APPROVED):
        raise PipelineError("지금 상태(%s)에서는 승인할 수 없음" % job.status_label)
    publish = job.publish
    publish.update(choices.__dict__)
    db.add_event(job_id, "승인됨")
    return db.update(
        job_id,
        status=jobs_db.APPROVED,
        step=STEP_PUBLISH,
        error="",
        minutes_json=json.dumps(minutes.to_dict(), ensure_ascii=False, indent=2),
        publish=publish,
    )


def publish(settings: Settings, db: Database, job_id: str, progress: Optional[Progress] = None) -> Job:
    """승인된 회의록을 Confluence → Jira → Teams 순서로 보낸다. 이미 된 것은 건너뛴다."""
    job = db.get(job_id)
    if next_step(job) != STEP_PUBLISH:
        raise PipelineError("승인된 작업만 게시할 수 있음 (지금: %s)" % job.status_label)
    minutes = Minutes.from_dict(job.minutes_dict or {})
    state: Dict[str, Any] = job.publish

    def note(message: str) -> None:
        db.add_event(job_id, message)
        log.info("[%s] %s", job_id, message)
        if progress:
            progress(message)

    def save() -> None:
        db.update(job_id, publish=state)

    db.update(job_id, status=jobs_db.PUBLISHING, step=STEP_PUBLISH, error="")
    try:
        if not state.get("confluence_page_id"):
            page = confluence.create_page(
                settings, minutes.title, confluence.minutes_to_storage(minutes),
                parent_page_id=state.get("parent_page_id") or None, space_key=state.get("space_key") or None,
            )
            state["confluence_page_id"], state["confluence_url"] = page.id, page.url
            save()
            note("Confluence 페이지 생성: %s" % (page.url or page.id))
        else:
            note("Confluence 페이지는 이미 있음: %s" % state.get("confluence_url", state["confluence_page_id"]))

        keys: List[str] = list(state.get("jira_keys", []))
        if state.get("create_jira", True):
            for index, item in enumerate(minutes.action_items):
                if index < len(keys):
                    continue
                issue = jira.create_issue(
                    settings, item.task,
                    jira.action_item_description(item, minutes.title, state.get("confluence_url", "")),
                    project_key=state.get("project_key") or None,
                )
                keys.append(issue.key)
                state["jira_keys"] = keys
                save()
                note("Jira 티켓 생성: %s (%s)" % (issue.key, item.task[:40]))
            if not minutes.action_items:
                note("액션아이템이 없어 Jira 티켓을 만들지 않음")

        if state.get("send_teams", True) and not state.get("teams_sent"):
            teams.send_card(settings, "회의록: " + minutes.title, _teams_lines(settings, minutes, state))
            state["teams_sent"] = True
            save()
            note("Teams 알림 전송")
    except STEP_ERRORS as error:
        return _fail(db, job_id, STEP_PUBLISH, error)

    note("게시 완료")
    return db.update(job_id, status=jobs_db.PUBLISHED, publish=state)


def _teams_lines(settings: Settings, minutes: Minutes, state: Dict[str, Any]) -> List[str]:
    lines = [minutes.summary[:300]]
    if state.get("confluence_url"):
        lines.append("[회의록 보기](%s)" % state["confluence_url"])
    keys = state.get("jira_keys", [])
    if keys:
        links = ", ".join("[%s](%s/browse/%s)" % (k, settings.jira_base_url, k) for k in keys)
        lines.append("액션아이템 티켓: " + links)
    return lines


__all__ = [
    "PipelineError", "PublishChoices", "approve", "job_dir", "load_transcript", "new_job",
    "next_step", "open_database", "publish", "run_to_review",
]
