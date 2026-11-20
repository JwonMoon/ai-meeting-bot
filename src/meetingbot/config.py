"""설정 로딩. 비밀 값은 .env 파일과 환경 변수에서만 읽는다."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


def read_env_file(path: Path) -> Dict[str, str]:
    """KEY=VALUE 형식의 .env 파일을 읽는다. 빈 줄과 # 주석은 건너뛴다."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    confluence_base_url: str
    confluence_token: str
    confluence_space_key: str
    confluence_parent_page_id: str
    jira_base_url: str
    jira_token: str
    jira_project_key: str
    jira_issue_type_id: str
    teams_webhook_url: str
    ollama_base_url: str
    ollama_model: str
    ollama_num_ctx: int
    # 음성 인식 (stt.py 설명 참고)
    stt_command: str = ""
    stt_path_map: str = ""
    stt_language: str = ""
    stt_max_new_tokens: int = 400
    stt_chunk_seconds: float = 60.0
    stt_silence_db: float = -40.0
    stt_min_silence_seconds: float = 0.7
    stt_chunk_timeout_seconds: int = 600
    stt_slow_factor: float = 2.0
    # 작업 기록과 녹음 파일을 두는 폴더
    data_dir: str = ""
    # ffmpeg, ffprobe 가 있는 폴더. 비우면 PATH 에서 찾는다.
    ffmpeg_dir: str = ""
    # 화면 (web/). Thor 안에서만 듣는다.
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    max_upload_mb: int = 500
    # 공용 GPU 양보 (gpu.py 설명 참고)
    gpu_busy_utilization: int = 20
    gpu_wait_minutes: int = 30
    gpu_poll_seconds: int = 30

    def require(self, *names: str) -> None:
        """이름으로 지정한 설정이 비어 있으면 어느 것이 비었는지 알려 준다."""
        missing: List[str] = [n.upper() for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError(".env 에 값이 없음: " + ", ".join(missing))


def load_settings(env_path: Optional[Path] = None) -> Settings:
    """환경 변수가 .env 파일보다 우선한다."""
    file_values = read_env_file(env_path or REPO_ROOT / ".env")

    def get(key: str) -> str:
        return os.environ.get(key, file_values.get(key, "")).strip()

    return Settings(
        confluence_base_url=get("CONFLUENCE_BASE_URL").rstrip("/"),
        confluence_token=get("CONFLUENCE_TOKEN"),
        confluence_space_key=get("CONFLUENCE_SPACE_KEY"),
        confluence_parent_page_id=get("CONFLUENCE_PARENT_PAGE_ID"),
        jira_base_url=get("JIRA_BASE_URL").rstrip("/"),
        jira_token=get("JIRA_TOKEN"),
        jira_project_key=get("JIRA_PROJECT_KEY"),
        jira_issue_type_id=get("JIRA_ISSUE_TYPE_ID"),
        teams_webhook_url=get("TEAMS_WEBHOOK_URL"),
        ollama_base_url=(get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/"),
        ollama_model=get("OLLAMA_MODEL"),
        ollama_num_ctx=_to_int("OLLAMA_NUM_CTX", get("OLLAMA_NUM_CTX") or "16384"),
        stt_command=get("STT_COMMAND"),
        stt_path_map=get("STT_PATH_MAP"),
        stt_language=get("STT_LANGUAGE"),
        stt_max_new_tokens=_to_int("STT_MAX_NEW_TOKENS", get("STT_MAX_NEW_TOKENS") or "400"),
        stt_chunk_seconds=_to_float("STT_CHUNK_SECONDS", get("STT_CHUNK_SECONDS") or "60"),
        stt_silence_db=_to_float("STT_SILENCE_DB", get("STT_SILENCE_DB") or "-40"),
        stt_min_silence_seconds=_to_float("STT_MIN_SILENCE_SECONDS", get("STT_MIN_SILENCE_SECONDS") or "0.7"),
        stt_chunk_timeout_seconds=_to_int("STT_CHUNK_TIMEOUT_SECONDS", get("STT_CHUNK_TIMEOUT_SECONDS") or "600"),
        stt_slow_factor=_to_float("STT_SLOW_FACTOR", get("STT_SLOW_FACTOR") or "2"),
        data_dir=get("DATA_DIR") or str(REPO_ROOT / "data"),
        ffmpeg_dir=get("FFMPEG_DIR").rstrip("/"),
        web_host=get("WEB_HOST") or "127.0.0.1",
        web_port=_to_int("WEB_PORT", get("WEB_PORT") or "8000"),
        max_upload_mb=_to_int("MAX_UPLOAD_MB", get("MAX_UPLOAD_MB") or "500"),
        gpu_busy_utilization=_to_int("GPU_BUSY_UTILIZATION", get("GPU_BUSY_UTILIZATION") or "20"),
        gpu_wait_minutes=_to_int("GPU_WAIT_MINUTES", get("GPU_WAIT_MINUTES") or "30"),
        gpu_poll_seconds=_to_int("GPU_POLL_SECONDS", get("GPU_POLL_SECONDS") or "30"),
    )


def _to_int(key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ConfigError("%s 는 숫자여야 함: %r" % (key, value)) from None


def _to_float(key: str, value: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ConfigError("%s 는 숫자여야 함: %r" % (key, value)) from None
