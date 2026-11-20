"""Jira (사내 설치형) 이슈 생성.

요청 형식의 근거 (공식 문서):
https://developer.atlassian.com/server/jira/platform/jira-rest-api-examples/

담당자와 기한은 Jira 의 전용 칸에 넣지 않고 설명에 글로 적는다.
생성 요청에서 그 칸들의 값 형식을 공식 문서로 확인하지 못했고,
회의에서 부른 이름을 Jira 계정으로 바꾸는 대응표도 아직 없기 때문이다.
"""

import json
from dataclasses import dataclass
from typing import Optional

from ..config import Settings
from ..httpclient import failure, request
from ..models import ActionItem

SUMMARY_MAX_CHARS = 250


@dataclass(frozen=True)
class Issue:
    key: str
    url: str


def create_issue(
    settings: Settings,
    summary: str,
    description: str,
    project_key: Optional[str] = None,
    issue_type_id: Optional[str] = None,
) -> Issue:
    """부모 없는 독립 이슈를 만든다. 위치를 안 주면 .env 의 기본값을 쓴다."""
    settings.require("jira_base_url", "jira_token")
    body = {
        "fields": {
            "project": {"key": project_key or settings.jira_project_key},
            # 제목은 한 줄이어야 한다.
            "summary": " ".join(summary.split())[:SUMMARY_MAX_CHARS],
            "description": description,
            "issuetype": {"id": issue_type_id or settings.jira_issue_type_id},
        }
    }
    status, text = request(
        "POST", settings.jira_base_url + "/rest/api/2/issue/", settings.jira_token, body
    )
    if status not in (200, 201):
        raise failure("Jira 이슈 생성", status, text)
    key = json.loads(text).get("key", "")
    return Issue(key=key, url="%s/browse/%s" % (settings.jira_base_url, key))


def action_item_description(item: ActionItem, meeting_title: str, page_url: str) -> str:
    lines = [
        "회의록에서 만들어진 액션아이템입니다.",
        "",
        "회의: " + meeting_title,
        "회의록: " + (page_url or "(주소 없음)"),
        "담당자(회의에서 언급): " + (item.assignee or "미정"),
        "기한(회의에서 언급): " + (item.due_date or "미정"),
    ]
    return "\n".join(lines)
