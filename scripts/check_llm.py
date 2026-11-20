#!/usr/bin/env python3
"""LLM 점검: 전사 텍스트 파일을 로컬 LLM(Ollama)에 넣어 회의록이 나오는지 확인한다.

봇이 실제로 쓰는 코드(src/meetingbot/summarize.py)를 그대로 호출한다.
추가 설치 없이 파이썬 기본 기능만 쓴다. 회의 내용은 Thor 밖으로 나가지 않는다.

사용법 (저장소 폴더에서):
    python3 scripts/check_llm.py ~/meetingbot-test/korean.txt
    python3 scripts/check_llm.py ~/meetingbot-test/korean.txt --model <모델이름>
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot.config import ConfigError, load_settings  # noqa: E402
from meetingbot.summarize import ApiError, SummarizeError, server_info, summarize  # noqa: E402


def _fmt(seconds: Optional[float]) -> str:
    return "%.1f초" % seconds if seconds is not None else "(정보 없음)"


def main() -> int:
    parser = argparse.ArgumentParser(description="로컬 LLM으로 회의록 생성 점검")
    parser.add_argument("transcript", type=Path, help="전사 텍스트 파일 (.txt)")
    parser.add_argument("--model", help="모델 이름. 비우면 .env 의 OLLAMA_MODEL")
    parser.add_argument("--output", type=Path, help="결과 JSON을 저장할 파일")
    args = parser.parse_args()

    if not args.transcript.is_file():
        print("전사 파일이 없음: %s" % args.transcript)
        return 1
    transcript = args.transcript.read_text(encoding="utf-8")

    try:
        settings = load_settings()
        model = args.model or settings.ollama_model
        version, models = server_info(settings)
        print("Ollama %s | 설치된 모델: %s" % (version, ", ".join(models) or "(없음)"))
        if model and model not in models:
            print("모델 '%s' 이(가) 설치돼 있지 않음. 위 목록의 이름을 그대로 쓸 것." % model)
            return 1
        print("모델 %s 에 전사 %d자 전달. 응답 대기 중..." % (model, len(transcript.strip())))
        result = summarize(settings, transcript, model)
    except (ConfigError, SummarizeError, ApiError) as error:
        print("실패: %s" % error)
        return 1

    print(
        "완료: %.1f초 | 입력 %s 토큰 | 출력 %s 토큰"
        % (result.elapsed_seconds, result.prompt_tokens, result.output_tokens)
    )
    print(
        "시간 내역: 모델 올리기 %s | 전사 읽기 %s | 회의록 쓰기 %s"
        % (_fmt(result.load_seconds), _fmt(result.read_seconds), _fmt(result.write_seconds))
    )
    if result.write_seconds and isinstance(result.output_tokens, int):
        print("쓰기 속도: 초당 %.1f 토큰" % (result.output_tokens / result.write_seconds))
    if result.near_context_limit:
        print(
            "경고: 입력이 한도(OLLAMA_NUM_CTX=%d)에 닿음. 전사 일부가 잘렸을 수 있음. 값을 늘려 재시도."
            % result.num_ctx
        )

    pretty = json.dumps(result.minutes.to_dict(), ensure_ascii=False, indent=2)
    print("\n" + pretty)
    if args.output:
        args.output.write_text(pretty + "\n", encoding="utf-8")
        print("\n저장: %s" % args.output)
    print(
        "\n통과: 안건 %d개, 액션아이템 %d개. 내용이 전사와 맞는지는 직접 읽고 판단."
        % (len(result.minutes.topics), len(result.minutes.action_items))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
