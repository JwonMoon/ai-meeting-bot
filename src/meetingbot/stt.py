"""음성 인식 단계: 녹음 파일 하나를 전사 텍스트로 만든다.

흐름: 어떤 형식이든 wav 로 변환 → 말 없는 구간 찾기 → 말소리만 1분 안팎 묶음으로 자르기
→ 묶음마다 음성 인식 명령 실행 → 시각 표시를 붙여 한 텍스트로 합치기.

음성 인식 명령은 .env 의 STT_COMMAND 로 정한다 (지금은 다른 사용자의 Docker 컨테이너 안
스크립트를 불러 쓰므로 명령이 환경마다 다르다). 자리표시자:
    {audio}           잘라 낸 wav 한 개의 경로 → 묶음마다 명령을 한 번씩 실행
    {audios}          잘라 낸 wav 전부의 경로 → 명령을 한 번만 실행하고, 표준 출력에서
                      한 줄에 하나씩 {"file": ..., "text": ...} JSON 을 읽음 (scripts/asr_worker.py 형식).
                      모델을 한 번만 올리므로 훨씬 빠르다
    {language}        STT_LANGUAGE 값 (예: Korean). 비어 있으면 빈 글자
    {max_new_tokens}  STT_MAX_NEW_TOKENS 값. 묶음 하나에서 모델이 만들 수 있는 글자 수 상한

말 없는 구간에서 음성 인식 한 묶음이 약 5분 걸리던 문제에 대한 대비:
- 말 없는 구간은 아예 모델에 넣지 않는다.
- 묶음을 짧게(기본 60초) 잡고, 묶음당 생성 한도를 낮게 둔다.
- 묶음 처리 시간이 음성 길이의 STT_SLOW_FACTOR 배를 넘으면 경고를 남기고,
  STT_CHUNK_TIMEOUT_SECONDS 를 넘으면 중단한다.
"""

import json
import logging
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import audio
from .audio import AudioError, Chunk, format_timestamp
from .config import ConfigError, Settings

log = logging.getLogger(__name__)


class SttError(Exception):
    pass


@dataclass
class Segment:
    start: float
    end: float
    text: str
    elapsed_seconds: float = 0.0
    slow: bool = False

    @property
    def seconds(self) -> float:
        return self.end - self.start


@dataclass
class Transcript:
    source: str
    duration_seconds: float
    speech_seconds: float
    segments: List[Segment] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def text(self) -> str:
        """요약 단계에 넘기는 글. 말소리 묶음마다 시작 시각을 앞에 붙인다."""
        lines = ["[%s] %s" % (format_timestamp(s.start), s.text) for s in self.segments if s.text]
        return "\n".join(lines)

    @property
    def slow_segments(self) -> List[Segment]:
        return [s for s in self.segments if s.slow]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Transcript":
        segments = [Segment(**s) for s in data.get("segments", [])]
        return cls(
            source=data["source"],
            duration_seconds=data["duration_seconds"],
            speech_seconds=data["speech_seconds"],
            segments=segments,
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
        )


# ---------------------------------------------------------------- 명령 만들기


def map_path(path: Path, path_map: str) -> str:
    """STT_PATH_MAP="호스트경로:컨테이너경로" 에 따라 컨테이너 안에서 보이는 경로로 바꾼다."""
    if not path_map:
        return str(path)
    host_prefix, _, inside_prefix = path_map.partition(":")
    if not inside_prefix:
        raise ConfigError("STT_PATH_MAP 은 '호스트경로:컨테이너경로' 형식이어야 함")
    host_prefix = host_prefix.rstrip("/")
    text = str(path)
    if text == host_prefix or text.startswith(host_prefix + "/"):
        return inside_prefix.rstrip("/") + text[len(host_prefix):]
    raise ConfigError("%s 는 STT_PATH_MAP 의 호스트 경로(%s) 아래에 없음" % (text, host_prefix))


def build_command(settings: Settings, audio_paths: Sequence[str]) -> Tuple[List[str], bool]:
    """(실행할 명령, 한 번에 여러 파일을 넘기는지)."""
    if not settings.stt_command:
        raise ConfigError(".env 에 값이 없음: STT_COMMAND")
    values = {
        "language": settings.stt_language,
        "max_new_tokens": str(settings.stt_max_new_tokens),
    }
    batch = False
    command: List[str] = []
    for token in shlex.split(settings.stt_command):
        if token == "{audios}":
            command.extend(audio_paths)
            batch = True
            continue
        if "{audio}" in token:
            if len(audio_paths) != 1:
                raise ConfigError("{audio} 는 파일 하나씩만 받음")
            token = token.replace("{audio}", audio_paths[0])
        for key, value in values.items():
            token = token.replace("{%s}" % key, value)
        command.append(token)
    if not batch and not any("{audio}" in t for t in shlex.split(settings.stt_command)):
        raise ConfigError("STT_COMMAND 에 {audio} 또는 {audios} 자리표시자가 있어야 함")
    return command, batch


def is_batch(settings: Settings) -> bool:
    """명령이 모든 구간을 한 번에 받는지 ({audios})."""
    if not settings.stt_command:
        raise ConfigError(".env 에 값이 없음: STT_COMMAND")
    tokens = shlex.split(settings.stt_command)
    if "{audios}" in tokens:
        return True
    if not any("{audio}" in t for t in tokens):
        raise ConfigError("STT_COMMAND 에 {audio} 또는 {audios} 자리표시자가 있어야 함")
    return False


def parse_text_output(stdout: str) -> str:
    """파일 하나짜리 명령의 출력. JSON 한 줄이면 text 항목을, 아니면 전체를 글로 본다."""
    stripped = stdout.strip()
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "text" in data:
                return str(data["text"]).strip()
    return stripped


# ---------------------------------------------------------------- 명령 실행


def _slow(elapsed: float, chunk: Chunk, settings: Settings) -> bool:
    return elapsed > max(30.0, chunk.seconds * settings.stt_slow_factor)


def _run_per_chunk(
    settings: Settings, pieces: Sequence[Tuple[Chunk, Path]], path_map: str
) -> Iterator[Segment]:
    for chunk, path in pieces:
        command, _ = build_command(settings, [map_path(path, path_map)])
        started = time.monotonic()
        try:
            done = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=settings.stt_chunk_timeout_seconds,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            raise SttError(
                "구간 %s~%s 음성 인식이 %d초 안에 끝나지 않음. 컨테이너 안 프로세스는 아직 돌고 있을 수 있음 (docker exec 는 끊어도 안쪽은 안 죽음)."
                % (format_timestamp(chunk.start), format_timestamp(chunk.end), settings.stt_chunk_timeout_seconds)
            ) from None
        except OSError as error:
            raise SttError("음성 인식 명령 실행 실패: %s" % error) from None
        elapsed = time.monotonic() - started
        if done.returncode != 0:
            raise SttError(
                "음성 인식 명령이 오류로 끝남 (코드 %d). 오류 출력 끝부분: %s"
                % (done.returncode, done.stderr.strip()[-400:])
            )
        yield Segment(chunk.start, chunk.end, parse_text_output(done.stdout), elapsed, _slow(elapsed, chunk, settings))


def _reader(stream, out: "queue.Queue[Optional[str]]") -> None:
    for line in stream:
        out.put(line)
    out.put(None)


def _run_batch(
    settings: Settings, pieces: Sequence[Tuple[Chunk, Path]], path_map: str
) -> Iterator[Segment]:
    by_name = {path.name: chunk for chunk, path in pieces}
    command, _ = build_command(settings, [map_path(p, path_map) for _, p in pieces])
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise SttError("음성 인식 명령 실행 실패: %s" % error) from None

    lines: "queue.Queue[Optional[str]]" = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, lines), daemon=True).start()
    errors: List[str] = []
    threading.Thread(target=lambda: errors.extend(proc.stderr.readlines()), daemon=True).start()

    last = time.monotonic()
    seen = 0
    try:
        while True:
            try:
                line = lines.get(timeout=settings.stt_chunk_timeout_seconds)
            except queue.Empty:
                proc.kill()
                raise SttError(
                    "음성 인식 명령이 %d초 동안 결과를 내지 않음 (%d/%d 구간 완료)."
                    % (settings.stt_chunk_timeout_seconds, seen, len(pieces))
                ) from None
            if line is None:
                break
            line = line.strip()
            if not line.startswith("{"):
                if line:
                    log.info("음성 인식 출력: %s", line)
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log.info("음성 인식 출력: %s", line)
                continue
            name = Path(str(data.get("file", ""))).name
            chunk = by_name.get(name)
            if chunk is None:
                log.warning("모르는 파일의 결과: %s", name)
                continue
            now = time.monotonic()
            elapsed = float(data.get("seconds", now - last))
            last = now
            seen += 1
            yield Segment(chunk.start, chunk.end, str(data.get("text", "")).strip(), elapsed, _slow(elapsed, chunk, settings))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    if proc.returncode != 0:
        raise SttError(
            "음성 인식 명령이 오류로 끝남 (코드 %d). 오류 출력 끝부분: %s"
            % (proc.returncode, "".join(errors).strip()[-400:])
        )
    if seen < len(pieces):
        raise SttError("음성 인식 결과가 모자람: %d/%d 구간" % (seen, len(pieces)))


# ---------------------------------------------------------------- 전체 흐름

Progress = Callable[[str], None]


def prepare(settings: Settings, source: Path, work_dir: Path, progress: Progress = log.info) -> Tuple[Path, float, List[Chunk]]:
    """wav 변환과 구간 계획까지. (wav 경로, 전체 길이, 구간 목록)."""
    audio.configure(settings.ffmpeg_dir)
    audio.ensure_ffmpeg()
    if not source.is_file():
        raise SttError("녹음 파일이 없음: %s" % source)
    if source.suffix.lower() not in audio.AUDIO_SUFFIXES:
        raise SttError("지원하지 않는 형식 %s. 가능: %s" % (source.suffix, ", ".join(audio.AUDIO_SUFFIXES)))
    work_dir.mkdir(parents=True, exist_ok=True)

    progress("wav 로 변환 중...")
    wav = audio.to_wav(source, work_dir / "audio.wav")
    # 길이는 변환된 wav 로 잰다. mp3 는 파일 머리의 길이 정보가 실제와 조금 다를 수 있다.
    duration = audio.probe_duration(wav)
    silences = audio.detect_silences(wav, duration, settings.stt_silence_db, settings.stt_min_silence_seconds)
    chunks = audio.plan_chunks(
        duration, silences, settings.stt_chunk_seconds, settings.stt_chunk_seconds * 1.5
    )
    speech = sum(c.seconds for c in chunks)
    progress(
        "말소리 %s / 전체 %s, 구간 %d개 (말 없는 구간 %d곳 제외)"
        % (format_timestamp(speech), format_timestamp(duration), len(chunks), len(silences))
    )
    return wav, duration, chunks


def transcribe(settings: Settings, source: Path, work_dir: Path, progress: Progress = log.info) -> Transcript:
    """녹음 파일 → Transcript. work_dir 에 wav 와 구간 파일이 남는다 (재시도와 확인용)."""
    started = time.monotonic()
    wav, duration, chunks = prepare(settings, source, work_dir, progress)
    transcript = Transcript(source=str(source), duration_seconds=duration, speech_seconds=sum(c.seconds for c in chunks))
    if not chunks:
        progress("말소리가 없음. 음성 인식을 건너뜀.")
        transcript.elapsed_seconds = time.monotonic() - started
        return transcript

    chunk_dir = work_dir / "chunks"
    chunk_dir.mkdir(exist_ok=True)
    pieces: List[Tuple[Chunk, Path]] = []
    for index, chunk in enumerate(chunks, 1):
        pieces.append((chunk, audio.cut(wav, chunk, chunk_dir / ("%03d.wav" % index))))

    runner = _run_batch if is_batch(settings) else _run_per_chunk
    progress("음성 인식 시작 (%s)" % ("한 번에 전부" if runner is _run_batch else "구간마다 명령 실행"))
    for index, segment in enumerate(runner(settings, pieces, settings.stt_path_map), 1):
        transcript.segments.append(segment)
        note = " ← 느림" if segment.slow else ""
        progress(
            "[%d/%d] %s~%s (%.0f초 분량) %.1f초 걸림%s: %s"
            % (index, len(pieces), format_timestamp(segment.start), format_timestamp(segment.end),
               segment.seconds, segment.elapsed_seconds, note, segment.text[:60])
        )
        if segment.slow:
            log.warning(
                "구간 %s 처리가 음성 길이의 %.1f배 걸림. 말 없는 구간을 지어내고 있을 수 있음. STT_SILENCE_DB 를 올리거나 STT_MAX_NEW_TOKENS 를 줄여 볼 것.",
                format_timestamp(segment.start), segment.elapsed_seconds / max(segment.seconds, 1),
            )
    transcript.elapsed_seconds = time.monotonic() - started
    return transcript


__all__ = ["AudioError", "Segment", "SttError", "Transcript", "build_command", "is_batch", "map_path", "prepare", "transcribe"]
