#!/usr/bin/env python3
"""화면 없이 작업 흐름을 돌려 보는 명령줄 도구. (화면 web/ 이 생기기 전까지의 운전석)

사용법 (Thor, 저장소 폴더에서):
    python3 scripts/jobs.py new ~/meetingbot-test/meeting.m4a      # 작업 만들고 전사·요약까지
    python3 scripts/jobs.py list                                    # 작업 목록
    python3 scripts/jobs.py show <작업ID>                           # 상태, 진행 기록, 회의록 초안
    python3 scripts/jobs.py run <작업ID>                            # 실패한 단계부터 다시
    python3 scripts/jobs.py approve <작업ID> [--minutes 고친.json] [--parent 페이지ID] [--project KEY] [--no-jira] [--no-teams]
    python3 scripts/jobs.py publish <작업ID>                        # 승인된 것을 Confluence, Jira, Teams 로
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot import db as jobs_db  # noqa: E402
from meetingbot import pipeline  # noqa: E402
from meetingbot.config import load_settings  # noqa: E402
from meetingbot.models import Minutes  # noqa: E402


def show(db: jobs_db.Database, job_id: str) -> None:
    job = db.get(job_id)
    print("작업 %s | %s | 상태: %s" % (job.id, job.title, job.status_label))
    if job.error:
        print("오류(%s): %s" % (job.step, job.error))
    print("\n진행 기록:")
    for event in db.events(job.id):
        print("  %s  %s" % (event["at"][11:19], event["message"]))
    if job.minutes_dict:
        print("\n회의록 초안 (%s):" % (pipeline.job_dir(load_settings(), job.id) / "minutes.json"))
        print(json.dumps(job.minutes_dict, ensure_ascii=False, indent=2))
    if job.publish:
        print("\n게시 정보: %s" % json.dumps(job.publish, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="회의록 봇 작업 명령줄")
    sub = parser.add_subparsers(dest="command", required=True)
    p_new = sub.add_parser("new", help="녹음 파일로 작업을 만들고 검토 대기까지 진행")
    p_new.add_argument("audio", type=Path)
    p_new.add_argument("--title")
    sub.add_parser("list", help="작업 목록")
    for name in ("show", "run", "publish"):
        sub.add_parser(name).add_argument("job_id")
    p_ap = sub.add_parser("approve", help="회의록을 승인하고 게시 위치를 정함 (보내지는 않음)")
    p_ap.add_argument("job_id")
    p_ap.add_argument("--minutes", type=Path, help="고친 회의록 JSON. 비우면 초안 그대로")
    p_ap.add_argument("--parent", default="", help="Confluence 상위 페이지 ID. 비우면 .env 기본값")
    p_ap.add_argument("--project", default="", help="Jira 프로젝트 키. 비우면 .env 기본값")
    p_ap.add_argument("--no-jira", action="store_true")
    p_ap.add_argument("--no-teams", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    db = pipeline.open_database(settings)

    try:
        if args.command == "list":
            for job in db.list_jobs():
                print("%s  %-10s  %s" % (job.id, job.status_label, job.title))
            return 0
        if args.command == "show":
            show(db, args.job_id)
            return 0
        if args.command == "new":
            job = pipeline.new_job(settings, db, args.audio, args.title)
            print("작업 생성: %s" % job.id)
            job = pipeline.run_to_review(settings, db, job.id, print)
        elif args.command == "run":
            job = pipeline.run_to_review(settings, db, args.job_id, print)
            if job.status == jobs_db.APPROVED or (job.status == jobs_db.FAILED and job.step == pipeline.STEP_PUBLISH):
                job = pipeline.publish(settings, db, job.id, print)
        elif args.command == "approve":
            job = db.get(args.job_id)
            minutes = Minutes.from_dict(json.loads(args.minutes.read_text(encoding="utf-8")) if args.minutes else job.minutes_dict)
            choices = pipeline.PublishChoices(
                parent_page_id=args.parent, project_key=args.project,
                create_jira=not args.no_jira, send_teams=not args.no_teams,
            )
            job = pipeline.approve(db, args.job_id, minutes, choices)
            print("승인됨. 보내려면: python3 scripts/jobs.py publish %s" % job.id)
        elif args.command == "publish":
            job = pipeline.publish(settings, db, args.job_id, print)
    except (pipeline.PipelineError, KeyError) as error:
        print("실패: %s" % error)
        return 1
    finally:
        db.close()

    print("\n상태: %s" % job.status_label)
    if job.status == jobs_db.FAILED:
        print("오류(%s): %s\n고친 뒤 다시: python3 scripts/jobs.py run %s" % (job.step, job.error, job.id))
        return 1
    if job.status == jobs_db.REVIEW:
        print("회의록 초안: %s" % (pipeline.job_dir(settings, job.id) / "minutes.json"))
        print("읽고 고친 뒤: python3 scripts/jobs.py approve %s [--minutes 고친.json]" % job.id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
