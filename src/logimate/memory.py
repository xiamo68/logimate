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
WINDOW_SIZE = 12
WINDOW_TOKEN_BUDGET = 3072
SUMMARY_TOKEN_BUDGET = 1024
MEMORY_FORMAT = 2


def token_upper_bound(payload):
    # Serialized UTF-8 bytes conservatively bound the ordinary tokens of a byte-based tokenizer.
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def compact_history(rows):
    window = deque()
    summary = deque()
    summary_order_ids = set()
    for row in rows:
        window.append(row)
        while len(window) > WINDOW_SIZE or token_upper_bound([item.question for item in window]) > WINDOW_TOKEN_BUDGET:
            previous = window.popleft()
            orders = sorted(set(ORDER_ID.findall(previous.question)))
            if len(summary_order_ids) < 2:
                summary_order_ids = set(sorted(summary_order_ids.union(orders))[:2])
            outcome = previous.result or {}
            summary.append({
                "order_ids": orders[:2],
                "question": previous.question.encode("utf-8")[:160].decode("utf-8", errors="ignore"),
                "result_status": outcome.get("status"),
                "diagnoses": [item["classification"] for item in outcome.get("diagnoses", [])[:2]],
                "evidence_ids": outcome.get("evidence_ids", [])[:3],
            })
            while token_upper_bound(list(summary)) > SUMMARY_TOKEN_BUDGET:
                summary.popleft()
    return {"window": [item.question for item in window], "summary": list(summary),
            "summary_order_ids": sorted(summary_order_ids)}


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
        query = select(store.runs.c.run_id, store.runs.c.question, store.runs.c.result).filter_by(
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
                    if cached.get("format") == MEMORY_FORMAT and cached["head"] == head:
                        return cached
            except (RedisError, ValueError, KeyError, AttributeError):
                log.warning("Session cache unavailable; rebuilding from database")
        snapshot = {"format": MEMORY_FORMAT, "head": head, **compact_history(rows)}
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
        summary = [item for item in history["summary"] if last_order and item["order_ids"] == [last_order]]
        return {"last_order": last_order, "summary": summary, "preferences": self.recall(actor, question)}

    def recall(self, actor, question):
        if not self.vectors:
            return []
        scope = preference_scope(actor)
        with self.engine.connect() as connection:
            indexed = connection.scalar(select(store.preferences.c.preference_id).filter_by(
                **scope, revoked_at=None,
            ).where(store.preferences.c.payload["index_status"].as_string() == "ready").limit(1))
        if not indexed:
            return []
        try:
            ids = self.vectors.search(PREFERENCE_COLLECTION, question, scope, threshold=0.55, limit=5)
        except Exception as exc:
            log.warning("Preference retrieval unavailable (%s)", type(exc).__name__)
            return []
        if not ids:
            return []
        with self.engine.connect() as connection:
            rows = connection.execute(select(store.preferences.c.preference_id, store.preferences.c.payload).filter_by(
                **scope, revoked_at=None,
            ).where(store.preferences.c.preference_id.in_(ids))).all()
        allowed = {row.preference_id: row.payload["text"] for row in rows if row.payload["index_status"] == "ready"}
        return [allowed[item] for item in ids if item in allowed]
