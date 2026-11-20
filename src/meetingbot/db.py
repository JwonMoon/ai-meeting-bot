"""작업 기록: 녹음 하나가 어느 단계까지 왔는지 SQLite 에 적어 둔다.

파일 하나(DATA_DIR/meetingbot.sqlite3)에 담기고, 파이썬 기본 sqlite3 모듈만 쓴다.
단계 흐름과 재시도 규칙은 pipeline.py 에 있다. 여기는 읽고 쓰기만 한다.
"""

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 상태 값. 화면과 pipeline 이 같은 글자를 쓴다.
UPLOADED = "uploaded"          # 녹음 받음
TRANSCRIBING = "transcribing"  # 음성 인식 중
TRANSCRIBED = "transcribed"    # 전사 완료
SUMMARIZING = "summarizing"    # 요약 중
REVIEW = "review"              # 회의록 초안 완료. 사람 검토 대기
APPROVED = "approved"          # 승인됨. 게시 대기
PUBLISHING = "publishing"      # Confluence, Jira, Teams 진행 중
PUBLISHED = "published"        # 끝
FAILED = "failed"              # 어느 단계에서 실패. step 에 단계 이름, error 에 이유

STATUS_LABELS = {
    UPLOADED: "대기", TRANSCRIBING: "음성 인식 중", TRANSCRIBED: "전사 완료", SUMMARIZING: "요약 중",
    REVIEW: "검토 대기", APPROVED: "승인됨", PUBLISHING: "게시 중", PUBLISHED: "게시 완료", FAILED: "실패",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    status TEXT NOT NULL,
    step TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    transcript_path TEXT NOT NULL DEFAULT '',
    minutes_json TEXT NOT NULL DEFAULT '',
    publish_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    at TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    created_at: str
    updated_at: str
    title: str
    audio_path: str
    status: str
    step: str = ""
    error: str = ""
    transcript_path: str = ""
    minutes_json: str = ""
    publish_json: str = "{}"

    @property
    def minutes_dict(self) -> Optional[Dict[str, Any]]:
        return json.loads(self.minutes_json) if self.minutes_json else None

    @property
    def publish(self) -> Dict[str, Any]:
        """게시 선택과 진행 상황: parent_page_id, space_key, project_key, send_teams,
        confluence_page_id, confluence_url, jira_keys(list), teams_sent."""
        return json.loads(self.publish_json or "{}")

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ jobs

    def create_job(self, title: str, audio_path: str, job_id: Optional[str] = None) -> Job:
        stamp = now()
        job_id = job_id or (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2))
        with self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, created_at, updated_at, title, audio_path, status) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, stamp, stamp, title, audio_path, UPLOADED),
            )
        self.add_event(job_id, "녹음 받음: " + title)
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("작업이 없음: %s" % job_id)
        return Job(**dict(row))

    def list_jobs(self, limit: int = 50) -> List[Job]:
        rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [Job(**dict(r)) for r in rows]

    def update(self, job_id: str, **fields: Any) -> Job:
        """지정한 칸만 바꾼다. publish 는 dict 로 주면 JSON 으로 저장한다."""
        if "publish" in fields:
            fields["publish_json"] = json.dumps(fields.pop("publish"), ensure_ascii=False)
        allowed = {"title", "audio_path", "status", "step", "error", "transcript_path", "minutes_json", "publish_json"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("모르는 칸: %s" % ", ".join(sorted(unknown)))
        fields["updated_at"] = now()
        assignments = ", ".join("%s = ?" % k for k in fields)
        with self._conn:
            changed = self._conn.execute(
                "UPDATE jobs SET %s WHERE id = ?" % assignments, tuple(fields.values()) + (job_id,)
            ).rowcount
        if changed == 0:
            raise KeyError("작업이 없음: %s" % job_id)
        return self.get(job_id)

    def delete(self, job_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # ------------------------------------------------------------ events

    def add_event(self, job_id: str, message: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO events (job_id, at, message) VALUES (?, ?, ?)", (job_id, now(), message)
            )

    def events(self, job_id: str, limit: int = 200) -> List[Dict[str, str]]:
        rows = self._conn.execute(
            "SELECT at, message FROM events WHERE job_id = ? ORDER BY id DESC LIMIT ?", (job_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
