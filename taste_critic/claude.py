"""ai_helper.client로 Claude를 부르는 얇은 래퍼.

ask_ai는 thinking을 끌 수 없는데, 이 모델은 thinking이 켜져 있으면 max_tokens(1000)를
생각에 다 써서 본문이 빈 채로 끝난다. 그래서 같은 client와 같은 모델, 같은 max_tokens로
thinking만 끄고 호출한다. ai_helper.py 자체는 수정하지 않는다.
"""

from typing import Any

from taste_critic.review import ReviewError

# ai_helper.ask_ai가 쓰는 모델 ID와 같아야 한다. ai_helper.py가 바뀌면 여기도 맞춘다.
MODEL = "claude-haiku-5.5"
MAX_TOKENS = 1000


def ask_without_thinking(prompt: str, client: Any = None) -> str:
    if client is None:
        from ai_helper import client as shared_client

        client = shared_client
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "max_tokens":
        raise ReviewError("Claude 응답이 길이 제한(max_tokens)에 걸려 잘렸습니다.")
    return "".join(block.text for block in response.content if block.type == "text")
