import logging
import os
from typing import Annotated
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, Header
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from . import store
from .workflow import DeepSeek, build_workflow, mcp_invoker, utc_now

log = logging.getLogger(__name__)
Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Key = Annotated[str | None, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


def mount_runs(app, engine, settings, identity, origin, error):
    mode = os.getenv("LOGIMATE_MODE", "demo")
    if mode not in ("demo", "deepseek"):
        raise ValueError("LOGIMATE_MODE must be demo or deepseek")
    signing_key = os.getenv("LOGIMATE_MCP_SIGNING_KEY")
    model = DeepSeek(os.getenv("DEEPSEEK_MODEL"), os.getenv("DEEPSEEK_API_KEY")) if mode == "deepseek" else None
    invoke = mcp_invoker(os.getenv("LOGIMATE_MCP_URL", "http://127.0.0.1:8011/mcp"), signing_key) if signing_key else None
    graph = build_workflow(invoke, model) if invoke else None

    def scoped(table, actor):
        return (table.c.tenant_id == actor["tenant_id"], table.c.user_id == actor["user_id"])

    def run_row(run_id, actor):
        with engine.connect() as connection:
            row = connection.execute(select(store.runs).where(
                store.runs.c.run_id == run_id, *scoped(store.runs, actor),
            )).mappings().one_or_none()
        if row is None:
            raise error(404, "not_found", "运行不存在。")
        return row

    def finish(run_id, output=None, failure=None):
        with engine.begin() as connection:
            if output:
                records = {item.evidence_id: item.model_dump() for item in output.get("evidence", [])}
                if records:
                    connection.execute(insert(store.evidence), [
                        {"evidence_id": evidence_id, "run_id": run_id, "payload": evidence}
                        for evidence_id, evidence in records.items()
                    ])
                connection.execute(update(store.runs).where(store.runs.c.run_id == run_id).values(
                    status="completed", result=output["result"],
                    trace=[{**event, "sequence": index} for index, event in enumerate(output["trace"], 1)],
                ))
            else:
                connection.execute(update(store.runs).where(store.runs.c.run_id == run_id).values(
                    status="failed", error=failure,
                ))

    def mark_started(run_id):
        with engine.begin() as connection:
            row = connection.execute(update(store.runs).where(
                store.runs.c.run_id == run_id, store.runs.c.status == "queued",
            ).values(status="running"))
            return row.rowcount == 1

    async def execute(record, actor, rules):
        run_id = record["run_id"]
        try:
            if not await run_in_threadpool(mark_started, run_id):
                return
            output = await graph.ainvoke({"question": record["question"], "trace": [], "rules": rules,
                                          "scope": {**actor, "run_id": run_id,
                                                    "as_of": record["snapshot"]["as_of"],
                                                    "data_version": record["snapshot"]["data_version"]}})
            await run_in_threadpool(finish, run_id, output)
        except Exception as exc:
            log.warning("Run %s failed (%s)", run_id, type(exc).__name__)
            await run_in_threadpool(finish, run_id, failure={
                "code": "run_failed", "message": "排查暂时失败。", "request_id": uuid4().hex,
            })

    @app.post("/api/v1/sessions/{id}/runs", status_code=202, dependencies=[Depends(origin)])
    def create_run(id: Id, body: RunInput, tasks: BackgroundTasks, actor=Depends(identity), key: Key = None):
        if graph is None:
            raise error(503, "mcp_not_configured", "请配置 MCP 服务签名密钥。")
        digest = store.hash_identifier(key) if key else None
        try:
            with engine.begin() as connection:
                owner = connection.scalar(select(store.sessions.c.session_id).where(
                    store.sessions.c.session_id == id, *scoped(store.sessions, actor),
                ))
                if not owner:
                    raise error(404, "not_found", "会话不存在。")
                as_of = connection.scalar(select(store.datasets.c.as_of).where(
                    store.datasets.c.version == settings.data_version,
                ))
                if not as_of:
                    raise error(503, "dataset_not_seeded", "请先加载合成数据。")
                versions, rules = store.active_bundle(connection, actor["tenant_id"])
                record = {"run_id": uuid4().hex, "session_id": id, "tenant_id": actor["tenant_id"],
                          "user_id": actor["user_id"], "question": body.question, "status": "queued",
                          "idempotency_key": digest, "snapshot": {
                              "mode": mode, "data_version": settings.data_version, "as_of": as_of,
                              "versions": versions, "created_at": utc_now(),
                          }}
                connection.execute(insert(store.runs).values(**record))
        except IntegrityError:
            with engine.connect() as connection:
                previous = connection.execute(select(store.runs).where(
                    store.runs.c.session_id == id, store.runs.c.idempotency_key == digest,
                    *scoped(store.runs, actor),
                )).mappings().one_or_none() if digest else None
            if previous is None:
                raise
            if previous["question"] != body.question:
                raise error(409, "idempotency_conflict", "相同幂等键已用于其他问题。") from None
            return {"run_id": previous["run_id"], "status": "queued"}
        except SQLAlchemyError:
            raise error(503, "database_unavailable", "暂时无法提交排查。") from None
        tasks.add_task(execute, record, actor, rules)
        return {"run_id": record["run_id"], "status": "queued"}

    @app.get("/api/v1/runs/{id}")
    def get_run(id: Id, actor=Depends(identity)):
        row = run_row(id, actor)
        return {**row["snapshot"], **{name: row[name] for name in ("run_id", "session_id", "status", "result", "error")}}

    @app.get("/api/v1/runs/{id}/trace")
    def trace(id: Id, actor=Depends(identity)):
        row = run_row(id, actor)
        return {"run_id": id, "events": row["trace"] or []}

    @app.get("/api/v1/evidence/{id}")
    def get_evidence(id: Id, actor=Depends(identity)):
        with engine.connect() as connection:
            result = connection.scalar(select(store.evidence.c.payload).join(
                store.runs, store.evidence.c.run_id == store.runs.c.run_id,
            ).where(store.evidence.c.evidence_id == id, *scoped(store.runs, actor)))
            if result is None and actor["role"] == "reviewer":
                referenced = connection.execute(select(store.feedback.c.payload, store.evidence.c.payload).join(
                    store.evidence, store.evidence.c.run_id == store.feedback.c.run_id,
                ).where(store.evidence.c.evidence_id == id,
                        store.feedback.c.tenant_id == actor["tenant_id"])).all()
                result = next((evidence for feedback, evidence in referenced if id in feedback["evidence_ids"]), None)
        if result is None:
            raise error(404, "not_found", "证据不存在。")
        return result
