"""공용 GPU 양보: 다른 사람이 GPU 를 쓰고 있으면 비기를 기다린다.

Thor 에서 확인된 것: nvidia-smi 의 GPU 사용률과 프로세스 목록 조회는 된다.
전체 메모리 사용량(memory.used)은 Jetson 에서 지원되지 않아 쓰지 않는다.

명령 형식의 근거: nvidia-smi 설명서 (`nvidia-smi --help-query-gpu`, `--help-query-compute-apps`).
    nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits        → "12"
    nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader      → "1234, /usr/bin/ollama" 줄들
미확인: Thor 에서 두 번째 명령이 컨테이너 안 프로세스도 보여 주는지. 첫 실행 때 확인.
"""

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import Settings

log = logging.getLogger(__name__)


class GpuBusy(Exception):
    """기다려도 GPU 가 비지 않았다."""


@dataclass
class GpuStatus:
    available: bool  # nvidia-smi 가 있고 답했는지
    utilization: Optional[int] = None  # %
    processes: List[str] = field(default_factory=list)

    def busy(self, threshold: int) -> bool:
        if not self.available:
            return False
        if self.utilization is not None and self.utilization > threshold:
            return True
        return bool(self.processes)

    def describe(self) -> str:
        if not self.available:
            return "nvidia-smi 없음 (확인 생략)"
        return "사용률 %s%%, 프로세스 %d개%s" % (
            self.utilization if self.utilization is not None else "?",
            len(self.processes),
            (": " + ", ".join(self.processes[:5])) if self.processes else "",
        )


def parse_utilization(text: str) -> Optional[int]:
    for line in text.splitlines():
        token = line.strip().rstrip("%").strip()
        if token.isdigit():
            return int(token)
    return None


def parse_processes(text: str) -> List[str]:
    names: List[str] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            names.append("%s(pid %s)" % (parts[1] or "?", parts[0]))
    return names


def _smi(*args: str) -> Optional[str]:
    try:
        done = subprocess.run(
            ["nvidia-smi"] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log.warning("nvidia-smi 실행 실패: %s", error)
        return None
    if done.returncode != 0:
        log.warning("nvidia-smi 오류: %s", done.stderr.strip()[:200])
        return None
    return done.stdout


def query() -> GpuStatus:
    if shutil.which("nvidia-smi") is None:
        return GpuStatus(available=False)
    utilization = _smi("--query-gpu=utilization.gpu", "--format=csv,noheader,nounits")
    processes = _smi("--query-compute-apps=pid,process_name", "--format=csv,noheader")
    if utilization is None and processes is None:
        return GpuStatus(available=False)
    return GpuStatus(
        available=True,
        utilization=parse_utilization(utilization or ""),
        processes=parse_processes(processes or ""),
    )


def wait_until_free(
    settings: Settings,
    progress: Callable[[str], None] = log.info,
    query_fn: Optional[Callable[[], GpuStatus]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> GpuStatus:
    """GPU 가 비면 바로 돌아온다. 바쁘면 주기적으로 다시 보고, 한도를 넘기면 GpuBusy."""
    deadline = time.monotonic() + settings.gpu_wait_minutes * 60
    while True:
        status = (query_fn or query)()
        if not status.busy(settings.gpu_busy_utilization):
            return status
        if time.monotonic() >= deadline:
            raise GpuBusy(
                "GPU 가 %d분 동안 비지 않음 (%s). 나중에 재시도." % (settings.gpu_wait_minutes, status.describe())
            )
        progress("GPU 사용 중 (%s). %d초 뒤 다시 확인." % (status.describe(), settings.gpu_poll_seconds))
        sleep(settings.gpu_poll_seconds)
