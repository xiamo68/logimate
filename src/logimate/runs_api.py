import logging
import os
from typing import Annotated
from uuid import uuid4
from time import monotonic

import aiosqlite
from fastapi import BackgroundTasks, Depends, Header
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from . import store
from .workflow import DeepSeek, build_workflow, mcp_invoker, resume_deadline, utc_now

log = logging.getLogger(__name__)
Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Key = Annotated[str | None, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


def mount_runs(app, engine, settings, runtime, identity, origin, error):
    mode = os.getenv("LOGIMATE_MODE", "demo")
    if mode not in ("demo", "deepseek"):
        raise ValueError("LOGIMATE_MODE must be demo or deepseek")
    signing_key = os.getenv("LOGIMATE_MCP_SIGNING_KEY")
    limit = os.getenv("LOGIMATE_DEEPSEEK_MAX_REQUESTS")
    model = DeepSeek(os.getenv("DEEPSEEK_MODEL"), os.getenv("DEEPSEEK_API_KEY"),
                     request_limit=int(limit) if limit else None) if mode == "deepseek" else None
    model_id = model.model if model else None
    app.state.model_runtime = {"model_mode": mode, "model_id": model_id}
    invoke = mcp_invoker(os.getenv("LOGIMATE_MCP_URL", "http://127.0.0.1:8011/mcp"), signing_key) if signing_key else None

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
        trace = [{**event, "sequence": index} for index, event in enumerate((output or {}).get("trace", []), 1)]
        with engine.begin() as connection:
            if failure is None:
                records = {item["evidence_id"]: item for item in output.get("evidence", [])}
                if records:
                    connection.execute(insert(store.evidence), [
                        {"evidence_id": evidence_id, "run_id": run_id, "payload": evidence}
                        for evidence_id, evidence in records.items()
                    ])
                connection.execute(update(store.runs).where(store.runs.c.run_id == run_id).values(
                    status="completed", result=output["result"], trace=trace,
                ))
            else:
                connection.execute(update(store.runs).where(store.runs.c.run_id == run_id).values(
                    status="failed", error=failure, trace=trace,
                ))

    def mark_started(run_id):
        with engine.begin() as connection:
            row = connection.execute(update(store.runs).where(
                store.runs.c.run_id == run_id, store.runs.c.status == "queued",
            ).values(status="running"))
            return row.rowcount == 1

    async def execute(record, actor, rules, resumed=False):
        run_id = record["run_id"]
        try:
            if not await run_in_threadpool(mark_started, run_id):
                return
            async def initial():
                memory = await run_in_threadpool(runtime.memory.context, actor, record["session_id"],
                                                 record["question"], run_id)
                return {"question": record["question"], "trace": [], "rules": rules, "memory": memory,
                        "scope": {**actor, "run_id": run_id, "as_of": record["snapshot"]["as_of"],
                                  "data_version": record["snapshot"]["data_version"]}}

            if runtime.checkpoint_path:
                config = {"configurable": {"thread_id": run_id}}
                serde = JsonPlusSerializer(allowed_msgpack_modules=[])
                async with aiosqlite.connect(runtime.checkpoint_path) as connection:
                    saver = AsyncSqliteSaver(connection, serde=serde)
                    graph = build_workflow(invoke, model, checkpointer=saver, retrieve=runtime.retrieve)
                    saved = await graph.aget_state(config) if resumed else None
                    if saved and saved.values:
                        token = resume_deadline.set(monotonic() + 8)
                        try:
                            output = await graph.ainvoke(None, config) if saved.next else saved.values
                        finally:
                            resume_deadline.reset(token)
                    else:
                        output = await graph.ainvoke(await initial(), config)
            else:
                output = await build_workflow(invoke, model, retrieve=runtime.retrieve).ainvoke(await initial())
            failure = None
            if output.get("model_failed"):
                failure = {"code": "model_call_failed", "message": "DeepSeek 意图识别失败，请重新排查。",
                           "request_id": uuid4().hex}
            await run_in_threadpool(finish, run_id, output, failure)
        except Exception as exc:
            log.warning("Run %s failed (%s)", run_id, type(exc).__name__)
            await run_in_threadpool(finish, run_id, failure={
                "code": "run_failed", "message": "排查暂时失败。", "request_id": uuid4().hex,
            })

    @app.post("/api/v1/sessions/{id}/runs", status_code=202, dependencies=[Depends(origin)])
    def create_run(id: Id, body: RunInput, tasks: BackgroundTasks, actor=Depends(identity), key: Key = None):
        if invoke is None:
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
                              "mode": mode, "model_id": model_id, "data_version": settings.data_version, "as_of": as_of,
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

    @app.post("/api/v1/runs/{id}/resume", status_code=202, dependencies=[Depends(origin)])
    def resume(id: Id, tasks: BackgroundTasks, actor=Depends(identity), key: Key = None):
        if invoke is None or not runtime.checkpoint_path:
            raise error(503, "resume_unavailable", "请启用检查点与 MCP 地址。")
        record = run_row(id, actor)
        if record["snapshot"]["mode"] != mode:
            raise error(409, "invalid_state", "当前模型模式与原运行不同。")
        if record["snapshot"].get("model_id", model_id) != model_id:
            raise error(409, "invalid_state", "当前模型与原运行不同。")
        receipt_id = store.hash_identifier(actor["tenant_id"], actor["user_id"], "resume", id, key) if key else None
        accepted = {"run_id": id, "status": "queued"}
        with engine.begin() as connection:
            if receipt_id:
                prior = connection.scalar(select(store.receipts.c.payload).filter_by(receipt_id=receipt_id))
                if prior:
                    return prior
            queued = connection.execute(update(store.runs).where(
                store.runs.c.run_id == id, store.runs.c.status == "interrupted",
            ).values(status="queued"))
            if queued.rowcount != 1:
                if receipt_id:
                    prior = connection.execute(select(store.receipts.c.payload)
                                               .filter_by(receipt_id=receipt_id).with_for_update()).scalar()
                    if prior:
                        return prior
                raise error(409, "invalid_state", "仅中断的运行可以续跑。")
            version_id = record["snapshot"]["versions"]["version_id"]
            bundle = connection.scalar(select(store.versions.c.payload).filter_by(
                tenant_id=actor["tenant_id"], version_id=version_id,
            )) if version_id != "ver_initial" else {"rules": []}
            if not bundle:
                raise error(409, "invalid_state", "原规则版本不可用。")
            if receipt_id:
                connection.execute(insert(store.receipts).values(receipt_id=receipt_id, request_hash=receipt_id,
                                   payload=accepted))
        tasks.add_task(execute, record, actor, bundle["rules"], True)
        return accepted

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
