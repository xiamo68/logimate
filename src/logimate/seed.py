"""Deterministic synthetic material. The online service does not import this module."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

DATA_VERSION = "synthetic-v1"
AS_OF = datetime(2026, 11, 19, 8, tzinfo=timezone.utc)
TENANTS = ("alpha", "beta")

FAMILIES = {
    "improvement": ("walkthrough", "late_dispatch", "missing_pickup_scan"),
    "validation": ("carrier_backlog", "delayed_handoff", "conflicting_scans"),
    "final": ("weekend_transfer", "regional_delay", "false_alarm", "partial_feed", "scan_gap"),
}


def fingerprint(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def timestamp(hours_ago):
    return (AS_OF - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def snapshot():
    business = []
    cases = {"improvement": [], "validation": [], "final": []}
    for split, families in FAMILIES.items():
        count = 100 if split == "final" else 15
        for index in range(count):
            tenant = TENANTS[index % 2]
            order_id = f"LM-{1001 + index}" if split == "improvement" else f"LM-{split}-{index:03d}"
            package_id = order_id.replace("LM-", "PK-", 1)
            family = families[index % len(families)]
            outcome = index % 5
            handed_off = outcome in (1, 2, 4)
            picked_up = outcome in (2, 4)
            expected = ("awaiting_handoff", "pickup_overdue", "normal", "needs_verification", "in_transit_delayed")[outcome]
            warehouse = [{"event_type": "outbound", "occurred_at": timestamp(73)}]
            tracking = [{"event_type": "label_created", "occurred_at": timestamp(74)}]
            if handed_off:
                warehouse.append({"event_type": "handed_off", "occurred_at": timestamp(72)})
            if picked_up:
                tracking.append({"event_type": "picked_up", "occurred_at": timestamp(70 if outcome == 4 else 1)})
            for kind, events in (("warehouse", warehouse), ("tracking", tracking)):
                for event_index, event in enumerate(events):
                    event.update(event_id=f"{kind}-{package_id}-{event_index}", package_id=package_id,
                                 observed_at=event["occurred_at"])
            business.append({
                "tenant_id": tenant, "order_id": order_id, "package_id": package_id,
                "order": {"status": "shipped", "placed_at": timestamp(76), "marked_shipped_at": timestamp(74),
                          "promised_by": timestamp(2 if outcome == 4 else -24),
                          "amount_minor": 10000 + index * 137, "currency": "CNY"},
                "warehouse_events": warehouse, "tracking_events": tracking,
                "tracking_available": outcome != 3,
                "aftersales_cases": [{"case_id": f"CS-{package_id}", "status": "open",
                                      "reason_code": "shipping_inquiry", "opened_at": timestamp(1),
                                      "updated_at": timestamp(1), "timeline": [
                                          {"status": "open", "reason_code": "shipping_inquiry",
                                           "occurred_at": timestamp(1)}
                                      ]}] if index % 3 == 0 else [],
            })
            cases[split].append({"case_id": f"{split}-{index:03d}", "family": family, "tenant_id": tenant,
                                 "order_id": order_id, "expected": expected})
    policies = [
        {"document_id": "refund-v1", "title": "退款规则", "body": "## 发货前退款\n订单尚未出库时可申请取消并退款。\n## 在途退款\n物流已揽收的订单须先核实退回轨迹。"},
        {"document_id": "claim-v1", "title": "物流赔付规则", "body": "## 揽收超时\n实际交接后超过 48 小时未揽收可发起承运商核实。\n## 签收争议\n须提供签收凭证及运单号后受理。"},
    ]
    aftersales = [
        {"case_id": f"rag-final-{index:03d}", "order_id": item["order_id"], "tenant_id": item["tenant_id"],
         "question": question, "expected_document": document}
        for index, item in enumerate(cases["final"][:60])
        for question, document in [(
            ("还没出库能退款吗？", "refund-v1") if index % 3 == 0
            else ("显示签收却没收到，如何申请核实？", "claim-v1") if index % 3 == 1
            else ("是否提供设备延保？", None)
        )]
    ]
    return {"data_version": DATA_VERSION, "as_of": timestamp(0), "business": business,
            "policies": policies, "logistics": cases, "aftersales": aftersales}


def export_cases(data, output):
    manifest = {"data_version": data["data_version"], "as_of": data["as_of"], "datasets": {}}
    for split, cases in {**data["logistics"], "aftersales_final": data["aftersales"]}.items():
        folder = output / ("final" if split.endswith("final") else split)
        folder.mkdir(parents=True, exist_ok=True)
        name = "aftersales.json" if split == "aftersales_final" else "logistics.json"
        path = folder / name
        path.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest["datasets"][split] = {"count": len(cases), "sha256": fingerprint(cases),
                                      "file": path.relative_to(output).as_posix()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
