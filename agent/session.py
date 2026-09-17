"""会话持久化：每轮结束自动写 sessions/<时间>.json，/resume 恢复，/sessions 列表。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from agent import config
from agent.render import console


@dataclass
class SessionMeta:
    path: Path
    created: str
    updated: str
    cwd: str
    model: str
    preview: str
    turns: int


class Session:
    def __init__(self) -> None:
        self.id = time.strftime("%Y%m%d-%H%M%S")
        self.path = config.SESSION_DIR / f"{self.id}.json"
        self.created = time.strftime("%Y-%m-%d %H:%M:%S")

    def save(self, messages: list[dict], todos: list[dict]) -> None:
        """只有出现过用户消息才落盘，避免空会话文件。"""
        if not any(m.get("role") == "user" for m in messages):
            return
        try:
            config.SESSION_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "id": self.id,
                "created": self.created,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "cwd": str(Path.cwd()),
                "model": config.MODEL_LABEL or config.MODEL_NAME,
                "messages": messages,
                "todos": todos,
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass


def _preview(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "user":
            text = str(m.get("content") or "").split("\n\n[系统路径提示]")[0]
            text = " ".join(text.split())
            return text[:60] + ("…" if len(text) > 60 else "")
    return "(空)"


def list_recent(limit: int = 10) -> list[SessionMeta]:
    if not config.SESSION_DIR.is_dir():
        return []
    files = sorted(config.SESSION_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[SessionMeta] = []
    for p in files[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        msgs = data.get("messages") or []
        out.append(
            SessionMeta(
                path=p,
                created=str(data.get("created", "")),
                updated=str(data.get("updated", "")),
                cwd=str(data.get("cwd", "")),
                model=str(data.get("model", "")),
                preview=_preview(msgs),
                turns=sum(1 for m in msgs if m.get("role") == "user"),
            )
        )
    return out


def load(meta: SessionMeta) -> tuple[list[dict], list[dict], str] | None:
    """返回 (messages, todos, cwd)。"""
    try:
        data = json.loads(meta.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    msgs = data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return None
    todos = data.get("todos") if isinstance(data.get("todos"), list) else []
    return msgs, todos, str(data.get("cwd") or "")


def print_list(items: list[SessionMeta]) -> None:
    from rich.table import Table

    if not items:
        console.print("  [dim]还没有保存的会话[/]")
        return
    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 1))
    table.add_column("#", justify="right", style="cyan")
    table.add_column("更新时间", style="dim")
    table.add_column("轮", justify="right", style="dim")
    table.add_column("模型", style="dim")
    table.add_column("首条消息")
    for i, s in enumerate(items, 1):
        table.add_row(str(i), s.updated, str(s.turns), s.model[:28], s.preview)
    console.print(table)
    console.print("  [dim]/resume N 恢复第 N 条[/]")
