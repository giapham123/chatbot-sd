"""CSV-backed repository adapters + a DataContext aggregate.

The conversational-RAG runtime only needs three sheets: responses (fixed bot
messages), actions (forward-to-admin), and knowledge_base (the RAG corpus).
Swapping CSV for a DB later means new adapters satisfying the same interfaces.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from ..domain.models import ActionDef, KBDoc, Response


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()] if value else []


class CsvResponseRepository:
    def __init__(self, path: Path) -> None:
        self._items = {
            r["response_key"]: Response(r["response_key"], r["text"], r.get("variables", ""))
            for r in _read(path)
        }

    def text(self, response_key: str) -> str:
        resp = self._items.get(response_key)
        return resp.text if resp else f"[missing response: {response_key}]"


class CsvActionRepository:
    def __init__(self, path: Path) -> None:
        self._items = {
            r["action_id"]: ActionDef(
                action_id=r["action_id"],
                type=r["type"],
                message_key=r["message_key"],
                payload=_split(r.get("payload", "")),
            )
            for r in _read(path)
        }

    def get(self, action_id: str) -> Optional[ActionDef]:
        return self._items.get(action_id)


class CsvKnowledgeRepository:
    def __init__(self, path: Path) -> None:
        self._items = [
            KBDoc(
                doc_id=r["doc_id"],
                topic_id=r.get("topic_id", ""),
                question=r["question"],
                answer=r["answer"],
                source=r.get("source", ""),
                tags=r.get("tags", ""),
            )
            for r in _read(path)
        ]

    def all(self) -> list[KBDoc]:
        return list(self._items)


class CsvDataContext:
    """Aggregates the sheet repositories used by the runtime."""

    def __init__(self, data_dir: Path) -> None:
        self.responses = CsvResponseRepository(data_dir / "responses.csv")
        self.actions = CsvActionRepository(data_dir / "actions.csv")
        self.knowledge = CsvKnowledgeRepository(data_dir / "knowledge_base.csv")