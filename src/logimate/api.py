from dataclasses import dataclass
import os
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from . import store
from .evolution_api import mount_evolution
from .runs_api import mount_runs


@dataclass(frozen=True)
class Settings:
    dsn: str
    secret: str
    origin: str = "http://127.0.0.1:8010"
    demo_sign_in: bool = False
    data_version: str = "synthetic-v1"

    @classmethod
    def from_environment(cls):
        return cls(os.environ["MYSQL_DSN"], os.environ["LOGIMATE_SESSION_SECRET"],
                   os.getenv("LOGIMATE_PUBLIC_ORIGIN", cls.origin),
                   os.getenv("LOGIMATE_DEMO_SIGN_IN") == "1",
                   os.getenv("LOGIMATE_DATA_VERSION", cls.data_version))


Profile = Literal["alpha_operator", "alpha_reviewer", "beta_operator", "beta_reviewer"]
PROFILES = {f"{tenant}_{role}": {"profile": f"{tenant}_{role}", "tenant_id": tenant,
            "user_id": f"{tenant}-{role}", "role": role}
            for tenant in ("alpha", "beta") for role in ("operator", "reviewer")}


class SignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Profile


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] = "排查会话"


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def create_app(settings=None, engine=None):
    settings = settings or Settings.from_environment()
    origin = urlsplit(settings.origin)
    if settings.demo_sign_in and (origin.scheme != "http" or origin.hostname not in ("127.0.0.1", "localhost")
                                  or origin.path or origin.query or origin.fragment or origin.username):
        raise ValueError("Demo sign-in requires a loopback HTTP origin")
    if len(settings.secret) < 32:
        raise ValueError("LOGIMATE_SESSION_SECRET must contain at least 32 characters")
    engine = engine if engine is not None else store.database(settings.dsn)
    tokens = URLSafeTimedSerializer(settings.secret, salt="logimate-demo-session")
    cursors = URLSafeTimedSerializer(settings.secret, salt="logimate-session-cursor")
    app = FastAPI(title="LogiMate", version="0.1.0")

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return JSONResponse(status_code=exc.status, content={
            "code": exc.code, "message": exc.message, "request_id": uuid4().hex,
        })

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        invalid_json = any(error["type"] == "json_invalid" for error in exc.errors())
        return await api_error(request, APIError(
            400 if invalid_json else 422, "invalid_json" if invalid_json else "invalid_input", "请求字段有误。",
        ))

    def same_origin(request: Request):
        if request.headers.get("origin") != settings.origin:
            raise APIError(403, "forbidden", "请求来源不匹配。")

    def identity(request: Request):
        try:
            profile = tokens.loads(request.cookies.get("lm_session", ""), max_age=8 * 3600)
        except BadSignature:
            profile = None
        if profile not in PROFILES:
            raise APIError(401, "unauthenticated", "请先登录演示身份。")
        return PROFILES[profile]

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(response: Response):
        try:
            with engine.connect() as connection:
                seeded = connection.scalar(select(store.datasets.c.version).where(
                    store.datasets.c.version == settings.data_version,
                ))
        except SQLAlchemyError:
            response.status_code = 503
            return {"status": "unavailable", "reason": "database_or_schema_unavailable"}
        if not seeded:
            response.status_code = 503
        return {"status": "ok" if seeded else "unavailable",
                "reason": None if seeded else "dataset_not_seeded"}

    @app.post("/api/v1/demo/sign-in")
    def sign_in(body: SignIn, response: Response, request: Request):
        if not settings.demo_sign_in:
            raise APIError(404, "not_found", "演示登录未启用。")
        same_origin(request)
        response.set_cookie("lm_session", tokens.dumps(body.profile), httponly=True, samesite="strict",
                            max_age=8 * 3600)
        return PROFILES[body.profile]

    @app.post("/api/v1/sessions", status_code=201, dependencies=[Depends(same_origin)])
    def create(body: SessionInput = SessionInput(), actor=Depends(identity),
               key: Annotated[str | None, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")] = None):
        try:
            result = store.create_session(engine, actor["tenant_id"], actor["user_id"], body.title, key)
        except SQLAlchemyError:
            raise APIError(503, "database_unavailable", "暂时无法保存会话。") from None
        if result is None:
            raise APIError(409, "idempotency_conflict", "相同幂等键已用于其他请求。")
        return result

    @app.get("/api/v1/sessions/{id}")
    def get_session(id: str, actor=Depends(identity), cursor: str | None = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 20):
        scope = [actor["tenant_id"], actor["user_id"], id]
        offset = 0
        if cursor:
            try:
                decoded = cursors.loads(cursor, max_age=8 * 3600)
                if decoded["scope"] != scope:
                    raise ValueError("cursor scope mismatch")
                offset = decoded["offset"]
            except (BadSignature, KeyError, ValueError):
                raise APIError(422, "invalid_input", "会话游标不可用。") from None
        try:
            with engine.connect() as connection:
                row = connection.execute(select(store.sessions).where(
                    store.sessions.c.session_id == id, store.sessions.c.tenant_id == actor["tenant_id"],
                    store.sessions.c.user_id == actor["user_id"],
                )).mappings().one_or_none()
                history = connection.execute(select(store.runs).where(
                    store.runs.c.session_id == id, store.runs.c.tenant_id == actor["tenant_id"],
                    store.runs.c.user_id == actor["user_id"],
                ).order_by(store.runs.c.snapshot["created_at"].as_string(), store.runs.c.run_id)).mappings().all() if row else []
        except SQLAlchemyError:
            raise APIError(503, "database_unavailable", "暂时无法读取会话。") from None
        if row is None:
            raise APIError(404, "not_found", "会话不存在。")
        messages = []
        summaries = {}
        for run in history:
            run_id = run["run_id"]
            created = run["snapshot"]["created_at"]
            summaries[run_id] = {"run_id": run_id, "status": run["status"], "created_at": created}
            messages.append({"message_id": run_id + "-user", "run_id": run_id, "role": "user",
                             "content": run["question"], "created_at": created})
            if run["result"]:
                messages.append({"message_id": run_id + "-assistant", "run_id": run_id, "role": "assistant",
                                 "content": run["result"]["summary"], "created_at": created})
        page = messages[offset:offset + limit]
        next_cursor = cursors.dumps({"scope": scope, "offset": offset + limit}) if offset + limit < len(messages) else None
        return {**store.public_session(row), "messages": page, "next_cursor": next_cursor,
                "runs": [summaries[run_id] for run_id in dict.fromkeys(item["run_id"] for item in page)]}

    mount_runs(app, engine, settings, identity, same_origin, APIError)
    mount_evolution(app, engine, settings, identity, same_origin, APIError)
    return app
