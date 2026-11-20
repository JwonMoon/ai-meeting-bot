#!/usr/bin/env python3
"""음성 인식 일꾼: Qwen3-ASR 로 wav 파일들을 글로 바꿔 한 줄에 하나씩 JSON 으로 찍는다.

이 파일은 봇 전용 컨테이너(qwen-asr 설치됨) 안에서 돈다. 모델을 한 번만 올리고
넘겨받은 파일을 차례로 처리하므로 stt.py 의 {audios} 방식과 짝을 이룬다.
Thor 호스트 쪽(stt.py)은 이 파일을 import 하지 않는다. torch 가 없어도 된다.

사용법 (컨테이너 안에서):
    python3 asr_worker.py --language Korean --max-new-tokens 400 /data/.../001.wav /data/.../002.wav

출력 (표준 출력, 파일마다 한 줄):
    {"file": "/data/.../001.wav", "text": "...", "language": "Korean", "seconds": 3.2}
진행 메시지와 오류는 표준 오류로 보낸다.

API 근거 (공식 README, 2026-11-20 확인): https://github.com/QwenLM/Qwen3-ASR
    Qwen3ASRModel.from_pretrained(model, dtype=..., device_map=..., max_inference_batch_size=..., max_new_tokens=...)
    model.transcribe(audio=<경로|URL|(np.ndarray, sr)>, language=None|"English"|...) -> [result.text, result.language]
설치: pip install -U qwen-asr   (PyPI qwen-asr 0.0.6, 2026-01-30)
미확인: Thor(Jetson, aarch64) 에서 설치와 실행이 되는지. 처음 실행 때 결과를 보고 판단.
"""

import argparse
import json
import sys
import time


def log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen3-ASR 로 wav 파일들을 전사")
    parser.add_argument("files", nargs="+", help="wav 파일 경로들 (컨테이너 안에서 보이는 경로)")
    parser.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B", help="모델 이름 또는 받아 둔 폴더")
    parser.add_argument("--language", default="", help="언어 강제 (Korean, English). 비우면 자동")
    parser.add_argument("--max-new-tokens", type=int, default=400, help="파일 하나당 생성 토큰 상한")
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    try:
        import torch  # noqa: F401
        from qwen_asr import Qwen3ASRModel
    except ImportError as error:
        log("qwen-asr 또는 torch 가 없음: %s. 컨테이너 안에서 `pip install -U qwen-asr`" % error)
        return 2

    started = time.monotonic()
    log("모델 올리는 중: %s" % args.model)
    model = Qwen3ASRModel.from_pretrained(
        args.model,
        dtype=torch.bfloat16,
        device_map=args.device,
        max_inference_batch_size=1,  # 공용 GPU 라 메모리를 적게 쓴다
        max_new_tokens=args.max_new_tokens,
    )
    log("모델 올림: %.1f초" % (time.monotonic() - started))

    language = args.language or None
    failed = 0
    for path in args.files:
        t0 = time.monotonic()
        try:
            results = model.transcribe(audio=path, language=language)
            result = results[0]
            record = {
                "file": path,
                "text": (result.text or "").strip(),
                "language": getattr(result, "language", None),
                "seconds": round(time.monotonic() - t0, 2),
            }
        except Exception as error:  # noqa: BLE001 - 한 파일 실패가 전체를 멈추지 않게
            failed += 1
            record = {"file": path, "text": "", "error": str(error)[:300], "seconds": round(time.monotonic() - t0, 2)}
            log("실패 %s: %s" % (path, error))
        sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    log("끝: 파일 %d개, 실패 %d개, 총 %.1f초" % (len(args.files), failed, time.monotonic() - started))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
