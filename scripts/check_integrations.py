#!/usr/bin/env python3
"""연동 점검: Confluence 시험 페이지, Jira 시험 티켓, Teams 시험 메시지를 하나씩 만든다.

봇이 실제로 쓰는 코드(src/meetingbot/integrations)를 그대로 호출한다.
추가 설치 없이 파이썬 기본 기능만 쓴다.

사용법 (저장소 폴더에서):
    python3 scripts/check_integrations.py                # 셋 다
    python3 scripts/check_integrations.py confluence     # 하나만
    python3 scripts/check_integrations.py jira teams     # 골라서
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot.config import ConfigError, Settings, load_settings  # noqa: E402
from meetingbot.httpclient import ApiError  # noqa: E402
from meetingbot.integrations import confluence, jira, teams  # noqa: E402
from meetingbot.models import ActionItem, Minutes, Topic  # noqa: E402

SAMPLE = Minutes(
    title="봇 연동 시험",
    summary="회의록 봇 연동 시험 페이지입니다. 지워도 됩니다.",
    topics=[Topic(topic="시험 안건", discussion="표시 확인용 문장입니다.", decisions=["시험 결정 <1>"])],
    action_items=[ActionItem(task="시험 할 일", assignee="홍길동", due_date="다음 주 금요일")],
)


def ask_yes(prompt: str) -> bool:
    """Y/n 질문. 그냥 Enter 는 Y 로 본다.

    한글 입력 상태에서 글자를 지우면 터미널이 깨진 바이트를 보낼 수 있다.
    그래도 죽지 않도록 바이트로 읽어서 해석한다. 한글 상태의 Y 키(ㅛ)도 받는다.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    answer = sys.stdin.buffer.readline().decode("utf-8", errors="ignore").strip().lower()
    return answer in ("", "y", "yes", "ㅛ")


def check_confluence(settings: Settings, stamp: str) -> None:
    print("\n[Confluence] 시험 페이지 생성")
    settings.require("confluence_space_key", "confluence_parent_page_id")
    page = confluence.create_page(
        settings, "[봇 연동 시험] " + stamp, confluence.minutes_to_storage(SAMPLE)
    )
    print("  성공: 페이지 ID %s" % page.id)
    print("  주소: %s" % (page.url or "(응답에 주소 없음. 상위 페이지 아래에서 직접 확인)"))
    print("  볼 것: 요약, 안건, 결정 사항, 액션아이템 표가 제대로 보이는지.")
    if ask_yes("  브라우저에서 확인한 뒤, 이 시험 페이지를 지울까요? [Y/n] "):
        confluence.delete_page(settings, page.id)
        print("  삭제됨.")
    else:
        print("  남겨 둠. 나중에 브라우저에서 지워 주세요.")


def check_jira(settings: Settings, stamp: str) -> None:
    print("\n[Jira] 시험 티켓 생성")
    settings.require("jira_project_key", "jira_issue_type_id")
    description = jira.action_item_description(
        SAMPLE.action_items[0], SAMPLE.title, "(시험이라 회의록 주소 없음)"
    )
    issue = jira.create_issue(settings, "[봇 연동 시험] " + stamp, description)
    print("  성공: %s" % issue.key)
    print("  주소: %s" % issue.url)
    print("  이 티켓은 자동으로 지우지 않음. 확인 후 브라우저에서 지우거나 닫아 주세요.")


def check_teams(settings: Settings, stamp: str) -> None:
    print("\n[Teams] 시험 메시지 전송")
    lines = ["회의록 봇 연동 시험입니다. (%s)" % stamp]
    if settings.jira_base_url:
        lines.append("링크 표시 시험: [Jira 열기](%s)" % settings.jira_base_url)
    status = teams.send_card(settings, "회의록 봇 연동 시험", lines)
    print("  전송됨 (HTTP %s). 채널에서 제목이 굵게, 링크가 눌리게 보이는지 확인해 주세요." % status)


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
            CHECKS[name](settings, stamp)
            results[name] = True
        except ConfigError as error:
            print("  설정 문제: %s" % error)
            results[name] = False
        except ApiError as error:
            print("  %s" % error)
            results[name] = False

    print("\n결과")
    for name, ok in results.items():
        print("  %-11s %s" % (name, "통과" if ok else "실패"))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
