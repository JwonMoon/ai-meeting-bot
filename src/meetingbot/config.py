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
    )
