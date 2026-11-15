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

"""Source lifecycle: propagate Source withdrawal to the evidence derived from it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    Memory,
    MemoryCitation,
    MemoryEntryVersion,
    MemoryService,
)
from powercontext.builtin.persistence.records import RelationalRecordService
from powercontext.builtin.persistence.source_lifecycle import (
    MEMORY_NODE,
    SOURCE_NODE,
    TOPIC_NODE,
    LifecycleNodeRow,
    SourceLifecycleRepository,
    encode_payload,
)
from powercontext.builtin.records import BaseAccessError
from powercontext.errors import ArtifactError, ArtifactNotFoundError
from powercontext.sources import SourceRef

LifecyclePurpose = Literal["generation", "audit"]
LifecycleReference = Union[SourceRef, MemoryCitation, ArtifactRef]  # noqa: UP007

TOPIC_MEMORY_FAMILY = "topic-memory"
PROPAGATION_REASON = "source lifecycle propagation"
MAX_NODE_ID_LENGTH = 128


class SourceLifecycleError(ValueError):
    """A lifecycle binding, event, or eligibility check violated its contract."""


class SourceLifecycleEvent(BaseModel):
    """One caller-supplied availability decision about a registered Source."""

    model_config = ConfigDict(extra="forbid")

    node_id: Annotated[str, Field(strict=True)]
    revision: Annotated[int, Field(strict=True, gt=0)]
    sequence: Annotated[int, Field(strict=True, gt=0)]
    available: Annotated[bool, Field(strict=True)]
    reason: Annotated[str, Field(strict=True)]

    @field_validator("node_id", "reason")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @property
    def digest(self) -> bytes:
        return event_digest(self)


def event_digest(event: SourceLifecycleEvent) -> bytes:
    payload = "|".join(
        (
            event.node_id,
            str(event.revision),
            str(event.sequence),
            "1" if event.available else "0",
            event.reason,
        )
    )
    return sha256(payload.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class SourceLifecycleBindings:
    """Durable graph plus the native evidence services one Scope's lifecycle writes through."""

    scope_id: str
    graph: SourceLifecycleRepository
    memory: Callable[[], MemoryService]
    memory_artifact_id: str
    records: RelationalRecordService
    write_lock: asyncio.Lock


class SourceLifecycleService:
    """Single-writer evidence-dependency graph for one Scope.

    Registration binds caller-provided evidence under declared parents; events move Sources. Both
    paths recompute availability from the whole graph instead of applying an increment, so a half
    finished propagation is repaired by the next registration or the retried event, and a replay of
    a settled event changes nothing — including the Memory revision counter.
    """

    def __init__(self, bindings: SourceLifecycleBindings, /) -> None:
        self._bindings = bindings

    @property
    def scope_id(self) -> str:
        return self._bindings.scope_id

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        """Bind one captured Source and the lifecycle revision its events must carry."""

        node_id = _validated_node_id(node_id)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise SourceLifecycleError("invalid-revision")
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source-reference")
        try:
            await self._bindings.records.get_source(
                self.scope_id,
                source_ref.source_type,
                source_ref.source_id,
            )
        except (BaseAccessError, LookupError, ValueError) as error:
            raise SourceLifecycleError("source-unreadable") from error
        await self._register(
            LifecycleNodeRow(
                node_id=node_id,
                kind=SOURCE_NODE,
                reference=encode_payload(source_ref.model_dump(mode="json")),
                parents=(),
                policy="none",
                lifecycle_revision=revision,
                event_sequence=0,
                event_digest=_NO_DIGEST,
                available=True,
                artifact_id=None,
            )
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Bind one Memory entry version to the evidence supporting its whole body."""

        node_id = _validated_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory-reference")
        try:
            memory = await self._bindings.memory().revision(citation.memory_ref)
        except (ArtifactError, LookupError, ValueError) as error:
            raise SourceLifecycleError("memory-unreadable") from error
        if not any(
            item.entry_id == citation.entry_id and item.entry_version_id == citation.entry_version_id
            for item in memory.content.manifest.entries
        ):
            raise SourceLifecycleError("memory-unreadable")
        await self._register(
            self._derived_row(
                node_id,
                MEMORY_NODE,
                encode_payload(citation.model_dump(mode="json")),
                parents,
                policy,
                citation.memory_ref.artifact_id,
            )
        )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Bind one published Topic Memory revision to the evidence behind its summary."""

        node_id = _validated_node_id(node_id)
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != TOPIC_MEMORY_FAMILY:
            raise SourceLifecycleError("topic-reference")
        try:
            await self._bindings.records.get_artifact_revision(
                self.scope_id,
                artifact_ref.family,
                artifact_ref.artifact_id,
                artifact_ref.revision,
            )
        except (BaseAccessError, LookupError, ValueError) as error:
            raise SourceLifecycleError("topic-unreadable") from error
        await self._register(
            self._derived_row(
                node_id,
                TOPIC_NODE,
                encode_payload(artifact_ref.model_dump(mode="json")),
                parents,
                policy,
                artifact_ref.artifact_id,
            )
        )

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        """Record one Source decision, propagate it, and return every node that stays available."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event-type")
        async with self._bindings.write_lock:
            rows = _by_identity(await self._bindings.graph.load(self.scope_id))
            node = rows.get(event.node_id)
            if node is None or node.kind != SOURCE_NODE:
                raise SourceLifecycleError("unknown-source")
            if node.lifecycle_revision != event.revision:
                raise SourceLifecycleError("revision-mismatch")
            settled = node.event_sequence == event.sequence and node.event_digest == event.digest
            if event.sequence < node.event_sequence or (event.sequence == node.event_sequence and not settled):
                raise SourceLifecycleError("sequence-regression")
            if event.sequence > node.event_sequence:
                rows[event.node_id] = _with_event(node, event)
            await self._bindings.graph.apply_updates(
                self.scope_id,
                availability=propagate(rows.values()),
                events=((node.node_id, event.sequence, event.digest, node.lifecycle_revision),),
            )
            availability = propagate(_by_identity(await self._bindings.graph.load(self.scope_id)).values())
            await self._synchronize(availability)
            return sorted(node_id for node_id, available in availability.items() if available)

    async def resolve(self, node_id: str, /, *, purpose: LifecyclePurpose) -> LifecycleReference:
        """Return the exact registered reference, withheld from generation when unsupported."""

        node_id = _validated_node_id(node_id)
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("unknown-purpose")
        rows = _by_identity(await self._bindings.graph.load(self.scope_id))
        node = rows.get(node_id)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "generation" and not propagate(rows.values())[node_id]:
            raise SourceLifecycleError("withdrawn")
        return _decode_reference(node)

    async def _register(self, row: LifecycleNodeRow, /) -> None:
        async with self._bindings.write_lock:
            rows = _by_identity(await self._bindings.graph.load(self.scope_id))
            existing = rows.get(row.node_id)
            if existing is not None:
                if not _same_binding(existing, row):
                    raise SourceLifecycleError("identity-conflict")
                await self._synchronize(propagate(rows.values()))
                return
            if row.kind != SOURCE_NODE:
                _require_parents(rows, row)
            rows[row.node_id] = row
            availability = propagate(rows.values())
            await self._bindings.graph.insert(self.scope_id, row)
            await self._bindings.graph.apply_updates(self.scope_id, availability=availability)
            await self._synchronize(availability)

    def _derived_row(
        self,
        node_id: str,
        kind: str,
        reference: str,
        parents: Sequence[str],
        policy: str,
        artifact_id: str,
    ) -> LifecycleNodeRow:
        if policy not in ("all", "any"):
            raise SourceLifecycleError("unknown-policy")
        return LifecycleNodeRow(
            node_id=node_id,
            kind=kind,
            reference=reference,
            parents=tuple(_validated_node_id(parent) for parent in parents),
            policy=policy,
            lifecycle_revision=0,
            event_sequence=0,
            event_digest=_NO_DIGEST,
            # Availability is always derived from parents; this is the pre-propagation value.
            available=True,
            artifact_id=artifact_id,
        )

    async def _synchronize(self, availability: Mapping[str, bool]) -> None:
        """Move native Memory state onto the availability the graph already records."""

        rows = _by_identity(await self._bindings.graph.load(self.scope_id))
        desired: dict[tuple[str, str], bool] = {}
        for node_id, available in availability.items():
            row = rows.get(node_id)
            if row is None or row.kind != MEMORY_NODE:
                continue
            citation = _decode_reference(row)
            identity = (citation.entry_id, citation.entry_version_id)
            # One body bound under several node ids stays available while any of them is supported.
            desired[identity] = desired.get(identity, False) or bool(available)
        if not desired:
            return
        service = self._bindings.memory()
        for target in ("inactive", "active"):
            head = await _memory_head(service, self._bindings.memory_artifact_id)
            if head is None:
                return
            states = {(item.entry_id, item.entry_version_id): item.state for item in head.content.manifest.entries}
            versions = {(entry.entry_id, entry.entry_version_id): entry for entry in await service.entries(head)}
            selected: list[MemoryEntryVersion] = []
            for identity, available in desired.items():
                if ("active" if available else "inactive") != target:
                    continue
                if states.get(identity) == target:
                    continue
                entry = versions.get(identity)
                if entry is None:
                    # The bound body version is no longer the current pointer; history stays readable.
                    continue
                selected.append(entry)
            if not selected:
                continue
            ordered = tuple(
                sorted(selected, key=lambda entry: (entry.entry_id.encode("utf-8"), entry.entry_version_id))
            )
            transition = service.forget if target == "inactive" else service.reactivate
            await transition(head, entries=ordered, reason=PROPAGATION_REASON)


_NO_DIGEST = b"\x00" * 32


def propagate(rows: Sequence[LifecycleNodeRow]) -> dict[str, bool]:
    """Compute every node's availability from Source decisions plus each all/any policy."""

    by_id = _by_identity(rows)
    availability: dict[str, bool] = {
        node_id: bool(row.available) for node_id, row in by_id.items() if row.kind == SOURCE_NODE
    }
    pending = {node_id: row for node_id, row in by_id.items() if row.kind != SOURCE_NODE}
    while pending:
        resolved = tuple(
            node_id for node_id, row in pending.items() if all(parent in availability for parent in row.parents)
        )
        if not resolved:
            raise SourceLifecycleError("cycle")
        for node_id in resolved:
            row = pending.pop(node_id)
            support = [availability[parent] for parent in row.parents]
            availability[node_id] = all(support) if row.policy == "all" else any(support)
    return availability


def _by_identity(rows: Sequence[LifecycleNodeRow]) -> dict[str, LifecycleNodeRow]:
    return {row.node_id: row for row in rows}


def _with_event(row: LifecycleNodeRow, event: SourceLifecycleEvent) -> LifecycleNodeRow:
    return LifecycleNodeRow(
        node_id=row.node_id,
        kind=row.kind,
        reference=row.reference,
        parents=row.parents,
        policy=row.policy,
        lifecycle_revision=row.lifecycle_revision,
        event_sequence=event.sequence,
        event_digest=event.digest,
        available=event.available,
        artifact_id=row.artifact_id,
    )


def _same_binding(existing: LifecycleNodeRow, row: LifecycleNodeRow) -> bool:
    return (
        existing.kind == row.kind
        and existing.reference == row.reference
        # all/any policies are order-free, so a restated parent list is the same binding.
        and sorted(existing.parents) == sorted(row.parents)
        and existing.policy == row.policy
        and (existing.kind != SOURCE_NODE or existing.lifecycle_revision == row.lifecycle_revision)
    )


def _require_parents(rows: Mapping[str, LifecycleNodeRow], row: LifecycleNodeRow) -> None:
    if not row.parents:
        raise SourceLifecycleError("missing-parents")
    if len(set(row.parents)) != len(row.parents):
        raise SourceLifecycleError("duplicate-parent")
    if row.node_id in row.parents:
        raise SourceLifecycleError("self-parent")
    for parent in row.parents:
        if parent not in rows:
            raise SourceLifecycleError("unknown-parent")
        if _reaches(rows, row.node_id, parent):
            raise SourceLifecycleError("cycle")


def _reaches(rows: Mapping[str, LifecycleNodeRow], start: str, target: str) -> bool:
    """Report whether ``target`` is already a descendant of ``start``."""

    children: dict[str, list[str]] = {}
    for row in rows.values():
        for parent in row.parents:
            children.setdefault(parent, []).append(row.node_id)
    seen: set[str] = set()
    queue = [start]
    while queue:
        node_id = queue.pop()
        if node_id == target:
            return True
        if node_id in seen:
            continue
        seen.add(node_id)
        queue.extend(children.get(node_id, ()))
    return False


def _decode_reference(row: LifecycleNodeRow) -> Any:  # noqa: ANN401
    if row.kind == SOURCE_NODE:
        return SourceRef.model_validate_json(row.reference)
    if row.kind == MEMORY_NODE:
        return MemoryCitation.model_validate_json(row.reference)
    return ArtifactRef.model_validate_json(row.reference)


async def _memory_head(service: MemoryService, artifact_id: str) -> Memory | None:
    try:
        return await service.head(artifact_id)
    except ArtifactNotFoundError:
        return None


def _validated_node_id(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool) or not value.strip():
        raise SourceLifecycleError("invalid-node-id")
    if len(value) > MAX_NODE_ID_LENGTH:
        raise SourceLifecycleError("invalid-node-id")
    return value


__all__ = (
    "SourceLifecycleBindings",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "SourceLifecycleService",
    "propagate",
)
