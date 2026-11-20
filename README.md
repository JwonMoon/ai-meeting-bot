# AI 회의록 봇

회의 녹음 파일 하나로 회의록 작성, Confluence 등록, Jira 티켓 생성, Teams 알림까지 처리하는 사내 도구.

```
노트북(녹음, 업로드, 검토)  →  Thor(음성 인식, 요약, 등록)  →  Confluence, Jira, Teams
```

업로드 → 음성 인식 → 회의록 초안 → **사람 검토·승인** → Confluence 페이지 → Jira 티켓 → Teams 알림.
승인 전에는 밖으로 아무것도 보내지 않는다.

## Thor 에서 준비 (처음 한 번)

```bash
git clone <저장소> ai-meeting-bot && cd ai-meeting-bot
cp .env.example .env && chmod 600 .env && nano .env     # 값 채우기. 토큰은 이 파일에만
python3 -m venv .venv && .venv/bin/pip install -r requirements-web.txt   # 화면용. 나머지는 기본 파이썬만 씀
python3 -m unittest discover -s tests                   # OK 가 나와야 함
```

필요한 것: ffmpeg(`FFMPEG_DIR`), 음성 인식 컨테이너 이미지(`STT_COMMAND`), Ollama 봇 컨테이너(`OLLAMA_BASE_URL`). 자세한 것은 `.env.example` 의 설명과 `CLAUDE.md`.

## 화면 켜기

```bash
.venv/bin/python scripts/run_web.py        # Thor 안 127.0.0.1:8000 에서만 듣는다
```

노트북(PowerShell)에서 터널을 열고 브라우저로 `http://localhost:8000`:

```powershell
ssh -L 8000:127.0.0.1:8000 <봇계정>@<Thor주소>
```

터널 창을 닫으면 화면도 닫힌다. 로그인은 없다. Thor 에 SSH 로 들어올 수 있는 사람만 쓴다는 전제다.

## 화면 없이 명령줄로

```bash
python3 scripts/jobs.py new <녹음파일>       # 작업 만들고 검토 대기까지
python3 scripts/jobs.py show <작업ID>        # 상태, 진행 기록, 회의록 초안
python3 scripts/jobs.py approve <작업ID> [--minutes 고친.json] [--parent 페이지ID] [--project KEY] [--no-jira] [--no-teams]
python3 scripts/jobs.py publish <작업ID>     # Confluence → Jira → Teams
python3 scripts/jobs.py run <작업ID>         # 실패한 단계부터 다시
```

## 부품별 점검

```bash
python3 scripts/check_stt.py <녹음파일> [--plan-only]   # 음성 인식만
python3 scripts/check_llm.py <전사.txt>                 # 요약만
python3 scripts/check_integrations.py [confluence|jira|teams]
```

## 파일 위치

`DATA_DIR`(기본 `data/`) 아래: `meetingbot.sqlite3`(작업 기록), `jobs/<작업ID>/`(녹음 원본, `audio.wav`, `chunks/`, `transcript.txt`, `minutes.json`).
