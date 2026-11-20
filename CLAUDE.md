# AI 회의록 봇

Claude Code가 이 저장소에서 작업을 이어갈 때 읽는 안내문이다. 상태가 바뀌면 "현재 상태"를 고친다.

## 무엇을 만드나

회의 녹음 파일 하나로 회의록 작성, Confluence 등록, Jira 티켓 생성, Teams 알림까지 처리하는 사내 도구.

```
노트북(녹음, 업로드, 검토)  →  사내 GPU 장비 "Thor"(음성 인식, 요약, 등록)  →  Confluence, Jira, Teams
```

흐름: 업로드 → 음성 인식 → 요약 → 사람 검토·승인 → Confluence 페이지 → Jira 티켓 → Teams 알림.
승인 전에는 외부로 아무것도 보내지 않는다. 게시 위치(Confluence 상위 페이지, Jira 프로젝트)는 검토 화면에서 고른다.

## 실행 환경

- 코드는 Thor(Jetson AGX Thor, Ubuntu, Python 3.12)의 봇 전용 계정에서 돈다. 개발은 다른 컴퓨터에서 하고 git으로 옮긴다.
- **Claude는 Thor에 접속할 수 없다.** Thor에서 실행할 명령은 사용자에게 안내하고, 사용자가 붙여 준 출력을 해석한다.
- 음성 인식: Qwen3-ASR 1.7B. 다른 사용자(minhojo)의 Docker 컨테이너 `minho_qwen_asr` 를 허락받아 쓰는 중이다. 원본 스크립트와 폴더는 고치지 않는다.
  - 그 컨테이너는 `/home/minhojo/work` 만 `/workspace` 로 본다. 스크립트 `extract_subtitles.py` 는 폴더 단위로 동영상(mp4 등)을 받아 `.txt` 로 저장하며,
    qwen-asr 의 `split_audio_into_chunks`(60초)와 `max_new_tokens=4096` 을 쓴다. 이 4096 이 말 없는 구간에서 5분씩 걸린 원인으로 보인다.
  - 모델 가중치는 이미지 안이 아니라 `/home/minhojo/work/models/Qwen3-ASR-1.7B` (호스트 폴더)에 있다.
  - 봇은 그 컨테이너를 직접 쓰지 않고, 같은 이미지 `minho_qwen_asr:backup-20261111` 로 봇 전용 컨테이너를 매번 새로 띄워
    `scripts/asr_worker.py` 를 돌린다 (`.env.example` 의 STT_COMMAND). 모델 폴더는 읽기 전용 마운트. minhojo 님 동의 필요.
- 요약: Ollama, 봇 전용 컨테이너(포트 11435, Thor 자신만 접속 가능). 다른 사용자의 Ollama(11434)는 쓰지 않는다.
- Confluence와 Jira는 사내 설치형(Server/Data Center)이다. Cloud API가 아니다. 인증은 Personal Access Token을 Bearer 헤더로 보낸다.
- ffmpeg: Thor 의 `/usr/bin/ffmpeg` 는 mp3 해독 부품이 없는 빌드다 (공용이라 안 고침). 봇 계정 홈 `~/ffmpeg` 에
  johnvansickle.com 의 arm64 정적 빌드를 풀어 두고 `.env` 의 `FFMPEG_DIR` 로 가리킨다. 2026-11-20 확인.
- 주소, 계정, 페이지 ID 같은 환경 값은 저장소에 적지 않는다. Thor의 `.env`와, 있으면 `CLAUDE.local.md`(git 제외)를 본다. 없으면 사용자에게 묻는다.

## 코드 구조

| 경로 | 역할 |
|---|---|
| `src/meetingbot/config.py` | `.env` 읽기 |
| `src/meetingbot/models.py` | 회의록 데이터 모양(Minutes, Topic, ActionItem)과 검증 |
| `src/meetingbot/audio.py` | ffmpeg 호출: wav 변환, 말 없는 구간 찾기, 구간 나누기 |
| `src/meetingbot/stt.py` | 녹음 → 전사. 음성 인식 명령(`STT_COMMAND`)을 구간마다 또는 한 번에 실행 |
| `src/meetingbot/summarize.py` | 전사 → 회의록 (Ollama) |
| `src/meetingbot/gpu.py` | nvidia-smi 로 GPU 사용 여부 확인, 비기를 기다림 |
| `src/meetingbot/db.py` | 작업 상태 기록 (SQLite, `DATA_DIR/meetingbot.sqlite3`) |
| `src/meetingbot/pipeline.py` | 단계 흐름과 재시도. 승인 뒤에만 게시 |
| `src/meetingbot/integrations/` | Confluence, Jira, Teams 호출 |
| `src/meetingbot/httpclient.py` | HTTP 공통. 오류 메시지에 토큰과 전체 주소를 넣지 않는다 |
| `scripts/check_integrations.py` | Thor에서 실제 페이지·티켓·메시지를 하나씩 만들어 보는 점검 |
| `scripts/check_llm.py` | Thor에서 전사 파일을 요약해 보는 점검 |
| `scripts/check_stt.py` | Thor에서 녹음 파일 하나를 전사해 보는 점검 (`--plan-only`로 자르기만) |
| `scripts/asr_worker.py` | 봇 전용 컨테이너 안에서 돌 Qwen3-ASR 일꾼. 모델 한 번 올려 여러 wav 처리 |
| `scripts/jobs.py` | 화면이 생기기 전까지 쓰는 명령줄: new, list, show, run, approve, publish |
| `tests/` | 네트워크 없이 도는 단위 시험 |

시험: `python3 -m unittest discover -s tests`

## 작업 규칙

- **확인이 구현보다 먼저.** 외부 API 형식은 공식 문서로 확인하고 출처를 코드 주석에 적는다. 확인하지 못한 것은 "미확인"이라고 밝히고 Thor 실행 결과로 검증한다.
- **비밀 값.** 토큰과 webhook URL은 `.env`에만 둔다. 사용자에게 채팅에 붙이지 말라고 안내하고, 붙였으면 교체를 권한다. `.env` 확인은 값을 가린 출력으로 한다.
- **공용 장비.** Thor는 여러 사람이 쓴다. 남의 컨테이너, 폴더, 모델을 고치지 않는다. GPU는 시작 전 사용 여부 확인, 한 번에 한 건, 처리 후 즉시 반환.
- **의존성.** 파이썬 쪽은 기본 기능만 쓴다. 음성 처리에 ffmpeg(시스템 패키지)가 필요하다. 화면(FastAPI)을 만들 때 처음으로 pip 설치가 필요해진다.
- **호환.** 개발 컴퓨터의 Python이 3.9라서 `X | None` 같은 3.10 이상 문법을 쓰지 않는다.
- **설명 방식.** 사용자는 이 분야가 처음이다. 쉬운 말로 설명하고, 명령마다 어느 컴퓨터(노트북 PowerShell, Thor)에서 치는지 적고, 결과 읽는 법을 같이 준다.
- **git.** 작업 브랜치는 `integration-checks`. Thor는 이 브랜치를 clone해서 `git pull`로 받는다.

## 현재 상태 (2026-11-20)

완료:
- 사전 확인: 노트북 → Thor 접속, Thor → Confluence·Jira·Teams 접속과 인증
- 음성 인식: 영어, 한국어 음원으로 동작 확인 (기존 스크립트 그대로)
- 요약: Ollama 봇 전용 컨테이너에서 회의록 형식 출력 확인
- 연동: 시험 페이지 생성·삭제, 시험 티켓 생성, Teams 메시지 수신 확인
- 본 구현 1번: 요약·연동 부품을 `src/meetingbot/`으로 정리, 단위 시험 15개
- 본 구현 2번 (코드만, Thor 미검증): `audio.py`, `stt.py`, `check_stt.py`, `asr_worker.py`.
  mp3·m4a·wav 입력, 말 없는 구간은 모델에 넣지 않음, 구간당 생성 한도(`STT_MAX_NEW_TOKENS`), 지연 경고와 시간 초과
- 본 구현 3번 (코드만, Thor 미검증): `db.py`, `gpu.py`, `pipeline.py`, `jobs.py`.
  실패 단계부터 재시도, Jira 티켓 중복 생성 방지, GPU 사용 중이면 대기. 단위 시험 50개 통과.
  가짜 음성 인식·가짜 Ollama 로 new → approve → publish 흐름 확인

바로 다음:
1. Thor에서 재확인 (`git pull` 후): 단위 시험, `check_integrations.py`, `check_llm.py`, 요약 직후 모델이 GPU에서 내려갔는지
2. Thor에서 음성 인식 연결: `.env.example` 의 STT_COMMAND 예시를 `.env` 에 넣고
   `check_stt.py` 로 확인 (`--plan-only` 는 통과: korean.mp3 가 구간 10개로 나뉨). 구간 수와 느린 구간 보고 `STT_SILENCE_DB` 조정.
   `nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader` 가 컨테이너 프로세스를 보여 주는지 확인
3. Thor에서 `jobs.py new <회의 녹음>` 으로 전체 흐름 한 번. 액션아이템 추출 품질 확인
4. 화면 `web/`: 업로드, 진행 상황, 검토·승인 (FastAPI, Thor 내부에서만 열림, 노트북은 SSH 터널로 접속).
   `pipeline.py` 의 `new_job`, `run_to_review`, `approve`, `publish` 를 그대로 부른다. 첫 외부 의존성 설치
5. 다듬기: 긴 회의 분할 요약, 자동 시작, 보관 기간 지난 녹음 삭제, README

미해결:
- `asr_worker.py` 를 minhojo 님 이미지로 띄운 컨테이너에서 돌리는 것은 미확인. 첫 실행에서 모델 올리는 시간과 구간당 시간을 기록할 것
- 모델 가중치를 봇 계정 폴더로 복사해 두면 minhojo 님 폴더 의존이 없어진다 (약 4GB). 동의 받은 뒤 진행
- 말 없는 구간 판정 기준(`STT_SILENCE_DB=-40`, `STT_MIN_SILENCE_SECONDS=0.7`)은 실제 회의실 녹음으로 맞춰야 함
- 액션아이템 추출 미검증 (시험 음원이 회의가 아니었음). 실제 회의 녹음으로 확인하기로 함
- 실제 회의실 녹음(노트북 마이크, 여러 명) 품질 미검증
- 1시간 회의는 요약 입력 한도(`OLLAMA_NUM_CTX`)에 닿을 것으로 추정. 분할 요약 필요 (`pipeline._summarize` 에서 처리)
- Jira 담당자·기한 전용 칸의 입력 형식 미확인. 지금은 티켓 설명에 글로 적음. 이름 → Jira 계정 대응표 없음
- GPU 양보는 사용률과 프로세스 목록만 본다. 컨테이너 안 프로세스가 목록에 안 보이면 사용률만으로 판단하게 됨
- 음성 인식을 남의 컨테이너에서 돌리는 중. 봇 전용 컨테이너로 분리 필요
- 이 저장소가 공개 상태로 보임. 비공개 전환 시 Thor에 읽기 전용 deploy key 필요
- 개인 토큰은 시험용. 팀 운영 전 봇 전용 계정 토큰으로 교체
