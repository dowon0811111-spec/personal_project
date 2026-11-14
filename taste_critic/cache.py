"""만료 시간이 있는 JSON 파일 캐시.

MusicBrainz처럼 호출 제한이 빡빡한 API 결과를 저장한다. 값은 JSON으로 저장 가능해야 하고,
"없음"(None)도 값으로 저장해 같은 조회를 반복하지 않게 한다.
"""

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class DiskCache:
    def __init__(
        self,
        path: str | Path,
        ttl_seconds: float,
        now: Callable[[], float] = time.time,
        max_entries: int = 5000,
    ) -> None:
        self._path = Path(path)
        self._ttl = ttl_seconds
        self._now = now
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is None:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._entries = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._entries = {}  # 파일이 없거나 깨졌으면 빈 캐시로 시작한다.
            self._prune(self._entries)
        return self._entries

    def _prune(self, entries: dict[str, dict[str, Any]]) -> None:
        """읽어 들일 때 만료되거나 형식이 깨진 항목을 지우고, 개수가 많으면 오래된 것부터 줄인다."""
        now = self._now()
        for key in [k for k, e in entries.items() if not isinstance(e, dict) or now - e.get("at", 0) > self._ttl]:
            del entries[key]
        excess = len(entries) - self._max_entries
        if excess > 0:
            for key in sorted(entries, key=lambda k: entries[k]["at"])[:excess]:
                del entries[key]

    def get(self, key: str) -> tuple[bool, Any]:
        """(저장돼 있는지, 값)을 돌려준다. 만료된 항목은 없는 것으로 본다."""
        with self._lock:
            entry = self._load().get(key)
            if not isinstance(entry, dict) or self._now() - entry.get("at", 0) > self._ttl:
                return False, None
            return True, entry.get("value")

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            entries = self._load()
            entries[key] = {"at": self._now(), "value": value}
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self._path)
            except OSError:
                pass  # 저장에 실패해도 이번 실행의 메모리 캐시는 계속 쓴다.
