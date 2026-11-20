"""Confluence (사내 설치형) 페이지 생성.

요청 형식의 근거 (공식 문서):
https://developer.atlassian.com/server/confluence/confluence-rest-api-examples/
"""

import json
from dataclasses import dataclass
from html import escape
from typing import List, Optional

from ..config import Settings
from ..httpclient import failure, request
from ..models import Minutes


@dataclass(frozen=True)
class Page:
    id: str
    url: str


def create_page(
    settings: Settings,
    title: str,
    storage_html: str,
    parent_page_id: Optional[str] = None,
    space_key: Optional[str] = None,
) -> Page:
    """상위 페이지 아래에 하위 페이지를 만든다. 위치를 안 주면 .env 의 기본값을 쓴다."""
    settings.require("confluence_base_url", "confluence_token")
    body = {
        "type": "page",
        "title": title,
        "ancestors": [{"id": parent_page_id or settings.confluence_parent_page_id}],
        "space": {"key": space_key or settings.confluence_space_key},
        "body": {"storage": {"value": storage_html, "representation": "storage"}},
    }
    status, text = request(
        "POST", settings.confluence_base_url + "/rest/api/content/", settings.confluence_token, body
    )
    if status != 200:
        raise failure("Confluence 페이지 생성", status, text)
    page = json.loads(text)
    links = page.get("_links", {})
    return Page(id=str(page.get("id")), url=links.get("base", "") + links.get("webui", ""))


def delete_page(settings: Settings, page_id: str) -> None:
    settings.require("confluence_base_url", "confluence_token")
    status, text = request(
        "DELETE",
        settings.confluence_base_url + "/rest/api/content/" + page_id,
        settings.confluence_token,
    )
    if status != 204:
        raise failure("Confluence 페이지 삭제", status, text)


def minutes_to_storage(minutes: Minutes) -> str:
    """회의록을 Confluence 저장 형식(XHTML)으로 바꾼다. 모든 글자는 이스케이프한다."""
    parts: List[str] = ["<h2>요약</h2>", "<p>%s</p>" % escape(minutes.summary)]

    parts.append("<h2>안건</h2>")
    if not minutes.topics:
        parts.append("<p>없음</p>")
    for topic in minutes.topics:
        parts.append("<h3>%s</h3>" % escape(topic.topic))
        parts.append("<p>%s</p>" % escape(topic.discussion))
        if topic.decisions:
            items = "".join("<li>%s</li>" % escape(d) for d in topic.decisions)
            parts.append("<p><strong>결정 사항</strong></p><ul>%s</ul>" % items)

    parts.append("<h2>액션아이템</h2>")
    if not minutes.action_items:
        parts.append("<p>없음</p>")
    else:
        rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (escape(a.task), escape(a.assignee or "-"), escape(a.due_date or "-"))
            for a in minutes.action_items
        )
        parts.append(
            "<table><tbody><tr><th>할 일</th><th>담당자</th><th>기한</th></tr>%s</tbody></table>"
            % rows
        )
    return "".join(parts)
