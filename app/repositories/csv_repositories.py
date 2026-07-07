"""CSV-backed repository adapters + a DataContext aggregate.

The conversational-RAG runtime only needs three sheets: responses (fixed bot
messages), actions (forward-to-admin), and knowledge_base (the RAG corpus).
Swapping CSV for a DB later means new adapters satisfying the same interfaces.
"""
from __future__ import annotations

import csv
import re
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


class MarkdownKnowledgeRepository:
    """Parses knowledge_base_full.md → KBDoc list for RAG embedding.

    Block format (separated by ---):
      **Q:** main question
      **Đồng nghĩa:** alt1 | alt2 | ...   (optional)
      **A:** answer text

    Each synonym gets its own KBDoc with the same answer so every phrasing
    receives an independent embedding vector.
    """

    _Q = re.compile(r'^\*\*Q:\*\*\s*(.+)$', re.MULTILINE)
    _SYN = re.compile(r'^\*\*Đồng nghĩa:\*\*\s*(.+)$', re.MULTILINE)
    _A = re.compile(r'^\*\*A:\*\*\s*(.+)$', re.MULTILINE)

    def __init__(self, path: Path) -> None:
        self._items = self._parse(path)

    def _parse(self, path: Path) -> list[KBDoc]:
        raw = path.read_text(encoding="utf-8")
        docs: list[KBDoc] = []
        idx = 0
        for block in raw.split("---"):
            block = block.strip()
            if not block or block.startswith("#"):
                continue
            q_m = self._Q.search(block)
            a_m = self._A.search(block)
            if not q_m or not a_m:
                continue
            main_q = q_m.group(1).strip()
            answer = a_m.group(1).strip()
            syn_m = self._SYN.search(block)
            synonyms = (
                [s.strip() for s in syn_m.group(1).split("|") if s.strip()]
                if syn_m else []
            )
            docs.append(KBDoc(
                doc_id=f"md_{idx:04d}",
                topic_id="",
                question=main_q,
                answer=answer,
                source=path.name,
                tags="",
            ))
            for s_i, syn in enumerate(synonyms, 1):
                docs.append(KBDoc(
                    doc_id=f"md_{idx:04d}_s{s_i}",
                    topic_id="",
                    question=syn,
                    answer=answer,
                    source=path.name,
                    tags="",
                ))
            idx += 1
        return docs

    def all(self) -> list[KBDoc]:
        return list(self._items)


class CsvDataContext:
    """Aggregates the sheet repositories used by the runtime.

    Prefers knowledge_base_full.md when present; falls back to knowledge_base.csv.
    """

    def __init__(self, data_dir: Path) -> None:
        self.responses = CsvResponseRepository(data_dir / "responses.csv")
        self.actions = CsvActionRepository(data_dir / "actions.csv")
        md_path = data_dir / "knowledge_base_full.md"
        if md_path.exists():
            self.knowledge: MarkdownKnowledgeRepository | CsvKnowledgeRepository = (
                MarkdownKnowledgeRepository(md_path)
            )
        else:
            self.knowledge = CsvKnowledgeRepository(data_dir / "knowledge_base.csv")