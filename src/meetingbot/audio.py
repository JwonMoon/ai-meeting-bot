"""음성 파일 준비: 형식 변환, 길이 조회, 말 없는 구간 찾기, 구간 나누기.

모두 ffmpeg/ffprobe 를 불러 쓴다 (Thor 에 설치돼 있어야 함: `ffmpeg -version`).
파이썬 쪽에는 추가 설치가 없다.

명령 형식의 근거 (공식 문서):
- ffprobe -show_entries, -of: https://ffmpeg.org/ffprobe.html
- 변환 옵션 -ac, -ar, -c:a, -ss, -t: https://ffmpeg.org/ffmpeg.html
- silencedetect 필터 출력 형식: https://ffmpeg.org/ffmpeg-filters.html#silencedetect
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# 음성 인식 모델이 받는 형식. 모노 16kHz 16bit PCM wav.
SAMPLE_RATE = 16000

# 녹음 파일로 받아 주는 확장자.
AUDIO_SUFFIXES = (".mp3", ".m4a", ".wav", ".mp4", ".aac", ".ogg", ".flac", ".webm", ".wma")


class AudioError(Exception):
    pass


@dataclass(frozen=True)
class Chunk:
    """원본 기준 시작·끝(초). 음성 인식에 넣을 한 구간."""

    start: float
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start


def ensure_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise AudioError(
            "%s 가 없음. Thor 에서 설치: sudo apt install ffmpeg" % ", ".join(missing)
        )


def _run(command: Sequence[str], what: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise AudioError("%s 시간 초과 (%d초)" % (what, timeout)) from None
    except OSError as error:
        raise AudioError("%s 실행 실패: %s" % (what, error)) from None


def probe_duration(path: Path) -> float:
    """파일 길이(초)."""
    done = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        "길이 조회",
        timeout=120,
    )
    if done.returncode != 0:
        raise AudioError("길이 조회 실패 (%s): %s" % (path.name, done.stderr.strip()[:300]))
    try:
        return float(done.stdout.strip())
    except ValueError:
        raise AudioError("길이를 읽지 못함 (%s): %r" % (path.name, done.stdout[:100])) from None


def to_wav(source: Path, target: Path) -> Path:
    """mp3, m4a, wav 등 무엇이든 모노 16kHz wav 로 바꾼다."""
    target.parent.mkdir(parents=True, exist_ok=True)
    done = _run(
        [
            "ffmpeg", "-y", "-nostdin", "-v", "error",
            "-i", str(source),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
            str(target),
        ],
        "wav 변환",
    )
    if done.returncode != 0:
        raise AudioError("wav 변환 실패 (%s): %s" % (source.name, done.stderr.strip()[:300]))
    return target


def cut(source_wav: Path, chunk: Chunk, target: Path) -> Path:
    """wav 에서 한 구간을 잘라 파일로 저장한다."""
    done = _run(
        [
            "ffmpeg", "-y", "-nostdin", "-v", "error",
            "-i", str(source_wav),
            "-ss", "%.3f" % chunk.start, "-t", "%.3f" % chunk.seconds,
            "-c:a", "pcm_s16le",
            str(target),
        ],
        "구간 자르기",
        timeout=300,
    )
    if done.returncode != 0:
        raise AudioError("구간 자르기 실패 (%.1f~%.1f초): %s" % (chunk.start, chunk.end, done.stderr.strip()[:300]))
    return target


_SILENCE_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def parse_silences(ffmpeg_stderr: str, duration: float) -> List[Tuple[float, float]]:
    """silencedetect 출력에서 (시작, 끝) 목록을 뽑는다.

    파일 끝까지 조용하면 silence_end 가 안 찍히므로 파일 길이로 닫는다.
    """
    silences: List[Tuple[float, float]] = []
    start: Optional[float] = None
    for line in ffmpeg_stderr.splitlines():
        found_start = _SILENCE_START.search(line)
        if found_start:
            start = max(0.0, float(found_start.group(1)))
            continue
        found_end = _SILENCE_END.search(line)
        if found_end and start is not None:
            silences.append((start, min(duration, float(found_end.group(1)))))
            start = None
    if start is not None and start < duration:
        silences.append((start, duration))
    return silences


def detect_silences(wav: Path, duration: float, noise_db: float, min_seconds: float) -> List[Tuple[float, float]]:
    """noise_db(dB) 보다 작은 소리가 min_seconds 이상 이어지는 구간을 찾는다."""
    done = _run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-nostats",
            "-i", str(wav),
            "-af", "silencedetect=noise=%sdB:d=%s" % (noise_db, min_seconds),
            "-f", "null", "-",
        ],
        "말 없는 구간 찾기",
    )
    if done.returncode != 0:
        raise AudioError("말 없는 구간 찾기 실패: %s" % done.stderr.strip()[-300:])
    return parse_silences(done.stderr, duration)


def speech_regions(duration: float, silences: Sequence[Tuple[float, float]], pad: float) -> List[Tuple[float, float]]:
    """말 없는 구간을 뺀 나머지. 앞뒤로 pad 초씩 여유를 둬서 첫 음절이 잘리지 않게 한다.

    0.3초도 안 되는 토막은 소리라고 보지 않는다.
    """
    regions: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(silences):
        if start > cursor:
            regions.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        regions.append((cursor, duration))
    return [
        (max(0.0, s - pad), min(duration, e + pad))
        for s, e in regions
        if e - s >= 0.3
    ]


def plan_chunks(
    duration: float,
    silences: Sequence[Tuple[float, float]],
    target_seconds: float,
    max_seconds: float,
    pad: float = 0.2,
) -> List[Chunk]:
    """말소리 구간을 target_seconds 안팎의 묶음으로 나눈다.

    - 말 없는 구간은 어느 묶음에도 넣지 않는다 (모델이 없는 말을 지어내며 시간을 끄는 것을 막음).
    - 묶음은 말 없는 구간에서만 끊는다. 말이 max_seconds 넘게 이어지면 그때만 중간에서 끊는다.
    """
    if target_seconds <= 0 or max_seconds < target_seconds:
        raise ValueError("구간 길이 설정이 잘못됨: target=%s max=%s" % (target_seconds, max_seconds))

    chunks: List[Chunk] = []
    current: Optional[Tuple[float, float]] = None
    for start, end in speech_regions(duration, silences, pad):
        # 너무 긴 말소리 구간은 target 단위로 쪼갠다.
        pieces: List[Tuple[float, float]] = []
        pos = start
        while end - pos > max_seconds:
            pieces.append((pos, pos + target_seconds))
            pos += target_seconds
        pieces.append((pos, end))

        for piece in pieces:
            if current is None:
                current = piece
            elif piece[1] - current[0] <= max_seconds and (current[1] - current[0]) < target_seconds:
                current = (current[0], piece[1])
            else:
                chunks.append(Chunk(*current))
                current = piece
    if current is not None:
        chunks.append(Chunk(*current))
    return chunks


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    return "%02d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)
