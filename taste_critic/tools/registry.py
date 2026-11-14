"""tool use 도구 등록부: 정의(JSON schema)와 실행 함수를 한 곳에 둔다.

도구 실행 오류는 예외로 터뜨리지 않고 is_error 결과로 돌려줘서 Claude가 다른 경로를 고르게 한다.
오류 메시지에는 내부 정보(API 키, 스택)를 넣지 않는다.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from taste_critic.lastfm import LastFmError


class ToolRefusal(Exception):
    """도구가 규칙상 실행을 거절할 때 발생한다. 메시지는 Claude에게 그대로 전달된다."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    run: Callable[[Any], Any]

    def definition(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self._tools.values()]

    def execute(self, name: str, raw_input: Any) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutcome(f"알 수 없는 도구입니다: {name}", True)
        try:
            args = tool.args_model.model_validate(raw_input)
        except ValidationError:
            return ToolOutcome("도구 인자가 올바르지 않습니다. 입력 스키마를 확인하세요.", True)
        try:
            result = tool.run(args)
        except ToolRefusal as exc:
            return ToolOutcome(str(exc), True)
        except LastFmError as exc:
            return ToolOutcome(str(exc), True)
        except Exception:
            return ToolOutcome("도구 실행 중 오류가 발생했습니다.", True)
        return ToolOutcome(json.dumps(result, ensure_ascii=False), False)
