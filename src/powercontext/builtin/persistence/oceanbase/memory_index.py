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

"""OceanBase Memory candidate queries over FULLTEXT and vector indexes.

Each channel is one ordered candidate member, and a hybrid search is the two members asked for in
one statement rather than a third way of looking things up.
"""

from __future__ import annotations

from pyobvector import VECTOR, VectorIndex
from sqlalchemy import (
    BigInteger,
    Column,
    MetaData,
    Table,
    UniqueConstraint,
    bindparam,
    delete,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.mysql import match
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemoryCapabilities,
    MemoryProjection,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.canonical import embedding_content_hash, validate_embedding
from powercontext.builtin.persistence.memory_index import memory_channel_hits, memory_channel_rows
from powercontext.builtin.persistence.tables import (
    ARTIFACT_HEADS_TABLE,
    MAX_MEMORY_ENTRY_ID_LENGTH,
    MAX_MEMORY_HASH_LENGTH,
    MEMORY_ENTRY_HEADS_TABLE,
    identity_string,
)
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql
from powercontext.limits import MAX_ARTIFACT_ID_LENGTH, MAX_SCOPE_ID_LENGTH

_OCEANBASE_FTS_INDEX_NAME = "ix_pc_memory_entry_heads_fts"
_OCEANBASE_CREATE_FTS_SQL = f"""
CREATE FULLTEXT INDEX {_OCEANBASE_FTS_INDEX_NAME}
ON pc_memory_entry_heads (searchable_text) WITH PARSER SPACE
"""
_OCEANBASE_FTS_INDEX_EXISTS_SQL = text(
    """
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'pc_memory_entry_heads'
      AND index_name = :index_name
    """
)
_OCEANBASE_VECTOR_TABLE_NAME = "pc_memory_vector_entries"
_OCEANBASE_VECTOR_INDEX_NAME = "ix_pc_memory_vector_entries_embedding"
_OCEANBASE_VECTOR_TYPE_SQL = text(
    """
    SELECT data_type
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = :table_name
      AND column_name = 'embedding'
    """
)
# One candidate member per channel. Each selects the same columns in the same order and keeps its
# own ranking, so the two can run alone or be unioned into a single statement (see
# :func:`_candidate_statement`) without either definition existing twice. ``channel`` says which
# ranking a row belongs to and ``distance`` stays NULL for the lexical rows that have none.
_OCEANBASE_LEXICAL_SEARCH_SQL = """
SELECT
    'fts' AS channel,
    m.memory_artifact_id,
    m.head_revision,
    m.entry_id,
    m.entry_version_id,
    v.text,
    NULL AS distance
FROM pc_memory_entry_heads AS m
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
  AND MATCH(m.searchable_text) AGAINST(:fts_query)
ORDER BY MATCH(m.searchable_text) AGAINST(:fts_query) DESC, m.memory_artifact_id, m.entry_id, m.entry_version_id
LIMIT :candidate_limit
"""
_OCEANBASE_VECTOR_SEARCH_SQL = """
SELECT
    'vector' AS channel,
    m.memory_artifact_id,
    m.head_revision,
    m.entry_id,
    m.entry_version_id,
    v.text,
    l2_distance(m.embedding, :query_vector) AS distance
FROM pc_memory_vector_entries AS m
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE
LIMIT :candidate_limit
"""


def _candidate_statement(members: tuple[str, ...]) -> str:
    """Compose the candidate members into one statement that keeps each member's own ranking.

    seekdb evaluates every parenthesized member on its own, so the plan still shows the fulltext
    retrieval scan inside one and the vector index scan inside the other while the application pays
    for a single round trip.
    """

    return " UNION ALL ".join(f"({member})" for member in members)


def _filtered_member(member: str) -> str:
    """Constrain one candidate member to the tagged entries and order it exactly.

    The tag predicate is a correlated count over ``pc_artifact_tags``, which the approximate kNN
    path cannot pre-filter, so this drops ``APPROXIMATE`` and lets the database sort the survivors
    — the same trade the standalone vector query makes.
    """

    return member.replace("ORDER BY", memory_tag_sql("m") + "ORDER BY").replace(" APPROXIMATE", "")


def _lexical_parameters(request: MemorySearchRequest, scope_id: str, /) -> dict[str, object]:
    """Bind the members that only need the analysed query, the Scope and the requested heads."""

    return {
        "scope_id": scope_id,
        "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
        "candidate_limit": request.candidate_limit,
        "fts_query": request.analyzed_query,
        **memory_tag_parameters(request.tag_filter),
    }


def _vector_parameters(request: MemorySearchRequest, scope_id: str, /) -> dict[str, object]:
    """Bind the vector member, whose distance needs the validated query embedding."""

    if request.query_vector is None or request.embedding_profile is None:
        raise CapabilityNotSupportedError("embedding-profile")
    return {
        "scope_id": scope_id,
        "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
        "candidate_limit": request.candidate_limit,
        "query_vector": validate_embedding(
            request.query_vector,
            dimension=request.embedding_profile.dimension,
        ),
        **memory_tag_parameters(request.tag_filter),
    }


def _candidate_query(member: str, dimension: int | None = None) -> TextClause:
    """Prepare one composed candidate statement with the bind types its markers require.

    Head ids and tag keys expand, and a vector marker needs the embedding column's dimension; a
    member without that marker is not given a type it never asks for, so no call site ever binds a
    parameter its statement does not contain.
    """

    statement = text(member).bindparams(bindparam("memory_artifact_ids", expanding=True))
    if ":tag_keys" in member:
        statement = statement.bindparams(bindparam("tag_keys", expanding=True))
    if ":tag_hashes" in member:
        statement = statement.bindparams(bindparam("tag_hashes", expanding=True))
    if ":query_vector" in member:
        if dimension is None:
            raise CapabilityNotSupportedError("embedding-profile")
        statement = statement.bindparams(bindparam("query_vector", type_=VECTOR(dimension)))
    return statement


class OceanBaseMemoryFTSIndex:
    """OceanBase FULLTEXT strategy over the relational active-head projection."""

    capabilities = MemoryCapabilities(fts=True, tag_filter=True)
    tables: tuple[Table, ...] = ()

    def __init__(self) -> None:
        self._search_sql = _candidate_query(_OCEANBASE_LEXICAL_SEARCH_SQL)
        self._tagged_search_sql = _candidate_query(_filtered_member(_OCEANBASE_LEXICAL_SEARCH_SQL))

    async def initialize(self, connection: AsyncConnection, /) -> None:
        if connection.dialect.name != "mysql":
            raise CapabilityNotSupportedError("oceanbase-fts")
        count = await connection.scalar(
            _OCEANBASE_FTS_INDEX_EXISTS_SQL,
            {"index_name": _OCEANBASE_FTS_INDEX_NAME},
        )
        if count == 0:
            await connection.exec_driver_sql(_OCEANBASE_CREATE_FTS_SQL)
        probe = match(MEMORY_ENTRY_HEADS_TABLE.c.searchable_text, against="powercontext")
        await connection.execute(select(MEMORY_ENTRY_HEADS_TABLE.c.entry_version_id).where(probe).limit(1))

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        del connection, scope_id, memory_ref, projections

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        del connection, scope_id, memory_ref, entry_ids

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        del connection, scope_id, memory_ref, projections

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode != "fts":
            return MemorySearchChannels()
        rows = (
            await connection.execute(
                self._search_sql if request.tag_filter is None else self._tagged_search_sql,
                _lexical_parameters(request, scope_id),
            )
        ).mappings()
        return MemorySearchChannels(fts=memory_channel_hits(rows, request.memories))

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        del connection, scope_id, memories, profile
        return False

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        del connection, scope_id
        return projections


class OceanBaseMemoryVectorIndex:
    """OceanBase HNSW strategy over rebuildable active-head embeddings.

    It also owns the native hybrid candidate query: because the vector member carries the
    approximate kNN, only this strategy can put the lexical member beside it, so a ``hybrid``
    request reaches the database exactly once and the composite routes it here alone.
    """

    def __init__(self, profile: EmbeddingProfile) -> None:
        if profile.dimension < 1 or profile.distance != "l2" or profile.normalization != "unit":
            raise CapabilityNotSupportedError(
                "vector",
                "OceanBase requires a positive unit-normalized L2 embedding profile",
            )
        self.profile = profile
        self.capabilities = MemoryCapabilities(
            tag_filter=True,
            fts=False,
            vector=True,
            hybrid=True,
            embedding_profile=profile,
        )
        metadata = MetaData()
        self.table = Table(
            _OCEANBASE_VECTOR_TABLE_NAME,
            metadata,
            Column("vector_id", BigInteger, primary_key=True, autoincrement=True),
            Column("scope_id", identity_string(MAX_SCOPE_ID_LENGTH), nullable=False),
            Column("memory_artifact_id", identity_string(MAX_ARTIFACT_ID_LENGTH), nullable=False),
            Column("head_revision", BigInteger, nullable=False),
            Column("entry_id", identity_string(MAX_MEMORY_ENTRY_ID_LENGTH), nullable=False),
            Column("entry_version_id", identity_string(MAX_MEMORY_ENTRY_ID_LENGTH), nullable=False),
            Column("entry_content_hash", identity_string(MAX_MEMORY_HASH_LENGTH), nullable=False),
            Column("embedding_content_hash", identity_string(MAX_MEMORY_HASH_LENGTH), nullable=False),
            Column("embedding", VECTOR(profile.dimension), nullable=False),
            UniqueConstraint(
                "scope_id",
                "memory_artifact_id",
                "entry_id",
                name="uq_pc_memory_vector_entries_head",
            ),
        )
        self.tables: tuple[Table, ...] = (self.table,)
        self._vector_index = VectorIndex(
            _OCEANBASE_VECTOR_INDEX_NAME,
            self.table.c.embedding,
            params="distance=l2,type=hnsw",
        )
        self._search_sql = _candidate_query(_OCEANBASE_VECTOR_SEARCH_SQL, profile.dimension)
        self._tagged_search_sql = _candidate_query(
            _filtered_member(_OCEANBASE_VECTOR_SEARCH_SQL),
            profile.dimension,
        )
        # One statement for both channels, composed from the very members the single-channel
        # searches run, so a hybrid query asks the database once and still receives each channel's
        # own ranking. Nothing about either channel is re-expressed for it.
        self._hybrid_sql = _candidate_query(
            _candidate_statement((_OCEANBASE_LEXICAL_SEARCH_SQL, _OCEANBASE_VECTOR_SEARCH_SQL)),
            profile.dimension,
        )
        self._hybrid_tagged_sql = _candidate_query(
            _candidate_statement(
                (
                    _filtered_member(_OCEANBASE_LEXICAL_SEARCH_SQL),
                    _filtered_member(_OCEANBASE_VECTOR_SEARCH_SQL),
                )
            ),
            profile.dimension,
        )

    async def initialize(self, connection: AsyncConnection, /) -> None:
        if connection.dialect.name != "mysql":
            raise CapabilityNotSupportedError("oceanbase-vector")
        column_type = await connection.scalar(
            _OCEANBASE_VECTOR_TYPE_SQL,
            {"table_name": _OCEANBASE_VECTOR_TABLE_NAME},
        )
        expected = f"VECTOR({self.profile.dimension})"
        if str(column_type).upper() != expected:
            raise CapabilityNotSupportedError(
                "vector",
                f"OceanBase projection uses {column_type!r}; expected {expected}",
            )
        await connection.run_sync(lambda sync_connection: self._vector_index.create(sync_connection, checkfirst=True))

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await connection.execute(
            delete(self.table).where(
                self.table.c.scope_id == scope_id,
                self.table.c.memory_artifact_id == memory_ref.artifact_id,
            )
        )
        await self.upsert(connection, scope_id, memory_ref, projections)

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        if not entry_ids:
            return
        await connection.execute(
            delete(self.table).where(
                self.table.c.scope_id == scope_id,
                self.table.c.memory_artifact_id == memory_ref.artifact_id,
                self.table.c.entry_id.in_(entry_ids),
            )
        )

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        values = []
        for projection in projections:
            if projection.embedding is None or projection.embedding_content_hash is None:
                continue
            entry = projection.entry_version
            values.append({
                "scope_id": scope_id,
                "memory_artifact_id": memory_ref.artifact_id,
                "head_revision": memory_ref.revision,
                "entry_id": entry.entry_id,
                "entry_version_id": entry.entry_version_id,
                "entry_content_hash": entry.entry_content_hash,
                "embedding_content_hash": projection.embedding_content_hash,
                "embedding": validate_embedding(
                    projection.embedding,
                    dimension=self.profile.dimension,
                ),
            })
        if values:
            await connection.execute(insert(self.table), values)

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode not in {"vector", "hybrid"}:
            return MemorySearchChannels()
        if request.embedding_profile != self.profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self.vector_complete(connection, scope_id, request.memories, self.profile):
            raise CapabilityNotSupportedError("vector")
        if request.mode == "hybrid":
            rows = (
                await connection.execute(
                    self._hybrid_sql if request.tag_filter is None else self._hybrid_tagged_sql,
                    {**_vector_parameters(request, scope_id), "fts_query": request.analyzed_query},
                )
            ).mappings()
            return memory_channel_rows(rows, request.memories)
        rows = (
            await connection.execute(
                self._search_sql if request.tag_filter is None else self._tagged_search_sql,
                _vector_parameters(request, scope_id),
            )
        ).mappings()
        return MemorySearchChannels(vector=memory_channel_hits(rows, request.memories))

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        if profile != self.profile:
            return False
        for memory in memories:
            current = await connection.scalar(
                select(ARTIFACT_HEADS_TABLE.c.revision).where(
                    ARTIFACT_HEADS_TABLE.c.scope_id == scope_id,
                    ARTIFACT_HEADS_TABLE.c.family == "memory",
                    ARTIFACT_HEADS_TABLE.c.artifact_id == memory.artifact_id,
                )
            )
            if current is None or int(current) != memory.revision:
                # The caller validated this head before the search. If it moved while
                # this query ran, report a stale head so the runtime can retry with it.
                raise CapabilityNotSupportedError("head")
            heads = (
                await connection.execute(
                    select(
                        MEMORY_ENTRY_HEADS_TABLE.c.entry_id,
                        MEMORY_ENTRY_HEADS_TABLE.c.entry_version_id,
                        MEMORY_ENTRY_HEADS_TABLE.c.entry_content_hash,
                    ).where(
                        MEMORY_ENTRY_HEADS_TABLE.c.scope_id == scope_id,
                        MEMORY_ENTRY_HEADS_TABLE.c.memory_artifact_id == memory.artifact_id,
                    )
                )
            ).all()
            metadata = (
                await connection.execute(
                    select(
                        self.table.c.entry_id,
                        self.table.c.entry_version_id,
                        self.table.c.entry_content_hash,
                        self.table.c.embedding_content_hash,
                    ).where(
                        self.table.c.scope_id == scope_id,
                        self.table.c.memory_artifact_id == memory.artifact_id,
                    )
                )
            ).all()
            expected = {(str(row[0]), str(row[1]), str(row[2])) for row in heads}
            actual = {(str(row[0]), str(row[1]), str(row[2])) for row in metadata}
            if actual != expected:
                return False
            if any(str(row[3]) != _embedding_hash(self.profile, str(row[2])) for row in metadata):
                return False
        return True

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        hydrated: list[MemoryProjection] = []
        for projection in projections:
            entry = projection.entry_version
            row = (
                await connection.execute(
                    select(
                        self.table.c.entry_content_hash,
                        self.table.c.embedding_content_hash,
                        self.table.c.embedding,
                    ).where(
                        self.table.c.scope_id == scope_id,
                        self.table.c.memory_artifact_id == entry.memory_artifact_id,
                        self.table.c.entry_id == entry.entry_id,
                        self.table.c.entry_version_id == entry.entry_version_id,
                    )
                )
            ).one_or_none()
            if (
                row is None
                or str(row[0]) != entry.entry_content_hash
                or str(row[1]) != _embedding_hash(self.profile, entry.entry_content_hash)
            ):
                hydrated.append(projection)
                continue
            embedding = validate_embedding(
                tuple(float(value) for value in row[2]),
                dimension=self.profile.dimension,
            )
            hydrated.append(
                MemoryProjection(
                    entry_version=entry,
                    searchable_text=projection.searchable_text,
                    embedding=embedding,
                    embedding_content_hash=str(row[1]),
                )
            )
        return tuple(hydrated)


def _embedding_hash(profile: EmbeddingProfile, entry_hash: str) -> str:
    return embedding_content_hash(
        profile_id=profile.profile_id,
        model=profile.model,
        dimension=profile.dimension,
        distance=profile.distance,
        normalization=profile.normalization,
        entry_content_hash=entry_hash,
    )
