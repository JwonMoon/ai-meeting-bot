"""HTTP 호출 공통 부분. 파이썬 기본 기능만 쓴다."""

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

DEFAULT_TIMEOUT_SECONDS = 30


class ApiError(Exception):
    """외부 서비스 호출이 실패했을 때. 메시지에 토큰이나 전체 주소를 넣지 않는다."""


def request(
    method: str,
    url: str,
    token: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Tuple[int, str]:
    """요청을 보내고 (상태 코드, 응답 본문)을 돌려준다.

    서버가 4xx, 5xx 로 답한 경우도 예외 없이 돌려준다. 판단은 호출한 쪽이 한다.
    서버에 닿지 못한 경우에만 ApiError 를 낸다.
    """
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except OSError as error:
        # Teams webhook 처럼 주소 자체가 비밀인 경우가 있어 서버 이름만 남긴다.
        raise ApiError("연결 실패 (%s): %s" % (urlsplit(url).netloc, error)) from None


_HINTS = {
    400: "요청 형식을 서버가 거절함. 응답에 이유가 적혀 있음.",
    401: "토큰이 틀렸거나 만료됨.",
    403: "토큰은 맞지만 이 위치에 만들 권한이 없음.",
    404: "주소나 ID가 틀림.",
}


def failure(what: str, status: int, text: str) -> ApiError:
    """실패한 응답을 사람이 읽을 수 있는 오류로 바꾼다."""
    snippet = text[:600].replace("\n", " ")
    return ApiError(
        "%s 실패: HTTP %s. %s 응답 앞부분: %s" % (what, status, _HINTS.get(status, ""), snippet)
    )
