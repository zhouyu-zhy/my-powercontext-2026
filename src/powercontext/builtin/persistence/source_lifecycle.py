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

"""Durable evidence-dependency graph that Source lifecycle events propagate over."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE

SOURCE_NODE = "source"
MEMORY_NODE = "memory"
TOPIC_NODE = "topic"
NODE_KINDS = (SOURCE_NODE, MEMORY_NODE, TOPIC_NODE)


@dataclass(frozen=True, slots=True)
class LifecycleNodeRow:
    """One persisted dependency node, still carrying its encoded reference payload."""

    node_id: str
    kind: str
    reference: str
    parents: tuple[str, ...]
    policy: str
    lifecycle_revision: int
    event_sequence: int
    event_digest: bytes
    available: bool
    artifact_id: str | None


def encode_payload(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_payload(text: str) -> object:
    return json.loads(text)


class SourceLifecycleRepository:
    """Read and write the per-Scope dependency graph without interpreting it."""

    def __init__(self, database: AsyncDatabase, /) -> None:
        self._database = database

    async def load(self, scope_id: str, /) -> tuple[LifecycleNodeRow, ...]:
        async with self._database.connection() as connection:
            return await self._load(connection, scope_id)

    async def _load(self, connection: AsyncConnection, scope_id: str) -> tuple[LifecycleNodeRow, ...]:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        rows = (
            (await connection.execute(select(table).where(table.c.scope_id == scope_id).order_by(table.c.node_id)))
            .mappings()
            .all()
        )
        return tuple(
            LifecycleNodeRow(
                node_id=str(row["node_id"]),
                kind=str(row["kind"]),
                reference=str(row["reference"]),
                parents=tuple(json.loads(str(row["parents"]))),
                policy=str(row["policy"]),
                lifecycle_revision=int(row["lifecycle_revision"]),
                event_sequence=int(row["event_sequence"]),
                event_digest=bytes(row["event_digest"]),
                available=bool(row["available"]),
                artifact_id=None if row["artifact_id"] is None else str(row["artifact_id"]),
            )
            for row in rows
        )

    async def insert(self, scope_id: str, row: LifecycleNodeRow, /) -> None:
        async with self._database.transaction() as connection:
            await connection.execute(
                SOURCE_LIFECYCLE_NODES_TABLE.insert().values(
                    scope_id=scope_id,
                    node_id=row.node_id,
                    kind=row.kind,
                    reference=row.reference,
                    parents=encode_payload(list(row.parents)),
                    policy=row.policy,
                    lifecycle_revision=row.lifecycle_revision,
                    event_sequence=row.event_sequence,
                    event_digest=row.event_digest,
                    available=row.available,
                    artifact_id=row.artifact_id,
                )
            )

    async def apply_updates(
        self,
        scope_id: str,
        /,
        *,
        availability: Mapping[str, bool],
        events: Sequence[tuple[str, int, bytes, int]] = (),
    ) -> None:
        """Persist one propagation step: recomputed availability plus event bookkeeping.

        ``events`` carries ``(node_id, event_sequence, event_digest, lifecycle_revision)`` for the
        Sources an event touched, so a replay can be recognised without re-deriving state.
        """

        async with self._database.transaction() as connection:
            table = SOURCE_LIFECYCLE_NODES_TABLE
            for node_id, sequence, digest, revision in events:
                await connection.execute(
                    update(table)
                    .where(table.c.scope_id == scope_id, table.c.node_id == node_id)
                    .values(event_sequence=sequence, event_digest=digest, lifecycle_revision=revision)
                )
            for available, node_ids in _group_by_flag(availability):
                await connection.execute(
                    update(table)
                    .where(table.c.scope_id == scope_id, table.c.node_id.in_(node_ids))
                    .values(available=available)
                )

    async def topic_visibility(self, scope_id: str, /) -> tuple[frozenset[str], frozenset[str]]:
        """Return ``(bound, available)`` artifact identities for Topic Memory nodes."""

        async with self._database.connection() as connection:
            table = SOURCE_LIFECYCLE_NODES_TABLE
            rows = (
                await connection.execute(
                    select(table.c.artifact_id, table.c.available).where(
                        table.c.scope_id == scope_id,
                        table.c.kind == TOPIC_NODE,
                        table.c.artifact_id.is_not(None),
                    )
                )
            ).all()
        bound = frozenset(str(row[0]) for row in rows)
        available = frozenset(str(row[0]) for row in rows if bool(row[1]))
        return bound, available


def _group_by_flag(availability: Mapping[str, bool]) -> tuple[tuple[bool, tuple[str, ...]], ...]:
    active = tuple(sorted(node_id for node_id, flag in availability.items() if flag))
    inactive = tuple(sorted(node_id for node_id, flag in availability.items() if not flag))
    return tuple(
        (flag, node_ids) for flag, node_ids in ((True, active), (False, inactive)) if node_ids
    )
