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
- 음성 인식: Qwen3-ASR 1.7B. 다른 사용자의 Docker 컨테이너를 허락받아 쓰는 중이다. 원본 스크립트와 폴더는 고치지 않는다.
- 요약: Ollama, 봇 전용 컨테이너(포트 11435, Thor 자신만 접속 가능). 다른 사용자의 Ollama(11434)는 쓰지 않는다.
- Confluence와 Jira는 사내 설치형(Server/Data Center)이다. Cloud API가 아니다. 인증은 Personal Access Token을 Bearer 헤더로 보낸다.
- 주소, 계정, 페이지 ID 같은 환경 값은 저장소에 적지 않는다. Thor의 `.env`와, 있으면 `CLAUDE.local.md`(git 제외)를 본다. 없으면 사용자에게 묻는다.

## 코드 구조

| 경로 | 역할 |
|---|---|
| `src/meetingbot/config.py` | `.env` 읽기 |
| `src/meetingbot/models.py` | 회의록 데이터 모양(Minutes, Topic, ActionItem)과 검증 |
| `src/meetingbot/summarize.py` | 전사 → 회의록 (Ollama) |
| `src/meetingbot/integrations/` | Confluence, Jira, Teams 호출 |
| `src/meetingbot/httpclient.py` | HTTP 공통. 오류 메시지에 토큰과 전체 주소를 넣지 않는다 |
| `scripts/check_integrations.py` | Thor에서 실제 페이지·티켓·메시지를 하나씩 만들어 보는 점검 |
| `scripts/check_llm.py` | Thor에서 전사 파일을 요약해 보는 점검 |
| `tests/` | 네트워크 없이 도는 단위 시험 |

시험: `python3 -m unittest discover -s tests`

## 작업 규칙

- **확인이 구현보다 먼저.** 외부 API 형식은 공식 문서로 확인하고 출처를 코드 주석에 적는다. 확인하지 못한 것은 "미확인"이라고 밝히고 Thor 실행 결과로 검증한다.
- **비밀 값.** 토큰과 webhook URL은 `.env`에만 둔다. 사용자에게 채팅에 붙이지 말라고 안내하고, 붙였으면 교체를 권한다. `.env` 확인은 값을 가린 출력으로 한다.
- **공용 장비.** Thor는 여러 사람이 쓴다. 남의 컨테이너, 폴더, 모델을 고치지 않는다. GPU는 시작 전 사용 여부 확인, 한 번에 한 건, 처리 후 즉시 반환.
- **의존성.** 지금까지는 파이썬 기본 기능만 썼다. 화면(FastAPI)을 만들 때 처음으로 설치가 필요해진다.
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

바로 다음:
1. Thor에서 정리된 부품 재확인: `git pull` 후 단위 시험, `check_integrations.py`, `check_llm.py`, 요약 직후 모델이 GPU에서 내려갔는지
2. 음성 인식 부품 `stt.py`: mp3·m4a·wav 직접 입력, 말 없는 구간 대비(구간당 생성 한도 축소, 지연 경고)
3. 작업 관리 `db.py`, `pipeline.py`: 상태 저장(SQLite), 실패 단계부터 재시도, GPU 사용 중이면 대기
4. 화면 `web/`: 업로드, 진행 상황, 검토·승인 (FastAPI, Thor 내부에서만 열림, 노트북은 SSH 터널로 접속)
5. 다듬기: 긴 회의 분할 요약, 자동 시작, 보관 기간 지난 녹음 삭제, README

미해결:
- 액션아이템 추출 미검증 (시험 음원이 회의가 아니었음). 실제 회의 녹음으로 확인하기로 함
- 실제 회의실 녹음(노트북 마이크, 여러 명) 품질 미검증
- 말 없는 구간에서 음성 인식 한 묶음이 약 5분 걸린 현상. 원인은 추정(생성 한도까지 생성)
- 1시간 회의는 요약 입력 한도(`OLLAMA_NUM_CTX`)에 닿을 것으로 추정. 분할 요약 필요
- Jira 담당자·기한 전용 칸의 입력 형식 미확인. 지금은 티켓 설명에 글로 적음. 이름 → Jira 계정 대응표 없음
- GPU 충돌 방지 미구현. Thor에서 `nvidia-smi`의 사용률과 프로세스 목록 조회는 되고, 전체 메모리 사용량은 지원되지 않음
- 음성 인식을 남의 컨테이너에서 돌리는 중. 봇 전용 컨테이너로 분리 필요
- 이 저장소가 공개 상태로 보임. 비공개 전환 시 Thor에 읽기 전용 deploy key 필요
- 개인 토큰은 시험용. 팀 운영 전 봇 전용 계정 토큰으로 교체
