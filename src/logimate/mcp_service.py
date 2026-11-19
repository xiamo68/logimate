import os
from typing import Annotated, Literal

from itsdangerous import BadSignature, URLSafeTimedSerializer
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from . import store

Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
TOOL_NAMES = {"order.get", "warehouse.get_events", "logistics.get_tracking", "aftersales.get_cases"}


class Evidence(BaseModel):
    evidence_id: Id
    source: Literal["order", "warehouse", "logistics", "aftersales"]
    resource_id: Id
    locator: str
    observed_at: str
    fields: list[dict]
    policy: None = None


class ToolResult(BaseModel):
    status: Literal["ok", "not_found", "forbidden", "invalid_input", "timeout", "unavailable"]
    data: dict | None = None
    evidence: list[Evidence] = []


def signer(key):
    if len(key) < 32:
        raise ValueError("LOGIMATE_MCP_SIGNING_KEY must contain at least 32 characters")
    return URLSafeTimedSerializer(key, salt="logimate-mcp-v1")


def create_service(engine, signing_key):
    tokens = signer(signing_key)
    server = FastMCP("LogiMate business tools", json_response=True, stateless_http=True)

    def authorized(ctx, tool):
        request = ctx.request_context.request
        header = request.headers.get("authorization", "") if request else ""
        try:
            if not header.startswith("Bearer "):
                return None
            claims = tokens.loads(header.removeprefix("Bearer "), max_age=30)
        except BadSignature:
            return None
        if claims.get("aud") == "logimate-mcp" and claims.get("tool") == tool:
            return claims
        return None

    def lookup(connection, table, claims, **filters):
        query = select(table).filter_by(data_version=claims["data_version"], tenant_id=claims["tenant_id"], **filters)
        return list(connection.execute(query).mappings())

    def scoped_payloads(connection, table, claims, property_name, value):
        query = select(table.c.payload).where(
            table.c.data_version == claims["data_version"], table.c.tenant_id == claims["tenant_id"],
            table.c.payload[property_name].as_string() == value,
        )
        return list(connection.scalars(query))

    def respond(source, resource_id, claims, data, fields):
        evidence_id = store.hash_identifier(
            claims["tenant_id"], claims["run_id"], claims["data_version"], source, resource_id, claims["as_of"],
        )
        return ToolResult(status="ok", data=data, evidence=[Evidence(
            evidence_id=evidence_id, source=source, resource_id=resource_id,
            locator=f"{source}:{resource_id}:{claims['data_version']}", observed_at=claims["as_of"],
            fields=[{"name": name, "value": value} for name, value in fields.items()],
        )])

    def get_order(connection, claims, order_id):
        rows = lookup(connection, store.orders, claims, order_id=order_id)
        if rows and rows[0]["payload"]["placed_at"] <= claims["as_of"]:
            return rows[0]["payload"]
        return None

    def packages_for(connection, claims, order_id):
        query = select(store.packages.c.package_id).where(
            store.packages.c.data_version == claims["data_version"], store.packages.c.tenant_id == claims["tenant_id"],
            store.packages.c.payload["order_id"].as_string() == order_id,
        ).order_by(store.packages.c.package_id)
        return list(connection.scalars(query))

    def visible_events(events, as_of):
        return sorted((event for event in events if event["observed_at"] <= as_of),
                      key=lambda event: (event["occurred_at"], event["event_id"]))

    def execute(ctx, tool, process):
        claims = authorized(ctx, tool)
        if claims is None:
            return ToolResult(status="forbidden")
        try:
            with engine.connect() as connection:
                return process(connection, claims)
        except SQLAlchemyError:
            return ToolResult(status="unavailable")

    @server.tool(name="order.get")
    def order(order_id: Id, ctx: Context) -> ToolResult:
        """Read a visible order and its package identifiers."""
        def query(connection, claims):
            row = get_order(connection, claims, order_id)
            if row is None:
                return ToolResult(status="not_found")
            packages = packages_for(connection, claims, order_id)
            return respond("order", order_id, claims, {**row, "order_id": order_id, "package_ids": packages},
                           {"status": row["status"], "marked_shipped_at": row["marked_shipped_at"],
                            "promised_by": row["promised_by"]})

        return execute(ctx, "order.get", query)

    @server.tool(name="warehouse.get_events")
    def warehouse(order_id: Id, ctx: Context, package_id: Id | None = None) -> ToolResult:
        """Read shipment and handoff events through the bound snapshot time."""
        def query(connection, claims):
            if get_order(connection, claims, order_id) is None:
                return ToolResult(status="not_found")
            packages = packages_for(connection, claims, order_id)
            if package_id and package_id not in packages:
                return ToolResult(status="not_found")
            events = visible_events(scoped_payloads(connection, store.warehouse_events, claims, "order_id", order_id),
                                    claims["as_of"])
            if package_id:
                packages = [package_id]
                events = [event for event in events if event["package_id"] == package_id]
            return respond("warehouse", package_id or order_id, claims,
                           {"order_id": order_id, "package_ids": packages, "events": events,
                            "observed_through": claims["as_of"]},
                           {"handoff_recorded": any(event["event_type"] == "handed_off" for event in events),
                            "outbound_at": next((event["occurred_at"] for event in reversed(events)
                                                 if event["event_type"] == "outbound"), None)})

        return execute(ctx, "warehouse.get_events", query)

    @server.tool(name="logistics.get_tracking")
    def tracking(package_id: Id, ctx: Context) -> ToolResult:
        """Read the synthetic carrier feed for a visible package."""
        def query(connection, claims):
            entries = lookup(connection, store.packages, claims, package_id=package_id)
            if not entries or get_order(connection, claims, entries[0]["payload"]["order_id"]) is None:
                return ToolResult(status="not_found")
            if not entries[0]["payload"]["tracking_available"]:
                return ToolResult(status="unavailable")
            events = visible_events(scoped_payloads(connection, store.tracking_events, claims, "package_id", package_id),
                                    claims["as_of"])
            current = events[-1]["event_type"] if events else "no_events"
            return respond("logistics", package_id, claims,
                           {"package_id": package_id, "carrier_id": "synthetic-carrier", "status": current,
                            "estimated_delivery_at": None, "events": events, "observed_through": claims["as_of"]},
                           {"status": current, "latest_event_at": events[-1]["occurred_at"] if events else None})

        return execute(ctx, "logistics.get_tracking", query)

    @server.tool(name="aftersales.get_cases")
    def aftersales(order_id: Id, ctx: Context) -> ToolResult:
        """Read support cases for a visible order."""
        def query(connection, claims):
            if get_order(connection, claims, order_id) is None:
                return ToolResult(status="not_found")
            cases = [case for case in scoped_payloads(connection, store.aftersales_cases, claims, "order_id", order_id)
                     if case["updated_at"] <= claims["as_of"]]
            return respond("aftersales", order_id, claims,
                           {"order_id": order_id, "cases": cases, "observed_through": claims["as_of"]},
                           {"case_count": len(cases)})

        return execute(ctx, "aftersales.get_cases", query)

    return server.streamable_http_app()


def create_app():
    return create_service(store.database(os.environ["MYSQL_DSN"]), os.environ["LOGIMATE_MCP_SIGNING_KEY"])
