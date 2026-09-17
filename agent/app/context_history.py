"""Source-linked same-task history, separate from cross-session user memory.

This module does not confer ReferenceContext permissions. A historical display
record is explanatory evidence only; active actions still require the existing
server's task/revision/scope validation. Archives have one writer per lane.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class HistoryArchiveError(ValueError):
    pass


class HistoryArchive:
    """Append-only, hash-chained raw records with a rebuildable in-memory index."""

    def __init__(self, directory: Path, *, session_id: str, task_id: str, create=False):
        self.directory = Path(directory).resolve()
        self.identity = {"sessionId": session_id, "taskId": task_id}
        if not all(isinstance(value, str) and value for value in self.identity.values()):
            raise HistoryArchiveError("missing_archive_identity")
        if create:
            self.directory.mkdir(parents=True, exist_ok=False)
            with (self.directory / "identity.json").open("x", encoding="utf-8") as stream:
                stream.write(_canonical(self.identity))
            with (self.directory / "messages.jsonl").open("x", encoding="utf-8"):
                pass
        if json.loads((self.directory / "identity.json").read_text(encoding="utf-8")) != self.identity:
            raise HistoryArchiveError("archive_identity_mismatch")
        self._records = []
        self._index = {}
        self._load()

    def _load(self):
        previous = _hash(self.identity)
        for line in (self.directory / "messages.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                raise HistoryArchiveError("blank_archive_record")
            record = json.loads(line)
            payload = {key: value for key, value in record.items() if key != "recordHash"}
            if record.get("previousHash") != previous or record.get("recordHash") != _hash(payload):
                raise HistoryArchiveError("archive_hash_chain_invalid")
            if record.get("ordinal") != len(self._records) + 1 or record.get("identity") != self.identity:
                raise HistoryArchiveError("archive_sequence_or_identity_invalid")
            if record.get("messageId") in self._index:
                raise HistoryArchiveError("duplicate_message_id")
            self._records.append(record)
            self._index[record["messageId"]] = record
            previous = record["recordHash"]

    def append(self, role: str, content: str, *, turn: int, scope_id=None, display=None):
        if role not in {"user", "assistant", "tool"} or not isinstance(content, str):
            raise HistoryArchiveError("invalid_message")
        if type(turn) is not int or turn < 1:
            raise HistoryArchiveError("invalid_turn")
        if display is not None:
            if role != "assistant" or not isinstance(display, dict):
                raise HistoryArchiveError("display_requires_actual_assistant_publication")
            if set(display) != {"batchId", "productIds"} or not isinstance(display["batchId"], str):
                raise HistoryArchiveError("invalid_display_record")
            ids = display["productIds"]
            if not isinstance(ids, list) or any(type(value) is not int or value < 1 for value in ids) or len(set(ids)) != len(ids):
                raise HistoryArchiveError("invalid_display_order")
        ordinal = len(self._records) + 1
        # Neither normalization nor sentence clipping changes original bytes.
        payload = {"identity": self.identity, "ordinal": ordinal,
            "messageId": "msg-" + _hash([self.identity, ordinal])[:24],
            "role": role, "content": content, "turn": turn, "scopeId": scope_id,
            "display": deepcopy(display), "createdAt": datetime.now(timezone.utc).isoformat(),
            "previousHash": self._records[-1]["recordHash"] if self._records else _hash(self.identity)}
        record = {**payload, "recordHash": _hash(payload)}
        with (self.directory / "messages.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_canonical(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._records.append(record)
        self._index[record["messageId"]] = record
        return deepcopy(record)

    def records(self):
        return deepcopy(self._records)

    def read(self, message_id: str):
        # Only IDs inside this identity-bound archive can be read; never paths.
        if message_id not in self._index:
            raise HistoryArchiveError("source_not_in_current_archive")
        return deepcopy(self._index[message_id])

    def index(self):
        return [{"messageId": row["messageId"], "turn": row["turn"], "role": row["role"],
                 "scopeId": row["scopeId"], "display": deepcopy(row["display"]),
                 "source": "messages.jsonl:" + str(row["ordinal"]), "recordHash": row["recordHash"]}
                for row in self._records]

    def search(self, query: str, *, limit=4):
        if type(limit) is not int or not 0 <= limit <= 32:
            raise HistoryArchiveError("invalid_lookup_limit")
        normalized = re.sub(r"\s+", "", query).casefold()
        grams = set(normalized[index:index + 2] for index in range(max(0, len(normalized) - 1)))
        ranked = []
        for row in self._records:
            text = re.sub(r"\s+", "", row["content"]).casefold()
            score = sum(term in text for term in grams)
            if row["messageId"] in query:
                score += 1000000
            if score:
                ranked.append((score, row["ordinal"], row))
        return [deepcopy(row) for _, _, row in sorted(ranked, key=lambda item: (item[0], item[1]), reverse=True)[:limit]]

    def historical_display(self, batch_id: str):
        matches = [row for row in self._records if row["display"] and row["display"]["batchId"] == batch_id]
        if len(matches) != 1:
            raise HistoryArchiveError("historical_batch_missing_or_ambiguous")
        return {"historicalOnly": True, "actionAuthorized": False, "record": deepcopy(matches[0])}
