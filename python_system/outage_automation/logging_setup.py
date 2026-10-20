"""Lightweight structured logging for the outage system.

This module avoids the `logging` module for two reasons:

1. The CLI is short-lived and may be invoked from cron, systemd, or a
   shell — and we want consistent, parseable output.
2. The output is consumed by humans via grep and by log aggregators
   via the `json` format, so we want a format that's easy to scan and
   easy to parse.

Format:
    [2026-06-06T12:34:56Z] [INFO ] [run] starting run-all (3 providers)

Set `OA_LOG_LEVEL=debug|info|warn|error` (default `info`).
Set `OA_LOG_FORMAT=json` to emit JSON lines instead of the pretty format.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional


_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_DEFAULT_LEVEL = "info"


def _env_level() -> str:
    raw = (os.environ.get("OA_LOG_LEVEL") or _DEFAULT_LEVEL).lower()
    return raw if raw in _LEVELS else _DEFAULT_LEVEL


def _env_format() -> str:
    raw = (os.environ.get("OA_LOG_FORMAT") or "pretty").lower()
    return raw if raw in ("pretty", "json") else "pretty"


class _Logger:
    def __init__(self, scope: str = "outage") -> None:
        self.scope = scope
        self.level = _env_level()
        self.format = _env_format()

    def _emit(self, level: str, msg: str, **fields: Any) -> None:
        if _LEVELS[level] < _LEVELS[self.level]:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.format == "json":
            payload: Dict[str, Any] = {
                "ts": ts,
                "level": level.upper(),
                "scope": self.scope,
                "msg": msg,
            }
            for k, v in fields.items():
                if v is not None:
                    payload[k] = v
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        else:
            tag = level.upper().ljust(5)
            tail = ""
            if fields:
                tail = " " + " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
            line = f"[{ts}] [{tag}] [{self.scope}] {msg}{tail}"
        print(line, file=sys.stderr, flush=True)

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit("debug", msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._emit("info", msg, **fields)

    def warn(self, msg: str, **fields: Any) -> None:
        self._emit("warn", msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._emit("warn", msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._emit("error", msg, **fields)


def get_logger(scope: str = "outage") -> _Logger:
    return _Logger(scope=scope)


def now_ms() -> int:
    return int(time.time() * 1000)
