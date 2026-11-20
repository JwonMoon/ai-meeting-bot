"""FastAPI 앱. 화면 세 개: 작업 목록(업로드), 작업 상세(진행 상황, 검토·승인), 전사 보기.

- 127.0.0.1 에서만 듣는다 (.env WEB_HOST). 노트북은 SSH 터널로 들어온다.
- 로그인은 없다. Thor 에 SSH 로 들어올 수 있는 사람만 쓸 수 있다는 전제다.
- 무거운 일은 Worker 스레드가 한 건씩 한다. 화면은 상태만 보여 주고, 진행 중이면 몇 초마다 새로 고친다.
- 폼 → 회의록 변환은 forms.py, 단계 처리는 pipeline.py 에 있다. 이 파일은 연결만 한다.

FastAPI 사용법 근거: https://fastapi.tiangolo.com/ (UploadFile, Form, Jinja2Templates, lifespan)
"""

import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db as jobs_db
from .. import pipeline
from ..audio import AUDIO_SUFFIXES, format_timestamp
from ..config import Settings, load_settings
from ..models import MinutesFormatError
from .forms import parse_review
from .worker import Worker

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["hms"] = format_timestamp

# 진행 중 상태: 화면을 자동으로 새로 고친다.
BUSY = (jobs_db.UPLOADED, jobs_db.TRANSCRIBING, jobs_db.SUMMARIZING, jobs_db.APPROVED, jobs_db.PUBLISHING)


def create_app(settings: Optional[Settings] = None, worker: Optional[Worker] = None) -> FastAPI:
    settings = settings or load_settings()
    worker = worker or Worker(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        recovered = worker.recover()
        if recovered:
            log.info("멈춰 있던 작업 %d개를 다시 줄에 세움", recovered)
        yield
        worker.stop()

    app = FastAPI(title="회의록 봇", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.worker = worker

    def open_db() -> jobs_db.Database:
        return pipeline.open_database(settings)

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.update({"request": request, "settings": settings, "labels": jobs_db.STATUS_LABELS})
        return TEMPLATES.TemplateResponse(request, name, context)

    def load_job(db: jobs_db.Database, job_id: str) -> jobs_db.Job:
        try:
            return db.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="작업이 없음: %s" % job_id)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, error: str = ""):
        db = open_db()
        try:
            jobs = db.list_jobs(limit=100)
        finally:
            db.close()
        busy = any(j.status in BUSY for j in jobs)
        return render(request, "index.html", jobs=jobs, error=error, refresh=10 if busy else 0,
                      suffixes=",".join(AUDIO_SUFFIXES), max_mb=settings.max_upload_mb)

    @app.post("/jobs")
    def upload(file: UploadFile = File(...), title: str = Form("")):
        name = Path(file.filename or "recording").name
        if Path(name).suffix.lower() not in AUDIO_SUFFIXES:
            return RedirectResponse("/?error=" + "지원하지 않는 형식입니다: " + Path(name).suffix, status_code=303)
        uploads = Path(settings.data_dir) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        target = uploads / name
        limit = settings.max_upload_mb * 1024 * 1024
        written = 0
        with target.open("wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    out.close()
                    target.unlink(missing_ok=True)
                    return RedirectResponse("/?error=파일이 너무 큽니다 (%dMB 한도)" % settings.max_upload_mb, status_code=303)
                out.write(chunk)
        if written == 0:
            target.unlink(missing_ok=True)
            return RedirectResponse("/?error=빈 파일입니다", status_code=303)
        db = open_db()
        try:
            job = pipeline.new_job(settings, db, target, title.strip() or name, move=True)
        finally:
            db.close()
        worker.enqueue(job.id)
        return RedirectResponse("/jobs/%s" % job.id, status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str, error: str = ""):
        db = open_db()
        try:
            job = load_job(db, job_id)
            events = db.events(job_id)
        finally:
            db.close()
        minutes = job.minutes_dict or {}
        folder = pipeline.job_dir(settings, job_id)
        has_transcript = (folder / "transcript.txt").is_file()
        pending = worker.is_pending(job_id)
        return render(
            request, "job.html", job=job, events=events, minutes=minutes, error=error,
            has_transcript=has_transcript, pending=pending,
            refresh=5 if (job.status in BUSY or pending) else 0,
            extra_topics=1, extra_actions=3,
            publish=job.publish,
        )

    @app.get("/jobs/{job_id}/transcript", response_class=PlainTextResponse)
    def transcript(job_id: str):
        path = pipeline.job_dir(settings, job_id) / "transcript.txt"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="전사가 아직 없음")
        return path.read_text(encoding="utf-8")

    @app.post("/jobs/{job_id}/approve")
    async def approve(request: Request, job_id: str):
        form = {k: str(v) for k, v in (await request.form()).items()}
        db = open_db()
        try:
            job = load_job(db, job_id)
            try:
                minutes, choices = parse_review(form)
            except MinutesFormatError as error:
                return RedirectResponse("/jobs/%s?error=%s" % (job_id, error), status_code=303)
            try:
                pipeline.approve(db, job.id, minutes, choices)
            except pipeline.PipelineError as error:
                return RedirectResponse("/jobs/%s?error=%s" % (job_id, error), status_code=303)
        finally:
            db.close()
        if form.get("action") == "publish":
            worker.enqueue(job_id)
        return RedirectResponse("/jobs/%s" % job_id, status_code=303)

    @app.post("/jobs/{job_id}/retry")
    def retry(job_id: str):
        db = open_db()
        try:
            job = load_job(db, job_id)
        finally:
            db.close()
        if pipeline.next_step(job) is None:
            return RedirectResponse("/jobs/%s?error=다시 할 단계가 없음" % job_id, status_code=303)
        worker.enqueue(job_id)
        return RedirectResponse("/jobs/%s" % job_id, status_code=303)

    @app.post("/jobs/{job_id}/delete")
    def delete(job_id: str):
        db = open_db()
        try:
            job = load_job(db, job_id)
            if job.status in BUSY or worker.is_pending(job_id):
                return RedirectResponse("/jobs/%s?error=진행 중인 작업은 지울 수 없음" % job_id, status_code=303)
            db.delete(job_id)
        finally:
            db.close()
        shutil.rmtree(pipeline.job_dir(settings, job_id), ignore_errors=True)
        return RedirectResponse("/", status_code=303)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if settings.web_host not in ("127.0.0.1", "localhost"):
        log.warning("WEB_HOST=%s: Thor 밖에서도 접속할 수 있는 설정. 로그인이 없으니 127.0.0.1 을 권함.", settings.web_host)
    uvicorn.run(create_app(settings), host=settings.web_host, port=settings.web_port, log_level="info")
