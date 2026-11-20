#!/usr/bin/env python3
"""화면 서버 켜기. Thor 안(127.0.0.1:8000)에서만 듣는다.

준비 (Thor, 저장소 폴더에서, 처음 한 번):
    python3 -m venv .venv
    .venv/bin/pip install -r requirements-web.txt
실행:
    .venv/bin/python scripts/run_web.py
노트북에서 보기 (PowerShell, 터널을 연 채로 브라우저에서 http://localhost:8000):
    ssh -L 8000:127.0.0.1:8000 <봇계정>@<Thor주소>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot.web.app import main  # noqa: E402

if __name__ == "__main__":
    main()
