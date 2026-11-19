from collections import deque
import json
import logging
import os

from redis import Redis, RedisError
from sqlalchemy import select, update

from . import store
from .retrieval import PREFERENCE_COLLECTION
from .workflow import ORDER_ID

log = logging.getLogger(__name__)


def preference_scope(actor):
    return {"tenant_id": actor["tenant_id"], "user_id": actor["user_id"]}


def index_preferences(engine, vectors):
    with engine.connect() as connection:
        rows = list(connection.execute(select(store.preferences).filter_by(revoked_at=None)).mappings())
    pending = [row for row in rows if row["payload"]["index_status"] != "ready"]
    vectors.write(PREFERENCE_COLLECTION, [{"id": row["preference_id"], **preference_scope(row),
                                           "text": row["payload"]["text"]} for row in pending])
    with engine.begin() as connection:
        for row in pending:
            connection.execute(update(store.preferences).filter_by(preference_id=row["preference_id"], revoked_at=None)
                               .values(payload={**row["payload"], "index_status": "ready"}))
    return len(pending)


class SessionMemory:
    def __init__(self, engine, redis_url=None, vectors=None, namespace=None):
        self.engine = engine
        self.vectors = vectors
        self.redis = Redis.from_url(redis_url, decode_responses=True, socket_timeout=0.2,
                                    socket_connect_timeout=0.2) if redis_url else None
        self.namespace = namespace or os.getenv("LOGIMATE_CACHE_NAMESPACE", "logimate")

    def close(self):
        if self.redis:
            self.redis.close()

    def history(self, actor, session_id, excluding=None):
        key = f"{self.namespace}:session:{actor['tenant_id']}:{actor['user_id']}:{session_id}"
        query = select(store.runs.c.run_id, store.runs.c.question).filter_by(
            **preference_scope(actor), session_id=session_id, status="completed",
        )
        if excluding is not None:
            query = query.where(store.runs.c.run_id != excluding)
        with self.engine.connect() as connection:
            rows = connection.execute(query.order_by(store.runs.c.snapshot["created_at"].as_string(),
                                                      store.runs.c.run_id)).all()
        head = [len(rows), rows[-1].run_id if rows else None]
        if self.redis:
            try:
                raw = self.redis.get(key)
                if raw:
                    cached = json.loads(raw)
                    if cached["head"] == head:
                        return cached
            except (RedisError, ValueError, KeyError):
                log.warning("Session cache unavailable; rebuilding from database")
        window = deque(maxlen=12)
        summary = []
        for row in rows:
            if len(window) == window.maxlen:
                summary.extend(ORDER_ID.findall(window[0]))
            window.append(row.question[:2000])
        snapshot = {"head": head, "window": list(window), "summary_order_ids": list(dict.fromkeys(summary))[-12:]}
        if self.redis:
            try:
                self.redis.set(key, json.dumps(snapshot, ensure_ascii=False), ex=3600)
            except RedisError:
                log.warning("Session cache write unavailable")
        return snapshot

    def context(self, actor, session_id, question, run_id):
        history = self.history(actor, session_id, excluding=run_id)
        last_order = None
        for previous in reversed(history["window"]):
            ids = set(ORDER_ID.findall(previous))
            if ids:
                if len(ids) == 1:
                    last_order = ids.pop()
                break
        else:
            if len(history["summary_order_ids"]) == 1:
                last_order = history["summary_order_ids"][0]
        return {"last_order": last_order, "preferences": self.recall(actor, question)}

    def recall(self, actor, question):
        if not self.vectors:
            return []
        try:
            ids = self.vectors.search(PREFERENCE_COLLECTION, question, preference_scope(actor), threshold=0.55, limit=5)
        except Exception as exc:
            log.warning("Preference retrieval unavailable (%s)", type(exc).__name__)
            return []
        if not ids:
            return []
        with self.engine.connect() as connection:
            rows = connection.execute(select(store.preferences.c.preference_id, store.preferences.c.payload).filter_by(
                **preference_scope(actor), revoked_at=None,
            ).where(store.preferences.c.preference_id.in_(ids))).all()
        allowed = {row.preference_id: row.payload["text"] for row in rows if row.payload["index_status"] == "ready"}
        return [allowed[item] for item in ids if item in allowed]
