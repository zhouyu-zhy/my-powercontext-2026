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

"""Count admission: start Family work from pending journal identities alone.

A Family cursor records the last journal position it committed successfully, and
journal positions are dense per Scope, so unprocessed identities are exactly
``head_position - cursor.sequence``. Replayed Source identities keep their first
position and a receipt trust upgrade rewrites the stored payload in place, so
neither adds progress. A server-owned lineage-only Source occupies a position,
therefore it helps start work while the Family's own eligibility rules still
exclude it from generation evidence.

Admission uses the existing invocation counter: one accepted request per Scope
that reaches the threshold, never a second one while an accepted request is still
unhandled, so retries of accepted work are not re-gated by the threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.tables import SOURCE_JOURNAL_HEADS_TABLE

DEFAULT_COUNT_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class CountAdmissionPage:
    """One keyset page over the Source journal heads of a deployment."""

    admitted: tuple[str, ...]
    next_scope_id: str | None
    page_full: bool


def pending_source_count(head_position: int, cursor_sequence: int) -> int:
    """Return the journal identities a Family cursor has not consumed yet."""

    remaining = int(head_position) - int(cursor_sequence)
    return remaining if remaining > 0 else 0


async def admit_count_threshold(
    connection: AsyncConnection,
    binding_name: str,
    threshold: int,
    /,
    *,
    after_scope_id: str | None = None,
    limit: int = DEFAULT_COUNT_PAGE_SIZE,
    intents: ArtifactProcessingIntentRepository | None = None,
) -> CountAdmissionPage:
    """Accept one processing invocation for every Scope at or above ``threshold``.

    The caller owns the transaction and must already hold the current lease fence;
    rows are visited in Scope order so a bounded page can rotate across restarts.
    """

    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise ValueError("source count threshold must be a positive integer")  # noqa: TRY003
    heads = SOURCE_JOURNAL_HEADS_TABLE
    statement = select(heads.c.scope_id, heads.c.position).order_by(heads.c.scope_id).limit(limit)
    if after_scope_id is not None:
        statement = statement.where(heads.c.scope_id > after_scope_id)
    rows = (await connection.execute(statement)).all()
    if not rows:
        return CountAdmissionPage((), None, False)
    scope_ids = tuple(str(row[0]) for row in rows)
    positions = (int(row[1]) for row in rows)
    cursors = await SourceCursorRepository().load_many(connection, scope_ids, binding_name)
    repository = intents or ArtifactProcessingIntentRepository()
    admitted: list[str] = []
    for scope_id, head_position in zip(scope_ids, positions, strict=True):
        stored = cursors.get(scope_id)
        pending = pending_source_count(head_position, 0 if stored is None else stored.cursor.sequence)
        if pending < threshold:
            continue
        intent = await repository.load(connection, scope_id, binding_name, for_update=True)
        if intent is None:
            intent = await repository.ensure(connection, scope_id, binding_name)
        if intent.requested_generation > intent.handled_generation:
            continue
        await repository.request(connection, scope_id, binding_name)
        admitted.append(scope_id)
    return CountAdmissionPage(tuple(admitted), scope_ids[-1], len(rows) == limit)


__all__ = [
    "DEFAULT_COUNT_PAGE_SIZE",
    "CountAdmissionPage",
    "admit_count_threshold",
    "pending_source_count",
]
