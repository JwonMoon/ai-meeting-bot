"""회의록 데이터 모양. LLM 출력, 검토 화면, 등록 단계가 모두 이 모양을 쓴다."""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


class MinutesFormatError(ValueError):
    """회의록 데이터가 정해진 모양이 아닐 때."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise MinutesFormatError("%s 는 글자여야 함: %r" % (name, value))
    return value.strip()


def _optional_text(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    return _text(value, name) or None


def _list(value: Any, name: str) -> List[Any]:
    if not isinstance(value, list):
        raise MinutesFormatError("%s 는 목록이어야 함: %r" % (name, value))
    return value


def _dict(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise MinutesFormatError("%s 는 항목 묶음이어야 함: %r" % (name, value))
    return value


def _get(data: Dict[str, Any], key: str, name: str) -> Any:
    if key not in data:
        raise MinutesFormatError("%s 에 %s 항목이 없음" % (name, key))
    return data[key]


@dataclass
class ActionItem:
    task: str
    assignee: Optional[str] = None
    due_date: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any) -> "ActionItem":
        data = _dict(data, "action_item")
        return cls(
            task=_text(_get(data, "task", "action_item"), "task"),
            assignee=_optional_text(data.get("assignee"), "assignee"),
            due_date=_optional_text(data.get("due_date"), "due_date"),
        )


@dataclass
class Topic:
    topic: str
    discussion: str
    decisions: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> "Topic":
        data = _dict(data, "topic")
        return cls(
            topic=_text(_get(data, "topic", "topic"), "topic"),
            discussion=_text(_get(data, "discussion", "topic"), "discussion"),
            decisions=[_text(d, "decision") for d in _list(data.get("decisions", []), "decisions")],
        )


@dataclass
class Minutes:
    title: str
    summary: str
    topics: List[Topic] = field(default_factory=list)
    action_items: List[ActionItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> "Minutes":
        data = _dict(data, "minutes")
        return cls(
            title=_text(_get(data, "title", "minutes"), "title"),
            summary=_text(_get(data, "summary", "minutes"), "summary"),
            topics=[Topic.from_dict(t) for t in _list(_get(data, "topics", "minutes"), "topics")],
            action_items=[
                ActionItem.from_dict(a)
                for a in _list(_get(data, "action_items", "minutes"), "action_items")
            ],
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
