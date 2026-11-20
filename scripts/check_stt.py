#!/usr/bin/env python3
"""음성 인식 점검: 녹음 파일 하나를 전사해 보고 구간별 시간을 보여 준다.

봇이 실제로 쓰는 코드(src/meetingbot/stt.py)를 그대로 호출한다.
결과 전사(.txt)는 바로 check_llm.py 에 넣을 수 있다.

사용법 (Thor, 저장소 폴더에서):
    python3 scripts/check_stt.py ~/meetingbot-test/korean.mp3
    python3 scripts/check_stt.py ~/meetingbot-test/korean.mp3 --plan-only   # 자르기 계획만 보고 모델은 안 돌림
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot import stt  # noqa: E402
from meetingbot.audio import AudioError, format_timestamp  # noqa: E402
from meetingbot.config import ConfigError, load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="녹음 파일 음성 인식 점검")
    parser.add_argument("audio", type=Path, help="녹음 파일 (mp3, m4a, wav 등)")
    parser.add_argument("--work-dir", type=Path, help="wav 와 구간 파일을 둘 폴더. 기본 DATA_DIR/stt-check/<파일이름>")
    parser.add_argument("--plan-only", action="store_true", help="변환과 구간 나누기까지만 하고 음성 인식은 건너뜀")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    try:
        settings = load_settings()
        work_dir = args.work_dir or Path(settings.data_dir) / "stt-check" / args.audio.stem
        if args.plan_only:
            _, duration, chunks = stt.prepare(settings, args.audio, work_dir, print)
            for index, chunk in enumerate(chunks, 1):
                print("  구간 %3d: %s ~ %s (%.0f초)" % (index, format_timestamp(chunk.start), format_timestamp(chunk.end), chunk.seconds))
            print("\n구간 수가 너무 많거나(1분에 3개 넘게) 말이 잘려 나갔으면 .env 의 STT_SILENCE_DB, STT_MIN_SILENCE_SECONDS 를 조정.")
            return 0
        print("음성 인식 명령: %s" % settings.stt_command)
        transcript = stt.transcribe(settings, args.audio, work_dir, print)
    except (ConfigError, AudioError, stt.SttError) as error:
        print("실패: %s" % error)
        return 1

    txt = work_dir / "transcript.txt"
    txt.write_text(transcript.text + "\n", encoding="utf-8")
    (work_dir / "transcript.json").write_text(
        json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    speech = transcript.speech_seconds
    print(
        "\n완료: 전체 %s, 말소리 %s, 구간 %d개, 걸린 시간 %.0f초 (말소리 1분당 %.0f초)"
        % (
            format_timestamp(transcript.duration_seconds), format_timestamp(speech), len(transcript.segments),
            transcript.elapsed_seconds, transcript.elapsed_seconds / max(speech / 60, 0.01),
        )
    )
    if transcript.slow_segments:
        print("느린 구간 %d개 (음성 길이의 %.0f배 넘게 걸림):" % (len(transcript.slow_segments), settings.stt_slow_factor))
        for s in transcript.slow_segments:
            print("  %s~%s %.0f초 분량에 %.0f초. 글: %s" % (format_timestamp(s.start), format_timestamp(s.end), s.seconds, s.elapsed_seconds, s.text[:80]))
        print("  → 글이 같은 말을 반복하면 말 없는 구간을 지어낸 것. 해당 구간 wav 를 직접 들어 볼 것: %s" % (work_dir / "chunks"))
    empty = [s for s in transcript.segments if not s.text]
    if empty:
        print("글이 비어 있는 구간 %d개. 조용한 잡음이었을 수 있음." % len(empty))
    print("전사 저장: %s" % txt)
    print("다음 점검: python3 scripts/check_llm.py %s" % txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
