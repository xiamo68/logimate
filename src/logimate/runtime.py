from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import update

from . import store
from .memory import SessionMemory
from .retrieval import Vectors, policy_retriever

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, engine, data_version):
        self.engine, self.data_version = engine, data_version
        self.checkpoint_path = os.getenv("LOGIMATE_CHECKPOINT_DB")
        self.vectors = None
        self.retrieve = None
        self.memory = SessionMemory(engine, os.getenv("REDIS_URL"))

    def prepare(self):
        if self.checkpoint_path:
            Path(self.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
            # The documented deployment runs one API process per database.
            with self.engine.begin() as connection:
                connection.execute(update(store.runs).where(store.runs.c.status.in_(("queued", "running")))
                                   .values(status="interrupted"))
        if os.getenv("LOGIMATE_MILVUS_URI"):
            try:
                self.vectors = Vectors.from_environment()
            except Exception as exc:
                log.warning("Milvus or encoder unavailable (%s); policy search will use BM25", type(exc).__name__)
        self.memory.vectors = self.vectors
        try:
            self.retrieve = policy_retriever(self.engine, self.vectors, self.data_version)
        except Exception as exc:
            log.warning("Policy corpus unavailable (%s)", type(exc).__name__)

    @asynccontextmanager
    async def lifespan(self, app):
        try:
            await run_in_threadpool(self.prepare)
            yield
        finally:
            self.memory.close()
            if self.vectors is not None:
                self.vectors.close()
