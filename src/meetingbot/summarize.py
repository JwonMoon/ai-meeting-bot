"""요약 단계: 전사 텍스트를 로컬 LLM(Ollama)에 넣어 회의록으로 만든다.

회의 내용은 Thor 밖으로 나가지 않는다.

요청 형식의 근거 (공식 문서):
- /api/chat, /api/tags, /api/version, format(JSON 스키마), options, keep_alive:
  https://github.com/ollama/ollama/blob/main/docs/api.md
- 스키마를 프롬프트에도 적고 temperature 를 0으로 두라는 권장:
  https://docs.ollama.com/capabilities/structured-outputs
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .config import Settings
from .httpclient import ApiError, failure, request
from .models import Minutes, MinutesFormatError

# 긴 전사는 처리에 몇 분 걸릴 수 있다.
CHAT_TIMEOUT_SECONDS = 1800

MINUTES_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "discussion": {"type": "string"},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic", "discussion", "decisions"],
            },
        },
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "assignee": {"type": ["string", "null"]},
                    "due_date": {"type": ["string", "null"]},
                },
                "required": ["task", "assignee", "due_date"],
            },
        },
    },
    "required": ["title", "summary", "topics", "action_items"],
}

SYSTEM_PROMPT = """너는 회의록 작성 담당이다. 회의 음성을 받아 적은 전사 텍스트를 읽고 회의록을 만든다.

규칙:
- 전사에 실제로 나온 내용만 쓴다. 추측하거나 지어내지 않는다.
- 전사는 음성 인식 결과라 오타와 잘못 들은 단어가 섞여 있다. 문맥으로 뜻이 분명할 때만 바로잡는다.
- action_items 에는 누군가 하기로 한 일만 넣는다. 없으면 빈 배열로 둔다.
- assignee 는 전사에서 담당자가 분명할 때만 이름을 적고, 아니면 null 로 둔다.
- due_date 는 전사에서 기한이 언급됐을 때만 언급된 표현 그대로 적고, 아니면 null 로 둔다.
- 한국어로 쓴다.
- 아래 JSON 스키마에 맞는 JSON 하나만 출력한다.

JSON 스키마:
"""


class SummarizeError(Exception):
    pass


@dataclass(frozen=True)
class SummaryResult:
    minutes: Minutes
    elapsed_seconds: float
    prompt_tokens: Optional[int]
    output_tokens: Optional[int]
    load_seconds: Optional[float]
    read_seconds: Optional[float]
    write_seconds: Optional[float]
    num_ctx: int

    @property
    def near_context_limit(self) -> bool:
        """입력이 한도에 닿았으면 전사 일부가 잘렸을 수 있다."""
        return self.prompt_tokens is not None and self.prompt_tokens >= self.num_ctx * 0.95


def _seconds(nanoseconds: Any) -> Optional[float]:
    # Ollama 는 시간을 나노초로 돌려준다.
    return nanoseconds / 1e9 if isinstance(nanoseconds, (int, float)) else None


def _get_json(url: str) -> Dict[str, Any]:
    status, text = request("GET", url, timeout=10)
    if status != 200:
        raise failure("Ollama 조회", status, text)
    return json.loads(text)


def server_info(settings: Settings) -> Tuple[str, List[str]]:
    """(Ollama 버전, 설치된 모델 이름 목록)."""
    base = settings.ollama_base_url
    version = _get_json(base + "/api/version").get("version", "")
    models = [m.get("name", "") for m in _get_json(base + "/api/tags").get("models", [])]
    return version, models


def summarize(settings: Settings, transcript: str, model: Optional[str] = None) -> SummaryResult:
    model = model or settings.ollama_model
    if not model:
        raise SummarizeError("모델 미지정. .env 의 OLLAMA_MODEL 을 채울 것.")
    transcript = transcript.strip()
    if not transcript:
        raise SummarizeError("전사 내용이 비어 있음.")

    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT + json.dumps(MINUTES_SCHEMA, ensure_ascii=False),
            },
            {"role": "user", "content": "전사 텍스트:\n\n" + transcript},
        ],
        "stream": False,
        "format": MINUTES_SCHEMA,
        "options": {"temperature": 0, "num_ctx": settings.ollama_num_ctx},
        # 끝나면 모델을 GPU에서 바로 내려 다른 사람이 쓸 수 있게 한다.
        "keep_alive": 0,
    }
    started = time.monotonic()
    status, text = request(
        "POST", settings.ollama_base_url + "/api/chat", body=body, timeout=CHAT_TIMEOUT_SECONDS
    )
    elapsed = time.monotonic() - started
    if status != 200:
        raise failure("회의록 생성", status, text)

    try:
        reply = json.loads(text)
        content = reply.get("message", {}).get("content", "")
        minutes = Minutes.from_dict(json.loads(content))
    except json.JSONDecodeError:
        raise SummarizeError("모델 응답이 JSON이 아님. 앞부분: " + text[:400]) from None
    except MinutesFormatError as error:
        raise SummarizeError("모델 응답이 회의록 형식과 다름: %s" % error) from None

    return SummaryResult(
        minutes=minutes,
        elapsed_seconds=elapsed,
        prompt_tokens=reply.get("prompt_eval_count"),
        output_tokens=reply.get("eval_count"),
        load_seconds=_seconds(reply.get("load_duration")),
        read_seconds=_seconds(reply.get("prompt_eval_duration")),
        write_seconds=_seconds(reply.get("eval_duration")),
        num_ctx=settings.ollama_num_ctx,
    )


__all__ = ["ApiError", "SummarizeError", "SummaryResult", "server_info", "summarize"]
