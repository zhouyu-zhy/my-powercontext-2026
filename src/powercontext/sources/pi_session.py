# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pi session-history Sources captured from one selected conversation branch.

A Pi archive is JSONL conversation graph: one session header followed by records
that each name a parent, so a leaf node and its ancestors select one history
path. Every visible event on that path becomes one ``CAPTURED`` Source whose
identity is derived from the session id and event id only, which keeps citations
stable across branch switches, appends and restarts. The Connector keeps imported
identities plus original record fingerprints in its checkpoint, so a resumed run
imports only what is new and a rewritten history is rejected before the stored
progress moves.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SOURCE_DEFINITION_VERSION = "1"
PI_CONNECTOR_VERSION = "1"
PI_IDENTITY_SCHEME = "pi"
PI_UNKNOWN_TOOL_NAME = "unknown"
PI_CUSTOM_MESSAGE_ROLE = "custom"

_SESSION_RECORD_TYPE = "session"
_SESSION_FORMAT_VERSION = 3
_SUMMARY_EVENT_TYPES = frozenset({"compaction", "branch_summary"})
_CHECKPOINT_VERSION = 1
_UTF8_BOM = chr(0xFEFF)


def _require_identity(field: str, value: str) -> str:
    """Graph components feed event identity, so blanks and padding are rejected."""
    if not value or not value.strip():
        raise ValueError(f"Pi {field} must be a non-empty string")  # noqa: TRY003
    if value != value.strip():
        raise ValueError(f"Pi {field} must not contain leading or trailing whitespace")  # noqa: TRY003
    return value


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build one event identity, which depends on session and event ids only.

    Paths, modification times and the selected leaf deliberately stay out of the
    identity, so moving or re-exporting an archive never forks its evidence.
    """
    _require_identity("session id", session_id)
    _require_identity("event id", event_id)
    source_id = f"{PI_IDENTITY_SCHEME}:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"Pi event identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


class PiSessionInput(BaseModel):
    """One whole Pi v3 archive together with the leaf that selects a branch."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None

    @field_validator("leaf_id")
    @classmethod
    def validate_leaf_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_identity("leaf id", value)


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its complete input record."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @field_validator("session_id", "event_id", "event_type", "role")
    @classmethod
    def validate_identity_parts(cls, value: str) -> str:
        return _require_identity("event field", value)

    @field_validator("parent_id")
    @classmethod
    def validate_parent_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_identity("parent id", value)

    @model_validator(mode="after")
    def validate_identity(self) -> PiEventInput:
        """Every visible event carries a bounded identity as soon as it is parsed."""
        pi_event_source_id(self.session_id, self.event_id)
        return self


def _decode_archive(content: bytes) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error  # noqa: TRY003
    return text.removeprefix(_UTF8_BOM)


def _read_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as error:
            raise ValueError("Pi session records must be one JSON value per line") from error  # noqa: TRY003
        if not isinstance(value, dict):
            raise ValueError("each Pi session record must be a JSON object")  # noqa: TRY003
        records.append(value)
    return records


def _read_archive(content: bytes) -> tuple[str, dict[str, dict[str, Any]], list[str]]:
    """Return the session id, every graph record by id, and the ids in file order."""
    records = _read_records(_decode_archive(content))
    header, *nodes = records if records else [{}]
    if header.get("type") != _SESSION_RECORD_TYPE or header.get("version") != _SESSION_FORMAT_VERSION:
        raise ValueError("a Pi session archive must start with a version 3 session record")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str):
        raise ValueError("the Pi session record must declare a string id")  # noqa: TRY003
    _require_identity("session id", session_id)
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in nodes:
        event_id = record.get("id")
        if not isinstance(event_id, str):
            raise ValueError("each Pi session record must declare a string id")  # noqa: TRY003
        _require_identity("event id", event_id)
        if event_id in by_id:
            raise ValueError(f"Pi event id {event_id!r} is not unique")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str):
                raise ValueError("the Pi parentId must be a string or null")  # noqa: TRY003
            _require_identity("parent id", parent_id)
            if parent_id not in by_id:
                raise ValueError(f"Pi event {event_id!r} has no earlier parent {parent_id!r}")  # noqa: TRY003
        by_id[event_id] = record
        order.append(event_id)
    return session_id, by_id, order


def _branch_path(by_id: dict[str, dict[str, Any]], order: list[str], leaf_id: str | None) -> list[str]:
    """Walk from the selected leaf to the root, rejecting an unknown or cyclic leaf."""
    if not order:
        return []
    leaf = order[-1] if leaf_id is None else leaf_id
    if leaf not in by_id:
        raise ValueError(f"Pi leaf {leaf!r} is not part of the session graph")  # noqa: TRY003
    path: list[str] = []
    visited: set[str] = set()
    cursor: str | None = leaf
    while cursor is not None:
        if cursor in visited:
            raise ValueError("the Pi parent chain contains a cycle")  # noqa: TRY003
        visited.add(cursor)
        path.append(cursor)
        parent_id = by_id[cursor].get("parentId")
        cursor = parent_id if isinstance(parent_id, str) else None
    path.reverse()
    return path


def _named(record: dict[str, Any], message: dict[str, Any], field: str) -> str:
    for value in (message.get(field), record.get(field)):
        if isinstance(value, str) and value.strip():
            return value
    return PI_UNKNOWN_TOOL_NAME


def _compact_json(value: object) -> str:
    """Render arguments with Unicode preserved, keys sorted and no extra spacing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _blocks_text(value: object) -> str:
    """Join the visible text blocks of a content list, ignoring private ones."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _message_body(record: dict[str, Any], message: dict[str, Any], role: str) -> str:
    content = message.get("content")
    if role == "toolResult":
        label = f"[tool_result {_named(record, message, 'toolName')} {_named(record, message, 'toolCallId')}]"
        text = _blocks_text(content)
        return f"{label} {text}" if text else label
    if role == "bashExecution":
        command = message.get("command", record.get("command"))
        if not isinstance(command, str) or not command:
            return ""
        output = message.get("output", record.get("output"))
        return f"$ {command}\n{output if isinstance(output, str) else ''}"
    if isinstance(content, str):
        return content
    blocks = content if isinstance(content, list) else []
    if role != "assistant":
        return _blocks_text(content)
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif kind == "toolCall":
            name = block.get("name")
            call_id = block.get("id")
            parts.append(
                f"[tool_call {name if isinstance(name, str) and name else PI_UNKNOWN_TOOL_NAME} "
                f"{call_id if isinstance(call_id, str) and call_id else PI_UNKNOWN_TOOL_NAME}] "
                f"{_compact_json(block.get('arguments'))}"
            )
    return "\n".join(parts)


def _visible_event(session_id: str, record: dict[str, Any]) -> PiEventInput | None:
    """Project one graph node to its visible event, or nothing for control nodes."""
    event_type = record.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("each Pi session record must declare a string type")  # noqa: TRY003
    parent_id = record.get("parentId")
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"Pi message event {record['id']!r} must declare a role")  # noqa: TRY003
        body = _message_body(record, message, role)
    elif event_type in _SUMMARY_EVENT_TYPES:
        role = event_type
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
    elif event_type == "custom_message":
        role = PI_CUSTOM_MESSAGE_ROLE
        body = _blocks_text(record.get("content"))
    else:
        return None
    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=parent_id if isinstance(parent_id, str) else None,
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _branch_events(
    session_id: str,
    by_id: dict[str, dict[str, Any]],
    order: list[str],
    leaf_id: str | None,
) -> tuple[PiEventInput, ...]:
    events: list[PiEventInput] = []
    for event_id in _branch_path(by_id, order, leaf_id):
        event = _visible_event(session_id, by_id[event_id])
        if event is not None:
            events.append(event)
    return tuple(events)


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return every visible event on the branch selected by the leaf, root first.

    Validation is atomic: the whole archive and the selected path must parse
    before any event is returned, so a rejected archive never yields partial work.
    """
    session_id, by_id, order = _read_archive(value.content)
    return _branch_events(session_id, by_id, order, value.leaf_id)


class PiSessionEvent(Source):
    """Durable identity, text and complete input record of one captured event."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiSessionSourceAdapter:
    """Turn one visible event into its immutable Source and read it back exactly."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiSessionEvent

    async def resolve(self, value: PiEventInput) -> PiSessionEvent:
        return PiSessionEvent(
            name=pi_event_source_id(value.session_id, value.event_id),
            definition_version=PI_SOURCE_DEFINITION_VERSION,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=dict(value.record),
        )

    async def read(self, source: PiSessionEvent) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=dict(source.record),
        )


class PiEventTextEvidenceProjection:
    """Expose the full original event text as standard citation-bearing evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiSessionEvent
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiSessionEvent) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION: AdapterSourceDefinition[PiEventInput, PiSessionEvent, PiEventInput] = (
    AdapterSourceDefinition(PiSessionSourceAdapter(), projections=(PiEventTextEvidenceProjection(),))
)


def _record_fingerprint(record: dict[str, Any]) -> str:
    """Hash one raw record, so a later rewrite of imported history is detectable."""
    declaration = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(declaration.encode('utf-8')).hexdigest()}"


def _imported_events(checkpoint: Any) -> dict[tuple[str, str], str]:
    """Read imported identities and record fingerprints from one stored checkpoint."""
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != _CHECKPOINT_VERSION:
        raise ValueError("Pi checkpoint must declare a supported version")  # noqa: TRY003
    events = checkpoint.get("events")
    if not isinstance(events, list):
        raise ValueError("Pi checkpoint must declare its imported events")  # noqa: TRY003
    imported: dict[tuple[str, str], str] = {}
    for item in events:
        if not isinstance(item, dict):
            raise ValueError("Pi checkpoint events must be JSON objects")  # noqa: TRY003
        identity = (item.get("session_id"), item.get("event_id"), item.get("fingerprint"))
        if not all(isinstance(part, str) and part for part in identity):
            raise ValueError("Pi checkpoint events need string identities and fingerprints")  # noqa: TRY003
        session_id, event_id, fingerprint = identity
        imported[(session_id, event_id)] = fingerprint
    return imported


def _checkpoint(imported: dict[tuple[str, str], str]) -> dict[str, Any]:
    return {
        "version": _CHECKPOINT_VERSION,
        "events": [
            {"session_id": session_id, "event_id": event_id, "fingerprint": fingerprint}
            for (session_id, event_id), fingerprint in imported.items()
        ],
    }


class PiSessionConnector:
    """Import one Pi branch completely, then resume from the persisted progress."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession) -> ConnectorRunCompletion:
        session_id, by_id, order = _read_archive(self.path.read_bytes())
        events = _branch_events(session_id, by_id, order, self.leaf_id)
        imported = _imported_events(session.checkpoint)
        for (stored_session, stored_event), fingerprint in imported.items():
            if stored_session != session_id:
                continue
            record = by_id.get(stored_event)
            if record is not None and _record_fingerprint(record) != fingerprint:
                raise ValueError(f"Pi event {stored_event!r} changed after it was imported")  # noqa: TRY003
        for event in events:
            identity = (event.session_id, event.event_id)
            if identity in imported:
                continue
            outcome = await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status is not ConnectorSubmissionStatus.ACCEPTED:
                continue
            imported[identity] = _record_fingerprint(by_id[event.event_id])
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=_checkpoint(imported))


__all__ = [
    "PI_CONNECTOR_VERSION",
    "PI_EVENT_SOURCE_NAME",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_IDENTITY_SCHEME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SOURCE_DEFINITION_VERSION",
    "PiEventInput",
    "PiSessionConnector",
    "PiSessionEvent",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
