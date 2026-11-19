from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Query, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from . import store
from .memory import preference_scope
from .retrieval import PREFERENCE_COLLECTION
from .runs_api import Id, Key
from .workflow import utc_now


class PreferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


def mount_preferences(app, engine, settings, runtime, identity, origin, error):
    cursors = URLSafeTimedSerializer(settings.secret, salt="logimate-preference-cursor")

    @app.get("/api/v1/preferences")
    def list_preferences(actor=Depends(identity), limit: Annotated[int, Query(ge=1, le=100)] = 20,
                         cursor: str | None = None):
        scope = preference_scope(actor)
        before = None
        if cursor:
            try:
                state = cursors.loads(cursor, max_age=8 * 3600)
                if state["scope"] != scope:
                    raise ValueError("Invalid cursor")
                before = state["before"]
            except (BadSignature, ValueError, KeyError):
                raise error(422, "invalid_input", "偏好游标不可用。") from None
        query = select(store.preferences.c.payload).filter_by(**scope, revoked_at=None)
        created_at = store.preferences.c.payload["created_at"].as_string()
        if before:
            query = query.where((created_at < before[0]) | (
                (created_at == before[0]) & (store.preferences.c.preference_id < before[1])))
        with engine.connect() as connection:
            items = list(connection.scalars(query.order_by(created_at.desc(), store.preferences.c.preference_id.desc())
                                            .limit(limit + 1)))
        next_cursor = None
        if len(items) > limit:
            tail = items[limit - 1]
            next_cursor = cursors.dumps({"scope": scope, "before": [tail["created_at"], tail["preference_id"]]})
        return {"items": items[:limit], "next_cursor": next_cursor}

    @app.post("/api/v1/preferences", status_code=201, dependencies=[Depends(origin)])
    def create_preference(body: PreferenceInput, actor=Depends(identity), key: Key = None):
        scope = preference_scope(actor)
        preference_id = store.hash_identifier(*scope.values(), "preferences", key) if key else uuid4().hex
        payload = {"preference_id": preference_id, "text": body.text, "index_status": "pending", "created_at": utc_now()}
        try:
            with engine.begin() as connection:
                connection.execute(insert(store.preferences).values(preference_id=preference_id, **scope, payload=payload))
        except IntegrityError:
            with engine.connect() as connection:
                prior = connection.scalar(select(store.preferences.c.payload).filter_by(
                    preference_id=preference_id, **scope,
                ))
            if not prior or prior["text"] != body.text:
                raise error(409, "idempotency_conflict", "相同幂等键已用于其他请求。") from None
            return prior
        if runtime.vectors:
            try:
                runtime.vectors.write(PREFERENCE_COLLECTION, [{"id": preference_id, **scope, "text": body.text}])
                payload["index_status"] = "ready"
            except Exception:
                payload["index_status"] = "failed"
            with engine.begin() as connection:
                connection.execute(update(store.preferences).where(store.preferences.c.preference_id == preference_id)
                                   .values(payload=payload))
        return payload

    @app.delete("/api/v1/preferences/{id}", status_code=204, dependencies=[Depends(origin)])
    def delete_preference(id: Id, actor=Depends(identity), key: Key = None):
        scope = preference_scope(actor)
        with engine.begin() as connection:
            row = connection.execute(select(store.preferences.c.revoked_at).filter_by(
                preference_id=id, **scope,
            )).one_or_none()
            if row is None:
                raise error(404, "not_found", "偏好不存在。")
            if row.revoked_at is None:
                connection.execute(update(store.preferences).where(store.preferences.c.preference_id == id)
                                   .values(revoked_at=utc_now()))
        # The authoritative revocation remains effective even while the vector store is down.
        if runtime.vectors:
            try:
                runtime.vectors.client.delete(PREFERENCE_COLLECTION, ids=[id], timeout=1)
            except Exception:
                pass
        return Response(status_code=204)
