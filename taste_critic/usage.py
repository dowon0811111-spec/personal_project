"""Claude 호출 횟수 제한 (비용 관리).

사용자가 화면에서 한 번 요청하는 것(리뷰, 패턴 해석, 질문 등)을 1회로 센다.
세션 단위의 가벼운 제한이라 새로고침으로 우회할 수 있다. 공개 배포에서 강한 제한이 필요하면
IP나 계정 기준의 서버 측 제한을 따로 둬야 한다.
"""

import time
from collections import deque
from collections.abc import Callable


class UsageLimitError(Exception):
    """허용된 호출 횟수를 넘었을 때 발생한다. 메시지는 사용자에게 그대로 보여 준다."""


class UsageLimiter:
    def __init__(
        self, max_calls: int, window_seconds: float = 3600, now: Callable[[], float] = time.monotonic
    ) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._now = now
        self._calls: deque[float] = deque()

    def consume(self) -> None:
        """한 번 쓴다. max_calls가 0 이하면 제한하지 않는다."""
        if self._max_calls <= 0:
            return
        now = self._now()
        while self._calls and now - self._calls[0] >= self._window:
            self._calls.popleft()
        if len(self._calls) >= self._max_calls:
            wait_minutes = max(1, int((self._calls[0] + self._window - now) / 60) + 1)
            raise UsageLimitError(
                f"요청이 너무 많습니다. 한 시간에 {self._max_calls}번까지 쓸 수 있어요. 약 {wait_minutes}분 뒤에 다시 시도해 주세요."
            )
        self._calls.append(now)
