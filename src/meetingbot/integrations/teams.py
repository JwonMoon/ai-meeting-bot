"""Teams 채널 알림 (Workflows webhook).

메시지 형식의 근거 (공식 문서):
https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook
"""

from typing import Any, Dict, List

from ..config import Settings
from ..httpclient import failure, request


def build_card(title: str, lines: List[str]) -> Dict[str, Any]:
    """제목 한 줄과 본문 여러 줄로 된 카드. 본문에는 [글자](주소) 형식의 링크를 쓸 수 있다."""
    blocks: List[Dict[str, Any]] = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}
    ]
    blocks += [{"type": "TextBlock", "text": line, "wrap": True} for line in lines]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": blocks,
                },
            }
        ],
    }


def send_card(settings: Settings, title: str, lines: List[str]) -> int:
    """카드를 보내고 HTTP 상태 코드를 돌려준다."""
    settings.require("teams_webhook_url")
    status, text = request("POST", settings.teams_webhook_url, body=build_card(title, lines))
    if status not in (200, 202):
        raise failure("Teams 알림 전송", status, text)
    return status
