#!/usr/bin/env python3
"""LLM 점검: 전사 텍스트 파일을 로컬 LLM(Ollama)에 넣어 회의록 JSON이 나오는지 확인한다.

추가 설치 없이 파이썬 기본 기능만 쓴다. 회의 내용은 Thor 밖으로 나가지 않는다.

사용법 (저장소 폴더에서):
    python3 scripts/check_llm.py ~/meetingbot-test/korean.txt
    python3 scripts/check_llm.py ~/meetingbot-test/korean.txt --model <모델이름>

요청 형식의 근거 (공식 문서):
- /api/chat, /api/tags, /api/version, format(JSON 스키마), options, keep_alive:
  https://github.com/ollama/ollama/blob/main/docs/api.md
- 스키마를 프롬프트에도 적고 temperature 를 0으로 두라는 권장:
  https://docs.ollama.com/capabilities/structured-outputs
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot.config import ConfigError, load_settings  # noqa: E402

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


def call(method: str, url: str, body: Optional[Dict[str, Any]], timeout: int) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _seconds(nanoseconds: Any) -> Optional[float]:
    return nanoseconds / 1e9 if isinstance(nanoseconds, (int, float)) else None


def _fmt(seconds: Optional[float]) -> str:
    return "%.1f초" % seconds if seconds is not None else "(정보 없음)"


def main() -> int:
    parser = argparse.ArgumentParser(description="로컬 LLM으로 회의록 JSON 생성 점검")
    parser.add_argument("transcript", type=Path, help="전사 텍스트 파일 (.txt)")
    parser.add_argument("--model", help="모델 이름. 비우면 .env 의 OLLAMA_MODEL")
    parser.add_argument("--output", type=Path, help="결과 JSON을 저장할 파일")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as error:
        print("설정 문제: %s" % error)
        return 1
    model = args.model or settings.ollama_model
    base = settings.ollama_base_url

    if not args.transcript.is_file():
        print("전사 파일이 없음: %s" % args.transcript)
        return 1
    transcript = args.transcript.read_text(encoding="utf-8").strip()
    if not transcript:
        print("전사 파일이 비어 있음: %s" % args.transcript)
        return 1

    # 1) 서버 연결
    try:
        version = call("GET", base + "/api/version", None, 10).get("version")
        models = [m.get("name") for m in call("GET", base + "/api/tags", None, 10).get("models", [])]
    except (urllib.error.URLError, OSError) as error:
        print("Ollama 서버에 연결 실패 (%s): %s" % (base, error))
        print("Ollama 컨테이너가 떠 있는지, 포트 11434가 열려 있는지 확인.")
        return 1
    print("Ollama %s | 설치된 모델: %s" % (version, ", ".join(models) or "(없음)"))

    # 2) 모델 확인
    if not model:
        print("모델 미지정. .env 의 OLLAMA_MODEL 을 채우거나 --model 로 지정.")
        return 1
    if model not in models:
        print("모델 '%s' 이(가) 설치돼 있지 않음. 위 목록의 이름을 그대로 쓸 것." % model)
        return 1

    # 3) 회의록 생성
    schema_text = json.dumps(MINUTES_SCHEMA, ensure_ascii=False)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + schema_text},
            {"role": "user", "content": "전사 텍스트:\n\n" + transcript},
        ],
        "stream": False,
        "format": MINUTES_SCHEMA,
        "options": {"temperature": 0, "num_ctx": settings.ollama_num_ctx},
    }
    print("모델 %s 에 전사 %d자 전달. 응답 대기 중..." % (model, len(transcript)))
    started = time.monotonic()
    try:
        reply = call("POST", base + "/api/chat", body, CHAT_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        print("실패: HTTP %s. %s" % (error.code, error.read().decode("utf-8", "replace")[:600]))
        return 1
    except (urllib.error.URLError, OSError) as error:
        print("실패: %s" % error)
        return 1
    elapsed = time.monotonic() - started

    prompt_tokens = reply.get("prompt_eval_count")
    output_tokens = reply.get("eval_count")
    print(
        "완료: %.1f초 | 입력 %s 토큰 | 출력 %s 토큰 | 종료 사유 %s"
        % (elapsed, prompt_tokens, output_tokens, reply.get("done_reason"))
    )
    # 어디서 시간이 걸렸는지 나눠 본다. Ollama 는 시간을 나노초로 돌려준다.
    load = _seconds(reply.get("load_duration"))
    read = _seconds(reply.get("prompt_eval_duration"))
    write = _seconds(reply.get("eval_duration"))
    print(
        "시간 내역: 모델 올리기 %s | 전사 읽기 %s | 회의록 쓰기 %s"
        % (_fmt(load), _fmt(read), _fmt(write))
    )
    if write and isinstance(output_tokens, int):
        print("쓰기 속도: 초당 %.1f 토큰" % (output_tokens / write))
    if isinstance(prompt_tokens, int) and prompt_tokens >= settings.ollama_num_ctx * 0.95:
        print(
            "경고: 입력이 한도(OLLAMA_NUM_CTX=%d)에 닿음. 전사 일부가 잘렸을 수 있음. 값을 늘려 재시도."
            % settings.ollama_num_ctx
        )

    # 4) 결과 검사
    content = reply.get("message", {}).get("content", "")
    try:
        minutes = json.loads(content)
    except json.JSONDecodeError:
        print("실패: 응답이 JSON이 아님. 앞부분:\n" + content[:800])
        return 1
    missing = [key for key in MINUTES_SCHEMA["required"] if key not in minutes]
    if missing:
        print("실패: 응답에 빠진 항목: %s" % ", ".join(missing))
        return 1

    pretty = json.dumps(minutes, ensure_ascii=False, indent=2)
    print("\n" + pretty)
    if args.output:
        args.output.write_text(pretty + "\n", encoding="utf-8")
        print("\n저장: %s" % args.output)
    print(
        "\n통과: 안건 %d개, 액션아이템 %d개. 내용이 전사와 맞는지는 직접 읽고 판단."
        % (len(minutes["topics"]), len(minutes["action_items"]))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
