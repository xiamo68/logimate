import asyncio
from collections import Counter
from math import log
import json
import logging
import os
from pathlib import Path
import re

import jieba
from sqlalchemy import select

from . import store

log_service = logging.getLogger(__name__)
POLICY_COLLECTION = "logimate_policies"
PREFERENCE_COLLECTION = "logimate_preferences"
MODEL = "BAAI/bge-small-zh-v1.5"


def policy_chunks(document, data_version, as_of):
    title = document["title"]
    section, buffer = "", []
    chunks = []

    def flush():
        text = "\n".join(buffer).strip()
        if text:
            for offset in range(0, len(text), 420):
                excerpt = text[offset:offset + 420]
                chunks.append({
                    "id": store.hash_identifier(data_version, document["document_id"], section, str(len(chunks))),
                    "document_id": document["document_id"], "version": data_version,
                    "title_path": [title, section] if section else [title], "excerpt": excerpt,
                    "effective_at": document.get("effective_at", as_of),
                })
        buffer.clear()

    for line in document["body"].splitlines():
        if re.match(r"^#{1,6}\s+", line):
            flush()
            section = line.lstrip("# ").strip()
        else:
            buffer.append(line)
    flush()
    return chunks


def text_with_title(chunk):
    return " / ".join(chunk["title_path"]) + "\n" + chunk["excerpt"]


def terms(text):
    stop = {"的", "了", "吗", "我", "你", "如何", "是否", "可以", "能", "有", "为", "是"}
    return [word for word in jieba.lcut(text.lower()) if any(char.isalnum() for char in word) and word not in stop]


def keyword_search(question, chunks, limit=20):
    tokens = set(terms(question))
    corpus = [Counter(terms(text_with_title(chunk))) for chunk in chunks]
    if not corpus or not tokens:
        return []
    mean = sum(sum(doc.values()) for doc in corpus) / len(corpus) or 1
    idf = {token: log(1 + (len(corpus) - sum(token in doc for doc in corpus) + 0.5)
                     / (sum(token in doc for doc in corpus) + 0.5)) for token in tokens}
    scores = []
    for chunk, doc in zip(chunks, corpus):
        normalizer = 1.2 * (0.25 + 0.75 * sum(doc.values()) / mean)
        score = sum(idf[token] * doc[token] * 2.2 / (doc[token] + normalizer) for token in tokens if token in doc)
        if score > 0:
            scores.append((score, chunk["id"]))
    return [key for _, key in sorted(scores, key=lambda item: (-item[0], item[1]))[:limit]]


def rrf(*rankings, limit=5):
    scores = Counter()
    for rank in rankings:
        for position, key in enumerate(dict.fromkeys(rank), 1):
            scores[key] += 1 / (60 + position)
    return sorted(scores, key=lambda key: (-scores[key], key))[:limit]


class Vectors:
    def __init__(self, uri, model=MODEL, cache_dir=".logimate/models"):
        from fastembed import TextEmbedding
        from pymilvus import MilvusClient

        if "://" not in uri:
            Path(uri).parent.mkdir(parents=True, exist_ok=True)
        self.encoder = TextEmbedding(model, cache_dir=cache_dir, threads=2)
        self.dim = len(next(self.encoder.embed(["物流"])))
        self.client = MilvusClient(uri)

    @classmethod
    def from_environment(cls):
        return cls(os.getenv("LOGIMATE_MILVUS_URI", ".logimate/milvus.db"),
                   os.getenv("LOGIMATE_EMBEDDING_MODEL", MODEL), os.getenv("LOGIMATE_MODEL_CACHE", ".logimate/models"))

    def close(self):
        self.client.close()

    def write(self, collection, rows):
        if not rows:
            return
        if not self.client.has_collection(collection, timeout=2):
            self.client.create_collection(collection, dimension=self.dim, id_type="string", max_length=64,
                                          metric_type="COSINE", consistency_level="Strong", timeout=5)
        vectors = self.encoder.passage_embed(row["text"] for row in rows)
        self.client.upsert(collection, [{**row, "vector": vector.tolist()} for row, vector in zip(rows, vectors)],
                           timeout=5)

    def search(self, collection, question, filters, threshold=0.45, limit=20):
        vector = next(self.encoder.query_embed(question))
        condition = " and ".join(f"{key} == {json.dumps(value)}" for key, value in filters.items())
        hits = self.client.search(collection, [vector.tolist()], filter=condition, limit=limit,
                                  search_params={"metric_type": "COSINE"}, timeout=0.7)[0]
        return [str(hit["id"]) for hit in hits if hit["distance"] >= threshold]


def load_chunks(engine, data_version, as_of):
    with engine.connect() as connection:
        documents = connection.scalars(select(store.policy_documents.c.payload).filter_by(data_version=data_version))
        return [chunk for doc in documents for chunk in policy_chunks(doc, data_version, as_of)
                if chunk["effective_at"] <= as_of]


def index_policies(engine, vectors, data_version):
    with engine.connect() as connection:
        as_of = connection.scalar(select(store.datasets.c.as_of).filter_by(version=data_version))
    if not as_of:
        raise ValueError("Seed the selected data version before indexing")
    chunks = load_chunks(engine, data_version, as_of)
    vectors.write(POLICY_COLLECTION, [{"id": chunk["id"], "version": data_version, "text": text_with_title(chunk)}
                                      for chunk in chunks])
    terms("物流政策")
    return len(chunks)


async def hybrid_retrieve(question, chunks, semantic=None, budget=0.8):
    if not chunks:
        return "unavailable", []

    async def retrieve(operation):
        try:
            return await asyncio.to_thread(operation)
        except Exception as exc:
            log_service.warning("Retrieval branch unavailable (%s)", type(exc).__name__)
            return None

    keyword = asyncio.create_task(retrieve(lambda: keyword_search(question, chunks)))
    vector = asyncio.create_task(retrieve(semantic)) if semantic else None
    tasks = [keyword, vector] if vector else [keyword]
    _, pending = await asyncio.wait(tasks, timeout=budget)
    for task in pending:
        task.cancel()
    bm = keyword.result() if keyword.done() and not keyword.cancelled() else None
    dense = vector.result() if vector and vector.done() and not vector.cancelled() else None
    mode = "hybrid" if bm is not None and dense is not None else "keyword_only" if bm is not None else (
        "semantic_only" if dense is not None else "unavailable")
    by_id = {item["id"]: item for item in chunks}
    return mode, [by_id[key] for key in rrf(bm or [], dense or []) if key in by_id]


def policy_retriever(engine, vectors, data_version):
    with engine.connect() as connection:
        as_of = connection.scalar(select(store.datasets.c.as_of).filter_by(version=data_version))
    chunks = load_chunks(engine, data_version, as_of) if as_of else []
    terms("物流政策")

    async def retrieve(question, scope):
        if scope["data_version"] != data_version:
            return "unavailable", []
        allowed = [chunk for chunk in chunks if chunk["effective_at"] <= scope["as_of"]]
        return await hybrid_retrieve(question, allowed, (lambda: vectors.search(
            POLICY_COLLECTION, question, {"version": data_version},
        )) if vectors else None)

    return retrieve
