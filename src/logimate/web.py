import json
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from fastapi import Depends, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

log = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name("web_assets")
Kind = Literal["logistics", "rag", "grounding"]
Mode = Literal["demo", "deepseek"]
Digest = Annotated[str, StringConstraints(pattern="^[a-f0-9]{64}$")]


class ArchiveRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Group(ArchiveRecord):
    name: str
    sample_count: Annotated[int, Field(ge=1)]
    metrics: dict[str, float | None]


class CaseResult(ArchiveRecord):
    group: str
    actual: str
    correct: bool


class Case(ArchiveRecord):
    case_id: str
    scenario: str
    expected: str
    results: Annotated[list[CaseResult], Field(min_length=1)]


class Evaluation(ArchiveRecord):
    evaluation_id: str
    kind: Kind
    mode: Mode
    model_id: str | None
    data_version: str
    data_hash: Digest
    code_ref: str
    config_hash: Digest
    baseline_group: str
    groups: Annotated[list[Group], Field(min_length=1)]
    case_examples: Annotated[list[Case], Field(max_length=5)]
    artifact_path: str
    created_at: str

    @field_validator("artifact_path")
    @classmethod
    def relative_artifact(cls, value):
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
            raise ValueError("Expected a project-relative artifact path")
        return value

    @model_validator(mode="after")
    def known_groups(self):
        names = {group.name for group in self.groups}
        if self.baseline_group not in names or len(names) != len(self.groups):
            raise ValueError("Invalid evaluation groups")
        if any(result.group not in names for case in self.case_examples for result in case.results):
            raise ValueError("Unknown case group")
        return self


def load_archive(directory, kind, mode):
    reports = {}
    for path in sorted(directory.glob("evaluation-*.json")):
        try:
            if path.is_symlink() or path.stat().st_size > 256_000:
                continue
            item = Evaluation.model_validate_json(path.read_text(encoding="utf-8"))
            if (kind is None or item.kind == kind) and (mode is None or item.mode == mode):
                reports[item.evaluation_id] = item
        except (OSError, ValidationError, UnicodeError):
            log.warning("Invalid evaluation archive file skipped")
    return sorted(reports.values(), key=lambda item: (item.created_at, item.evaluation_id), reverse=True)


def mount_workbench(app, settings, identity, error):
    cursor_signer = URLSafeTimedSerializer(settings.secret, salt="logimate-evaluation-cursor")
    archive_directory = Path(os.getenv("LOGIMATE_EVALUATION_DIR", ".logimate/evaluations"))
    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")

    @app.get("/", include_in_schema=False)
    def workbench():
        return FileResponse(ASSETS / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        })

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/api/v1/evaluations")
    def evaluations(actor=Depends(identity), kind: Kind | None = None, mode: Mode | None = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None):
        scope = [actor["tenant_id"], actor["user_id"], kind, mode]
        before = None
        if cursor:
            try:
                decoded = cursor_signer.loads(cursor, max_age=8 * 3600)
                if decoded["scope"] != scope:
                    raise ValueError("Invalid archive cursor")
                before = tuple(decoded["before"])
            except (BadSignature, ValueError, KeyError):
                raise error(422, "invalid_input", "评测归档游标不可用。") from None
        items = load_archive(archive_directory, kind, mode)
        if before:
            items = [item for item in items if (item.created_at, item.evaluation_id) < before]
        page = items[:limit]
        token = cursor_signer.dumps({"scope": scope, "before": [page[-1].created_at, page[-1].evaluation_id]}) if (
            len(items) > limit) else None
        return {"items": page, "next_cursor": token}
