#!/usr/bin/env python3
"""연동 점검: Confluence 시험 페이지, Jira 시험 티켓, Teams 시험 메시지를 하나씩 만든다.

본 구현 전에 "API로 실제 생성이 되는가"만 확인하는 스크립트다.
추가 설치 없이 파이썬 기본 기능만 쓴다.

사용법 (저장소 폴더에서):
    python3 scripts/check_integrations.py                # 셋 다
    python3 scripts/check_integrations.py confluence     # 하나만
    python3 scripts/check_integrations.py jira teams     # 골라서

요청 형식의 근거 (공식 문서):
- Confluence 하위 페이지 생성·삭제:
  https://developer.atlassian.com/server/confluence/confluence-rest-api-examples/
- Jira 이슈 생성:
  https://developer.atlassian.com/server/jira/platform/jira-rest-api-examples/
- Personal Access Token 을 Bearer 헤더로 전송:
  https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html
- Teams Workflows webhook 카드 형식:
  https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot.config import ConfigError, Settings, load_settings  # noqa: E402

TIMEOUT_SECONDS = 30


def call(
    method: str,
    url: str,
    token: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str]:
    """HTTP 요청을 보내고 (상태 코드, 응답 본문)을 돌려준다. 토큰은 출력하지 않는다."""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def explain_failure(status: int, text: str) -> None:
    hints = {
        400: "요청 형식을 서버가 거절함. 아래 응답에 이유가 적혀 있음.",
        401: "토큰이 틀렸거나 만료됨.",
        403: "토큰은 맞지만 이 위치에 만들 권한이 없음.",
        404: "주소나 ID가 틀림 (BASE_URL, 페이지 ID, 프로젝트 키 확인).",
    }
    print("  실패: HTTP %s. %s" % (status, hints.get(status, "")))
    print("  응답 앞부분: " + text[:600].replace("\n", " "))


def ask_yes(prompt: str) -> bool:
    """Y/n 질문. 그냥 Enter 는 Y 로 본다.

    한글 입력 상태에서 글자를 지우면 터미널이 깨진 바이트를 보낼 수 있다.
    그래도 죽지 않도록 바이트로 읽어서 해석한다. 한글 상태의 Y, N 키(ㅛ, ㅜ)도 받는다.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    answer = sys.stdin.buffer.readline().decode("utf-8", errors="ignore").strip().lower()
    return answer in ("", "y", "yes", "ㅛ")


def check_confluence(settings: Settings, stamp: str) -> bool:
    print("\n[Confluence] 시험 페이지 생성")
    settings.require(
        "confluence_base_url",
        "confluence_token",
        "confluence_space_key",
        "confluence_parent_page_id",
    )
    body = {
        "type": "page",
        "title": "[봇 연동 시험] " + stamp,
        "ancestors": [{"id": settings.confluence_parent_page_id}],
        "space": {"key": settings.confluence_space_key},
        "body": {
            "storage": {
                "value": "<p>회의록 봇 연동 시험 페이지입니다. 지워도 됩니다.</p>",
                "representation": "storage",
            }
        },
    }
    url = settings.confluence_base_url + "/rest/api/content/"
    status, text = call("POST", url, settings.confluence_token, body)
    if status != 200:
        explain_failure(status, text)
        return False

    page = json.loads(text)
    page_id = page.get("id")
    links = page.get("_links", {})
    web_url = links.get("base", "") + links.get("webui", "")
    print("  성공: 페이지 ID %s" % page_id)
    print("  주소: %s" % (web_url or "(응답에 주소 없음. 상위 페이지 아래에서 직접 확인)"))

    if ask_yes("  브라우저에서 확인한 뒤, 이 시험 페이지를 지울까요? [Y/n] "):
        status, text = call("DELETE", url + str(page_id), settings.confluence_token)
        if status == 204:
            print("  삭제됨.")
        else:
            print("  삭제 실패 (HTTP %s). 브라우저에서 직접 지워 주세요." % status)
    else:
        print("  남겨 둠. 나중에 브라우저에서 지워 주세요.")
    return True


def check_jira(settings: Settings, stamp: str) -> bool:
    print("\n[Jira] 시험 티켓 생성")
    settings.require(
        "jira_base_url", "jira_token", "jira_project_key", "jira_issue_type_id"
    )
    body = {
        "fields": {
            "project": {"key": settings.jira_project_key},
            "summary": "[봇 연동 시험] " + stamp,
            "description": "회의록 봇 연동 시험 티켓입니다. 지우거나 닫아도 됩니다.",
            "issuetype": {"id": settings.jira_issue_type_id},
        }
    }
    url = settings.jira_base_url + "/rest/api/2/issue/"
    status, text = call("POST", url, settings.jira_token, body)
    if status not in (200, 201):
        explain_failure(status, text)
        return False

    key = json.loads(text).get("key")
    print("  성공: %s" % key)
    print("  주소: %s/browse/%s" % (settings.jira_base_url, key))
    print("  이 티켓은 자동으로 지우지 않음. 확인 후 브라우저에서 지우거나 닫아 주세요.")
    return True


def check_teams(settings: Settings, stamp: str) -> bool:
    print("\n[Teams] 시험 메시지 전송")
    settings.require("teams_webhook_url")
    body = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "회의록 봇 연동 시험 (" + stamp + ")",
                            "wrap": True,
                        }
                    ],
                },
            }
        ],
    }
    status, text = call("POST", settings.teams_webhook_url, None, body)
    if status not in (200, 202):
        explain_failure(status, text)
        return False
    print("  전송됨 (HTTP %s). 채널에 메시지가 떴는지 확인해 주세요." % status)
    return True


CHECKS = {"confluence": check_confluence, "jira": check_jira, "teams": check_teams}


def main() -> int:
    parser = argparse.ArgumentParser(description="Confluence, Jira, Teams 연동 점검")
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="대상",
        help="점검할 대상 (%s). 비우면 전부." % ", ".join(CHECKS),
    )
    targets = parser.parse_args().targets or list(CHECKS)
    unknown = [name for name in targets if name not in CHECKS]
    if unknown:
        parser.error("모르는 대상: %s (가능: %s)" % (", ".join(unknown), ", ".join(CHECKS)))

    settings = load_settings()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results: Dict[str, bool] = {}
    for name in targets:
        try:
            results[name] = CHECKS[name](settings, stamp)
        except ConfigError as error:
            print("  설정 문제: %s" % error)
            results[name] = False
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print("  연결 실패: %s" % error)
            results[name] = False

    print("\n결과")
    for name, ok in results.items():
        print("  %-11s %s" % (name, "통과" if ok else "실패"))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
