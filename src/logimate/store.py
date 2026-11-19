from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import JSON, Column, MetaData, String, Table, Text, UniqueConstraint, create_engine, insert, select
from sqlalchemy.exc import IntegrityError

metadata = MetaData()


def hash_identifier(*parts):
    return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


schema_versions = Table("schema_versions", metadata, Column("version", String(32), primary_key=True))
datasets = Table("datasets", metadata, Column("version", String(32), primary_key=True),
                 Column("sha256", String(64), nullable=False), Column("as_of", String(32), nullable=False))
policy_documents = Table("policy_documents", metadata, Column("data_version", String(32), primary_key=True),
                         Column("document_id", String(64), primary_key=True), Column("payload", JSON, nullable=False))


def domain_table(name, key):
    return Table(name, metadata, Column("data_version", String(32), primary_key=True),
                 Column("tenant_id", String(32), primary_key=True),
                 Column(key, String(64), primary_key=True), Column("payload", JSON, nullable=False))


orders = domain_table("orders", "order_id")
packages = domain_table("packages", "package_id")
warehouse_events = domain_table("warehouse_events", "event_id")
tracking_events = domain_table("tracking_events", "event_id")
aftersales_cases = domain_table("aftersales_cases", "case_id")
sessions = Table(
    "sessions", metadata, Column("session_id", String(64), primary_key=True),
    Column("tenant_id", String(32), nullable=False), Column("user_id", String(64), nullable=False),
    Column("title", String(120), nullable=False), Column("created_at", String(32), nullable=False),
    Column("idempotency_key", String(128)),
    UniqueConstraint("tenant_id", "user_id", "idempotency_key", name="uq_session_request"),
)
runs = Table(
    "runs", metadata, Column("run_id", String(64), primary_key=True),
    Column("session_id", String(64), nullable=False, index=True),
    Column("tenant_id", String(32), nullable=False), Column("user_id", String(64), nullable=False),
    Column("question", Text, nullable=False), Column("status", String(32), nullable=False),
    Column("idempotency_key", String(64)), Column("snapshot", JSON, nullable=False),
    Column("result", JSON), Column("trace", JSON), Column("error", JSON),
    UniqueConstraint("session_id", "idempotency_key", name="uq_run_request"),
)
evidence = Table(
    "evidence", metadata, Column("evidence_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False, index=True), Column("payload", JSON, nullable=False),
)


def database(dsn):
    return create_engine(dsn, pool_pre_ping=True)


def migrate(engine):
    metadata.create_all(engine)
    with engine.begin() as connection:
        installed = set(connection.scalars(select(schema_versions.c.version)))
        for version in ("001", "002"):
            if version not in installed:
                connection.execute(insert(schema_versions).values(version=version))


def seed_database(engine, data, digest):
    version = data["data_version"]
    with engine.begin() as connection:
        previous = connection.scalar(select(datasets.c.sha256).where(datasets.c.version == version))
        if previous == digest:
            return False
        if previous is not None:
            raise ValueError("The data version already exists with a different fingerprint")
        connection.execute(insert(datasets).values(version=version, sha256=digest, as_of=data["as_of"]))
        for row in data["business"]:
            scope = {"data_version": version, "tenant_id": row["tenant_id"]}
            order_id, package_id = row["order_id"], row["package_id"]
            connection.execute(insert(orders).values(**scope, order_id=order_id, payload=row["order"]))
            connection.execute(insert(packages).values(**scope, package_id=package_id,
                               payload={"order_id": order_id, "tracking_available": row["tracking_available"]}))
            for table, key, records in (
                (warehouse_events, "event_id", row["warehouse_events"]),
                (tracking_events, "event_id", row["tracking_events"]),
                (aftersales_cases, "case_id", row["aftersales_cases"]),
            ):
                if records:
                    connection.execute(insert(table), [
                        {**scope, key: item[key], "payload": {**item, "order_id": order_id}}
                        for item in records
                    ])
        connection.execute(insert(policy_documents), [
            {"data_version": version, "document_id": item["document_id"], "payload": item}
            for item in data["policies"]
        ])
    return True


def public_session(row):
    return {key: row[key] for key in ("session_id", "title", "created_at")}


def create_session(engine, tenant_id, user_id, title, key):
    key = sha256(key.encode("ascii")).hexdigest() if key is not None else None
    record = {"session_id": uuid4().hex, "tenant_id": tenant_id, "user_id": user_id, "title": title,
              "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
              "idempotency_key": key}
    try:
        with engine.begin() as connection:
            connection.execute(insert(sessions).values(**record))
    except IntegrityError:
        with engine.connect() as connection:
            existing = connection.execute(select(sessions).where(
                sessions.c.tenant_id == tenant_id, sessions.c.user_id == user_id,
                sessions.c.idempotency_key == key,
            )).mappings().one_or_none() if key else None
        if existing is None:
            raise
        if existing["title"] != title:
            return None
        return public_session(existing)
    return public_session(record)
