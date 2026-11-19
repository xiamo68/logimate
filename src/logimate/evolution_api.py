from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, Query, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from . import store
from .evolution import DETAIL_FIELDS, get_verified, query_payload, version_payload
from .rules import Classification, matchable_features
from .runs_api import Id, Key
from .seed import fingerprint
from .workflow import utc_now

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Decision = Literal["approve", "reject"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeedbackInput(Input):
    corrected_classification: Classification
    reason: Reason
    evidence_ids: list[Id] = Field(min_length=1, max_length=20)


class VerifyInput(Input):
    decision: Decision
    reason: Reason


class BaseJob(Input):
    kind: Literal["base"]
    feedback_ids: list[Id] = Field(min_length=1, max_length=100)


class MetaJob(Input):
    kind: Literal["meta"]


class ReplayJob(Input):
    kind: Literal["replay"]
    version_id: Id


class ReviewInput(VerifyInput):
    report_id: Id


class ReleaseInput(Input):
    expected_release_id: Id


class RollbackInput(ReleaseInput):
    reason: Reason


def mount_evolution(app, engine, settings, identity, origin, error):
    cursor_signer = URLSafeTimedSerializer(settings.secret, salt="logimate-evolution-cursor")

    def require(actor, role):
        if actor["role"] != role:
            raise error(403, "forbidden", "当前身份不能执行该操作。")

    def conflict(message, code="invalid_state"):
        raise error(409, code, message)

    def existing(payload):
        if payload is None:
            raise error(404, "not_found", "资源不存在。")
        return payload

    def page(rows, cursor, limit, actor, scope):
        binding = [actor["tenant_id"], actor["user_id"], *scope]
        offset = 0
        if cursor:
            try:
                value = cursor_signer.loads(cursor, max_age=8 * 3600)
                if value["binding"] != binding:
                    raise ValueError("cursor scope")
                offset = value["offset"]
            except (BadSignature, KeyError, ValueError):
                raise error(422, "invalid_input", "游标不可用。") from None
        after = offset + limit
        return {"items": rows[offset:after], "next_cursor": cursor_signer.dumps({
            "binding": binding, "offset": after,
        }) if after < len(rows) else None}

    def mutate(request, actor, key, body, action):
        tenant = actor["tenant_id"]
        receipt_id = store.hash_identifier(tenant, actor["user_id"], request.method, request.url.path, key) if key else None
        digest = fingerprint(body.model_dump())

        def read_receipt(connection):
            if not receipt_id:
                return None
            row = connection.execute(select(store.receipts).filter_by(receipt_id=receipt_id)).mappings().one_or_none()
            if row and row["request_hash"] != digest:
                conflict("相同幂等键已用于其他请求。", "idempotency_conflict")
            return row["payload"] if row else None

        try:
            with engine.begin() as connection:
                connection.execute(select(store.active_versions).filter_by(tenant_id=tenant).with_for_update()).one()
                prior = read_receipt(connection)
                if prior is not None:
                    return prior
                result = action(connection, tenant)
                if receipt_id:
                    connection.execute(insert(store.receipts).values(
                        receipt_id=receipt_id, request_hash=digest, payload=result,
                    ))
                return result
        except IntegrityError:
            with engine.connect() as connection:
                prior = read_receipt(connection)
                if prior is not None:
                    return prior
            raise

    def feedback_public(payload):
        return {name: value for name, value in payload.items() if name not in ("features", "data_version")}

    def evidence_features(evidence):
        fields = {item["source"]: {field["name"]: field["value"] for field in item["fields"]} for item in evidence}
        return matchable_features(fields.get("warehouse", {}).get("handoff_recorded"),
                                  fields.get("logistics", {}).get("status"),
                                  fields.get("aftersales", {}).get("case_count"))

    @app.post("/api/v1/runs/{id}/feedback", status_code=201, dependencies=[Depends(origin)])
    def submit_feedback(id: Id, body: FeedbackInput, request: Request, actor=Depends(identity), key: Key = None):
        require(actor, "operator")

        def action(connection, tenant):
            run = existing(connection.execute(select(store.runs).filter_by(
                tenant_id=tenant, user_id=actor["user_id"], run_id=id,
            )).mappings().one_or_none())
            if run["status"] != "completed" or not run["result"]:
                conflict("需先完成排查。")
            rows = list(connection.scalars(select(store.evidence.c.payload).filter_by(run_id=id)))
            evidence_by_id = {item["evidence_id"]: item for item in rows}
            chosen = set(body.evidence_ids)
            if len(chosen) != len(body.evidence_ids) or not chosen.issubset(evidence_by_id):
                raise error(422, "invalid_input", "纠正必须引用本次运行的有效证据。")
            if not {"warehouse", "logistics"}.issubset({evidence_by_id[item]["source"] for item in chosen}):
                raise error(422, "invalid_input", "物流纠正需同时引用仓储与物流证据。")
            packages = {item["resource_id"] for item in rows if item["source"] == "logistics"}
            order_id = next((item["resource_id"] for item in rows if item["source"] == "order"), None)
            package_count = connection.scalar(select(func.count()).select_from(store.packages).where(
                store.packages.c.tenant_id == tenant, store.packages.c.data_version == run["snapshot"]["data_version"],
                store.packages.c.payload["order_id"].as_string() == order_id,
            )) if order_id else 0
            if len(packages) != 1 or package_count != 1:
                raise error(422, "invalid_input", "当前纠正入口需要单包裹物流证据。")
            payload = {**body.model_dump(), "feedback_id": uuid4().hex, "run_id": id, "status": "pending",
                       "submitted_by": actor["user_id"], "reviewed_by": None, "review_reason": None,
                       "created_at": utc_now(), "features": evidence_features(rows),
                       "data_version": run["snapshot"]["data_version"]}
            connection.execute(insert(store.feedback).values(feedback_id=payload["feedback_id"], tenant_id=tenant,
                               session_id=run["session_id"], run_id=id, status="pending", payload=payload))
            return feedback_public(payload)

        return mutate(request, actor, key, body, action)

    @app.get("/api/v1/feedback")
    def list_feedback(actor=Depends(identity), cursor: str | None = None,
                      limit: Annotated[int, Query(ge=1, le=100)] = 20, session_id: Id | None = None,
                      status: Literal["pending", "verified", "rejected"] | None = None):
        tenant = actor["tenant_id"]
        query = select(store.feedback.c.payload).filter_by(tenant_id=tenant)
        if actor["role"] == "operator":
            query = query.where(store.feedback.c.payload["submitted_by"].as_string() == actor["user_id"])
        if session_id:
            query = query.filter_by(session_id=session_id)
        if status:
            query = query.filter_by(status=status)
        with engine.connect() as connection:
            rows = [feedback_public(item) for item in connection.scalars(
                query.order_by(store.feedback.c.payload["created_at"].as_string().desc(), store.feedback.c.feedback_id),
            )]
        return page(rows, cursor, limit, actor, ["feedback", session_id, status])

    @app.post("/api/v1/feedback/{id}/verify", dependencies=[Depends(origin)])
    def verify_feedback(id: Id, body: VerifyInput, request: Request, actor=Depends(identity), key: Key = None):
        require(actor, "reviewer")

        def action(connection, tenant):
            payload = existing(query_payload(connection, store.feedback, tenant, feedback_id=id))
            if payload["status"] != "pending" or payload["submitted_by"] == actor["user_id"]:
                conflict("纠正不能重复复核或自行复核。")
            payload = {**payload, "status": "verified" if body.decision == "approve" else "rejected",
                       "reviewed_by": actor["user_id"], "review_reason": body.reason}
            connection.execute(update(store.feedback).where(store.feedback.c.feedback_id == id).values(
                status=payload["status"], payload=payload,
            ))
            return feedback_public(payload)

        return mutate(request, actor, key, body, action)

    @app.post("/api/v1/evolution/jobs", status_code=202, dependencies=[Depends(origin)])
    def submit_job(body: BaseJob | MetaJob | ReplayJob, request: Request, actor=Depends(identity), key: Key = None):
        require(actor, "operator")

        def action(connection, tenant):
            if isinstance(body, BaseJob):
                try:
                    if len(set(body.feedback_ids)) != len(body.feedback_ids):
                        raise ValueError
                    get_verified(connection, tenant, body.feedback_ids, settings.data_version)
                except ValueError:
                    raise error(422, "invalid_input", "只允许同租户、同数据版本且已复核的纠正。") from None
            if isinstance(body, ReplayJob):
                existing(query_payload(connection, store.versions, tenant, version_id=body.version_id))
            job_id = uuid4().hex
            payload = {"job_id": job_id, "kind": body.kind, "request": body.model_dump(),
                       "submitted_by": actor["user_id"], "data_version": settings.data_version,
                       "processed_cases": 0, "total_cases": 0, "version_id": None, "report_ids": [],
                       "created_at": utc_now(), "error": None}
            connection.execute(insert(store.jobs).values(job_id=job_id, tenant_id=tenant, status="queued", payload=payload))
            return {"job_id": job_id, "status": "queued"}

        return mutate(request, actor, key, body, action)

    @app.get("/api/v1/evolution/jobs/{id}")
    def get_job(id: Id, actor=Depends(identity)):
        with engine.connect() as connection:
            row = existing(connection.execute(select(store.jobs).filter_by(
                job_id=id, tenant_id=actor["tenant_id"],
            )).mappings().one_or_none())
        fields = ("job_id", "kind", "processed_cases", "total_cases", "version_id", "report_ids", "error", "created_at")
        return {**{name: row["payload"][name] for name in fields}, "status": row["status"]}

    def detail(connection, tenant, payload):
        version_id = payload["version"]["version_id"]
        return {**{name: payload[name] for name in DETAIL_FIELDS}, "reports": list(connection.scalars(
            select(store.reports.c.payload).filter_by(tenant_id=tenant, version_id=version_id).order_by(store.reports.c.report_id),
        )), "activation_events": [
            event["public"] for event in connection.scalars(select(store.releases.c.payload).filter_by(
                tenant_id=tenant, version_id=version_id,
            ).order_by(store.releases.c.payload["created_at"].as_string().desc()))
        ]}

    @app.get("/api/v1/versions")
    def versions(actor=Depends(identity), limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None):
        tenant = actor["tenant_id"]
        with engine.connect() as connection:
            rows = [item["version"] for item in connection.scalars(select(store.versions.c.payload).filter_by(
                tenant_id=tenant,
            ).order_by(store.versions.c.payload["created_at"].as_string().desc(), store.versions.c.version_id))]
            rows.append(version_payload(connection, tenant, "ver_initial")["version"])
            active, _ = store.active_bundle(connection, tenant)
        return {"active": active, **page(rows, cursor, limit, actor, ["versions"])}

    @app.get("/api/v1/versions/{id}")
    def get_version(id: Id, actor=Depends(identity)):
        tenant = actor["tenant_id"]
        with engine.connect() as connection:
            return detail(connection, tenant, existing(version_payload(connection, tenant, id)))

    def valid_report(connection, tenant, payload, report_id, expected_release_id):
        summary = payload["version"]
        report = query_payload(connection, store.reports, tenant, report_id=report_id, version_id=summary["version_id"])
        if (not report or not report["passed"] or report["comparison"] != "active_baseline"
                or report["candidate_content_hash"] != summary["content_hash"]
                or report["baseline_release_id"] != expected_release_id):
            conflict("报告未通过、与候选不匹配或基线已过期。", "stale_baseline")
        return report

    @app.post("/api/v1/versions/{id}/review", dependencies=[Depends(origin)])
    def review(id: Id, body: ReviewInput, request: Request, actor=Depends(identity), key: Key = None):
        require(actor, "reviewer")

        def action(connection, tenant):
            payload = existing(version_payload(connection, tenant, id))
            active, _ = store.active_bundle(connection, tenant)
            if payload["version"]["state"] != "passed" or payload["submitted_by"] == actor["user_id"]:
                conflict("候选尚未通过回放、已审核或属于自审。")
            valid_report(connection, tenant, payload, body.report_id, active["release_id"])
            state = "approved" if body.decision == "approve" else "rejected"
            payload["version"]["state"] = state
            payload.update(reviewed_by=actor["user_id"], reviewed_report_id=body.report_id, review_reason=body.reason)
            connection.execute(update(store.versions).where(store.versions.c.version_id == id).values(
                state=state, payload=payload,
            ))
            connection.execute(insert(store.reviews).values(review_id=uuid4().hex, tenant_id=tenant, version_id=id,
                               payload={**body.model_dump(), "actor_id": actor["user_id"], "created_at": utc_now()}))
            return detail(connection, tenant, payload)

        return mutate(request, actor, key, body, action)

    def activate(connection, tenant, id, expected, actor, operation, reason=None):
        payload = existing(version_payload(connection, tenant, id))
        active, _ = store.active_bundle(connection, tenant)
        summary = payload["version"]
        if active["release_id"] != expected:
            conflict("活动版本已变更。", "stale_baseline")
        if active["version_id"] == id:
            conflict("此版本已经启用。")
        if operation == "release":
            if summary["state"] != "approved":
                conflict("候选尚未批准。")
            valid_report(connection, tenant, payload, payload["reviewed_report_id"], expected)
            summary["state"] = "released"
            connection.execute(update(store.versions).where(store.versions.c.version_id == id).values(
                state="released", payload=payload,
            ))
        elif summary["state"] != "released":
            conflict("只能回滚至此前已发布的版本。")
        release_id = "rel_" + uuid4().hex
        changed = connection.execute(update(store.active_versions).where(
            store.active_versions.c.tenant_id == tenant, store.active_versions.c.release_id == expected,
        ).values(release_id=release_id, version_id=id))
        if changed.rowcount != 1:
            conflict("活动版本已变更。", "stale_baseline")
        event = {"operation": operation, "previous_release_id": expected, "actor_id": actor["user_id"],
                 "active": {"release_id": release_id, **{name: summary[name] for name in
                            ("version_id", "rule_version_id", "skill_version_id")}}, "created_at": utc_now()}
        connection.execute(insert(store.releases).values(release_id=release_id, tenant_id=tenant, version_id=id,
                           payload={"public": event, "reason": reason, "created_at": event["created_at"]}))
        return event

    @app.post("/api/v1/versions/{id}/release", dependencies=[Depends(origin)])
    def release(id: Id, body: ReleaseInput, request: Request, actor=Depends(identity), key: Key = None):
        require(actor, "reviewer")
        return mutate(request, actor, key, body, lambda connection, tenant: activate(
            connection, tenant, id, body.expected_release_id, actor, "release",
        ))

    @app.post("/api/v1/versions/{id}/rollback", dependencies=[Depends(origin)])
    def rollback(id: Id, body: RollbackInput, request: Request, actor=Depends(identity), key: Key = None):
        require(actor, "reviewer")
        return mutate(request, actor, key, body, lambda connection, tenant: activate(
            connection, tenant, id, body.expected_release_id, actor, "rollback", body.reason,
        ))
