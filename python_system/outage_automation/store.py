from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List


import shutil


def _safe_replace_or_copy(tmp: Path, dst: Path) -> None:
    try:
        os.replace(tmp, dst)
    except OSError as e:
        if getattr(e, "errno", None) in (18, 13) or isinstance(e, (PermissionError, OSError)):
            shutil.copy2(tmp, dst)
            try:
                tmp.unlink()
            except OSError:
                pass
        else:
            raise


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
    except OSError:
        from .paths import resolve_paths
        state_dir = resolve_paths(root=str(path.parent)).state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = state_dir / f"{path.name}.tmp"
        tmp.write_text(text, encoding="utf-8", newline="\n")
    _safe_replace_or_copy(tmp, path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def append_ndjson(path: Path, items: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
            count += 1
    return count


def read_ndjson(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out

