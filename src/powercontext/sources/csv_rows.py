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

"""Row-level Sources captured from an offline CSV table.

One physical data record becomes one ``CAPTURED`` Source whose identity is the
document plus the primary-key value, so a retrieval citation always points back
to a single complete record regardless of ordering or restarts.
"""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, JsonValue, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
CSV_IDENTITY_SCHEME = "csv"
_UTF8_BOM = chr(0xFEFF)


def _require_document_component(name: str, value: str) -> str:
    """Document components are identity inputs, so blanks and padding are rejected."""
    if not value or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")  # noqa: TRY003
    if value != value.strip():
        raise ValueError(f"{name} must not contain leading or trailing whitespace")  # noqa: TRY003
    return value


def _require_primary_key_value(value: str) -> str:
    """Primary-key values keep their exact bytes, including any inner padding."""
    if not value.strip():
        raise ValueError("primary_key_value must not be blank")  # noqa: TRY003
    return value


class CSVDocumentInput(BaseModel):
    """One whole CSV document read offline together with its record identity."""

    document_id: str
    primary_key: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _require_document_component("document_id", value)

    @field_validator("primary_key")
    @classmethod
    def validate_primary_key(cls, value: str) -> str:
        if not value:
            raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003
        return value


class CSVRowInput(BaseModel):
    """One validated data record with the header order preserved."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _require_document_component("document_id", value)

    @field_validator("primary_key_value")
    @classmethod
    def validate_primary_key_value(cls, value: str) -> str:
        return _require_primary_key_value(value)

    @model_validator(mode="after")
    def validate_row_shape(self) -> CSVRowInput:
        if len(self.values) != len(self.columns):
            raise ValueError("a CSV row must carry exactly one value per header column")  # noqa: TRY003
        if not self.columns:
            raise ValueError("a CSV row must carry at least one column")  # noqa: TRY003
        return self


class CSVRowSource(Source):
    """Durable identity and field values of one captured CSV row."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _require_document_component("source_id", value)

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _require_document_component("document_id", value)

    @field_validator("primary_key_value")
    @classmethod
    def validate_primary_key_value(cls, value: str) -> str:
        return _require_primary_key_value(value)


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the delimiter-safe identity of one row inside one document."""
    _require_document_component("document_id", document_id)
    _require_primary_key_value(primary_key_value)
    source_id = f"{CSV_IDENTITY_SCHEME}:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    _require_identity_length(source_id)
    return source_id


def _require_identity_length(source_id: str) -> None:
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"Source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003


def _decode_document(content: bytes) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error  # noqa: TRY003
    return text.removeprefix(_UTF8_BOM)


def _read_records(text: str) -> list[list[str]]:
    stream = io.StringIO(text, newline="")
    try:
        return list(csv.reader(stream, strict=True))
    except csv.Error as error:
        raise ValueError(f"CSV document is malformed: {error}") from error  # noqa: TRY003


def _resolve_primary_key(header: list[str], primary_key: str) -> int:
    if not header:
        raise ValueError("CSV header record must not be empty")  # noqa: TRY003
    for column in header:
        if not column.strip():
            raise ValueError("CSV header columns must not be blank")  # noqa: TRY003
    if len(set(header)) != len(header):
        raise ValueError("CSV header columns must be unique")  # noqa: TRY003
    for index, column in enumerate(header):
        if column == primary_key:
            return index
    raise ValueError(f"CSV header has no column exactly named {primary_key!r}")  # noqa: TRY003


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and validate a whole document, returning one row input per data record.

    Nothing is returned until every record passes, so a rejected document never
    leaves a caller with partially captured rows.
    """
    records = _read_records(_decode_document(value.content))
    if not records:
        raise ValueError("CSV document must contain a header record")  # noqa: TRY003
    header = records[0]
    key_index = _resolve_primary_key(header, value.primary_key)
    columns = tuple(header)
    rows: list[CSVRowInput] = []
    seen: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(columns):
            raise ValueError(  # noqa: TRY003
                f"CSV record has {len(record)} fields but the header declares {len(columns)}"
            )
        primary_key_value = record[key_index]
        _require_primary_key_value(primary_key_value)
        if primary_key_value in seen:
            raise ValueError(f"CSV primary key {primary_key_value!r} is not unique")  # noqa: TRY003
        seen.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return tuple(rows)


class CSVRowSourceAdapter:
    """Turn validated rows into Sources and read them back without re-parsing."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            materialization=SourceMaterialization.CAPTURED,
            source_id=source_id,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=tuple(value.columns),
            values=tuple(value.values),
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=tuple(source.columns),
            values=tuple(source.values),
        )


class CSVRowTextEvidenceProjection:
    """Expose one row as searchable text evidence with its field mapping."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row: dict[str, JsonValue] = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION: AdapterSourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    projections=(CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
