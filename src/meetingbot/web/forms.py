"""검토 화면의 입력(form)을 회의록과 게시 선택으로 바꾼다. FastAPI 없이도 돌아 시험이 쉽다.

입력 이름 규칙:
    title, summary
    topic_<n>_topic, topic_<n>_discussion, topic_<n>_decisions   (decisions 는 한 줄에 하나)
    action_<n>_task, action_<n>_assignee, action_<n>_due
    parent_page_id, space_key, project_key, create_jira(체크), send_teams(체크)
비어 있는 안건(topic 비움)과 비어 있는 할 일(task 비움)은 버린다. 그래서 화면에 빈 줄을 미리 몇 개 둘 수 있다.
"""

import re
from typing import Dict, List, Mapping, Tuple

from ..models import ActionItem, Minutes, MinutesFormatError, Topic
from ..pipeline import PublishChoices

_ROW = re.compile(r"^(topic|action)_(\d+)_([a-z]+)$")


def _rows(form: Mapping[str, str], kind: str) -> List[Dict[str, str]]:
    grouped: Dict[int, Dict[str, str]] = {}
    for key, value in form.items():
        found = _ROW.match(key)
        if found and found.group(1) == kind:
            grouped.setdefault(int(found.group(2)), {})[found.group(3)] = (value or "").strip()
    return [grouped[n] for n in sorted(grouped)]


def _lines(text: str) -> List[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_minutes(form: Mapping[str, str]) -> Minutes:
    title = (form.get("title") or "").strip()
    summary = (form.get("summary") or "").strip()
    if not title:
        raise MinutesFormatError("제목이 비어 있음")
    if not summary:
        raise MinutesFormatError("요약이 비어 있음")
    topics = [
        Topic(topic=row["topic"], discussion=row.get("discussion", ""), decisions=_lines(row.get("decisions", "")))
        for row in _rows(form, "topic")
        if row.get("topic")
    ]
    actions = [
        ActionItem(task=row["task"], assignee=row.get("assignee") or None, due_date=row.get("due") or None)
        for row in _rows(form, "action")
        if row.get("task")
    ]
    return Minutes(title=title, summary=summary, topics=topics, action_items=actions)


def parse_choices(form: Mapping[str, str]) -> PublishChoices:
    return PublishChoices(
        parent_page_id=(form.get("parent_page_id") or "").strip(),
        space_key=(form.get("space_key") or "").strip(),
        project_key=(form.get("project_key") or "").strip(),
        create_jira=bool(form.get("create_jira")),
        send_teams=bool(form.get("send_teams")),
    )


def parse_review(form: Mapping[str, str]) -> Tuple[Minutes, PublishChoices]:
    return parse_minutes(form), parse_choices(form)
