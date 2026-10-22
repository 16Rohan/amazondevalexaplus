"""Deterministic, simulated Alexa MCP service."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

mcp = MCPServer("Alexa Appliance Service")
DATA_DIR = Path(__file__).resolve().parent / "data"

# Fixtures loaded from disk
_APPLIANCES: dict[str, dict[str, Any]] = {}
_TROUBLESHOOTING: list[dict[str, Any]] = []
_WARRANTIES: dict[str, dict[str, Any]] = {}
_SERVICE_SLOTS: list[dict[str, Any]] = []
_CASES: dict[str, dict[str, Any]] = {}

_REQUESTS: dict[str, dict[str, Any]] = {}
_IDEMPOTENT_REQUESTS: dict[str, tuple[str, str]] = {}


def _load_fixtures() -> None:
    """Load fixtures from mcp/data/ directory on disk."""
    global _APPLIANCES, _TROUBLESHOOTING, _WARRANTIES, _SERVICE_SLOTS, _CASES

    # Backward compatibility defaults
    _APPLIANCES = {
        "appliance-001": {
            "appliance_id": "appliance-001",
            "product_id": "product-001",
            "brand": "WhirlClean",
            "model_number": "WC-500",
            "display_name": "WhirlClean WC-500 Washing Machine",
            "appliance_type": "washing_machine",
            "is_simulated": True,
        }
    }
    _WARRANTIES = {
        "appliance-001": {
            "appliance_id": "appliance-001",
            "eligibility": "eligible",
            "coverage_end_date": "2027-12-31",
            "reason": "Simulated warranty record.",
            "evidence_source": "simulated_record",
            "is_simulated": True,
        }
    }

    try:
        appliances_file = DATA_DIR / "appliances.json"
        if appliances_file.is_file():
            data = json.loads(appliances_file.read_text(encoding="utf-8"))
            for record in data.get("records", []):
                aid = record.get("appliance_id")
                if aid:
                    _APPLIANCES[aid] = record
    except Exception as exc:
        print(f"Warning: Failed to load appliances.json: {exc}", file=sys.stderr)

    try:
        trouble_file = DATA_DIR / "troubleshooting.json"
        if trouble_file.is_file():
            data = json.loads(trouble_file.read_text(encoding="utf-8"))
            _TROUBLESHOOTING = data.get("records", [])
    except Exception as exc:
        print(f"Warning: Failed to load troubleshooting.json: {exc}", file=sys.stderr)

    try:
        warranty_file = DATA_DIR / "warranty.json"
        if warranty_file.is_file():
            data = json.loads(warranty_file.read_text(encoding="utf-8"))
            for record in data.get("records", []):
                aid = record.get("appliance_id")
                if aid:
                    _WARRANTIES[aid] = record
    except Exception as exc:
        print(f"Warning: Failed to load warranty.json: {exc}", file=sys.stderr)

    try:
        slots_file = DATA_DIR / "service_slots.json"
        if slots_file.is_file():
            data = json.loads(slots_file.read_text(encoding="utf-8"))
            _SERVICE_SLOTS = data.get("slots", [])
    except Exception as exc:
        print(f"Warning: Failed to load service_slots.json: {exc}", file=sys.stderr)

    try:
        cases_file = DATA_DIR / "cases.json"
        if cases_file.is_file():
            data = json.loads(cases_file.read_text(encoding="utf-8"))
            for record in data.get("records", []):
                cid = record.get("request_id")
                if cid:
                    _CASES[cid] = record
    except Exception as exc:
        print(f"Warning: Failed to load cases.json: {exc}", file=sys.stderr)


# Load fixtures on startup
_load_fixtures()


def _clean(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be blank")
    return value


def _record_execution(tool_name: str) -> None:
    """Emit a minimal server-side audit record without logging user arguments."""
    print(f"MCP_TOOL_EXECUTED tool={tool_name}", file=sys.stderr, flush=True)


def _confirmation_payload(appliance_id: str, slot_id: str | None, issue_summary: str) -> str:
    return "|".join((appliance_id, slot_id or "", issue_summary))


def _valid_confirmation(appliance_id: str, slot_id: str | None, issue_summary: str, confirmation_id: str) -> bool:
    """Verify a backend-issued HMAC token bound to the proposed action."""
    secret = os.getenv("MCP_CONFIRMATION_SECRET")
    if not secret:
        return False
    digest = hmac.new(secret.encode(), _confirmation_payload(appliance_id, slot_id, issue_summary).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(confirmation_id, f"mcp-confirmation-{digest}")


def _request_failure(status: str, slot_id: str | None) -> dict[str, Any]:
    return {"status": status, "request_id": None, "request_status": "failed", "booking_status": "unknown", "provider_name": None, "slot_id": slot_id, "confirmation_reference": None, "is_simulated": True}


@mcp.tool(description="Look up simulated household appliance inventory and purchase history for the user. Do not confuse with find_product (catalogue search).")
def get_appliance_inventory(appliance_type: str | None = None, appliance_id: str | None = None, household_id: str | None = None) -> dict[str, Any]:
    """Return user's simulated owned household appliances and purchase history."""
    _record_execution("get_appliance_inventory")
    target_household = household_id or "demo-household"
    results = []
    for app in _APPLIANCES.values():
        if household_id != "all" and app.get("household_id", "demo-household") != target_household:
            continue
        if appliance_id and app.get("appliance_id") != appliance_id:
            continue
        if appliance_type and app.get("appliance_type") != appliance_type and appliance_type.lower() not in (app.get("appliance_type") or "").lower():
            continue
        results.append(app)
    return {
        "status": "success" if results else "not_found",
        "total_count": len(results),
        "records": results,
        "appliances": results,
        "household_id": target_household,
        "is_simulated": True,
    }


@mcp.tool(description="Search the simulated appliance catalogue only; this does not verify ownership or purchase history.")
def find_product(query: str = Field(min_length=1), appliance_type: str | None = None, brand: str | None = None, model_number: str | None = None) -> dict[str, Any]:
    """Return matching simulated catalogue products."""
    query = _clean(query, "query")
    _record_execution("find_product")
    filters = [query, appliance_type or "", brand or "", model_number or ""]
    matches = []
    search_keys = ("display_name", "brand", "model_number", "appliance_type", "category")
    for appliance in _APPLIANCES.values():
        values = [str(appliance[k]) for k in search_keys if k in appliance and appliance[k] is not None]
        haystack = " ".join(values).lower()
        if all(term.lower() in haystack for term in filters if term):
            basis = [key for key in search_keys if key in appliance and isinstance(appliance[key], str) and query.lower() in appliance[key].lower()]
            matches.append({**appliance, "match_basis": basis or ["query"]})
    return {"status": "success" if matches else "not_found", "matches": matches, "is_simulated": True}


@mcp.tool(description="Return safe simulated troubleshooting guidance. It is not a substitute for manufacturer documentation or a technician.")
def get_troubleshooting_steps(model_id: str = Field(min_length=1), symptom: str = Field(min_length=1), error_code: str | None = None) -> dict[str, Any]:
    """Return only safety-conscious, deterministic demo guidance."""
    model_id, symptom = _clean(model_id, "model_id"), _clean(symptom, "symptom")
    _record_execution("get_troubleshooting_steps")

    # Match against troubleshooting records from troubleshooting.json
    lowered_sym = symptom.lower()
    lowered_err = (error_code or "").lower()
    lowered_mod = model_id.lower()

    # Scenario B: Refrigerator not cooling
    if "cooling" in lowered_sym or "fridge" in lowered_mod or "column-ref" in lowered_mod or "refrigerator" in lowered_sym or "lfxs" in lowered_mod:
        fridge_rec = next((r for r in _TROUBLESHOOTING if r.get("appliance_type") == "refrigerator"), None)
        probable_causes = fridge_rec.get("probable_causes", []) if fridge_rec else [
            "Temperature controls may be set incorrectly.",
            "Internal air vents may be obstructed.",
            "Frequent door opening may temporarily affect temperature.",
        ]
        safe_steps = fridge_rec.get("safe_steps", []) if fridge_rec else [
            "Check temperature settings without opening service panels.",
            "Ensure internal air vents are not obstructed by food packages.",
            "Ensure doors close fully.",
        ]
        stop_conditions = fridge_rec.get("stop_conditions", []) if fridge_rec else [
            "Smoke, burning smell, or electrical hazard: stop and seek qualified help."
        ]
        return {
            "status": "success",
            "model_id": model_id,
            "exact_model_match": False,
            "document_found": True,
            "documentation_scope": "general manufacturer guidance, not exact-model diagnosis",
            "document_reference": {"title": "General Manufacturer Guidance", "page": None, "url": None},
            "documented_error_meaning": None,
            "probable_causes": probable_causes,
            "safe_steps": safe_steps,
            "stop_conditions": stop_conditions,
            "requires_technician": False,
            "is_simulated": True,
            "source_ids": fridge_rec.get("source_ids", []) if fridge_rec else [],
        }

    # Scenario A / WC-500: Washing machine error E21 / documentation_not_found
    if model_id == "WC-500" or "e21" in lowered_err or "e21" in lowered_sym or "wm8000" in lowered_mod or (("washer" in lowered_sym or "washing" in lowered_sym) and "dish" not in lowered_sym):
        washer_rec = next((r for r in _TROUBLESHOOTING if r.get("appliance_type") == "washing_machine"), None)
        probable_causes = ["A temporary cycle interruption", "An appliance condition requiring service"] if model_id == "WC-500" else (washer_rec.get("probable_causes", []) if washer_rec else [])
        safe_steps = (washer_rec.get("safe_steps", []) if washer_rec else [
            "Turn the appliance off.",
            "Disconnect power only if it is safe to do so.",
            "Do not remove panels or bypass safety interlocks.",
        ])
        return {
            "status": "documentation_not_found",
            "model_id": model_id,
            "exact_model_match": (model_id == "WC-500"),
            "document_found": False,
            "document_reference": {"title": None, "page": None, "url": None},
            "documented_error_meaning": None,
            "probable_causes": probable_causes,
            "safe_steps": safe_steps,
            "requires_technician": True,
            "is_simulated": True,
            "source_ids": washer_rec.get("source_ids", []) if washer_rec else [],
        }

    # Check across other categories in _TROUBLESHOOTING
    for rec in _TROUBLESHOOTING:
        app_type = rec.get("appliance_type", "").lower()
        if app_type in lowered_sym or app_type in lowered_mod:
            return {
                "status": "success",
                "model_id": model_id,
                "exact_model_match": False,
                "document_found": True,
                "documentation_scope": f"category guidance for {app_type.replace('_', ' ')}, not exact-model diagnosis",
                "document_reference": {"title": f"Manufacturer Guidance ({app_type.replace('_', ' ').capitalize()})", "page": None, "url": None},
                "documented_error_meaning": None,
                "probable_causes": rec.get("probable_causes", []),
                "safe_steps": rec.get("safe_steps", []),
                "stop_conditions": rec.get("stop_conditions", []),
                "requires_technician": False,
                "is_simulated": True,
                "source_ids": rec.get("source_ids", []),
            }

    # General unverified model
    return {
        "status": "model_unverified",
        "model_id": model_id,
        "exact_model_match": False,
        "document_found": False,
        "document_reference": {"title": None, "page": None, "url": None},
        "documented_error_meaning": None,
        "probable_causes": ["Unverified appliance condition", "Installation or utility supply issue"],
        "safe_steps": ["Stop use if there is a burning smell, leak, or electrical damage and contact a qualified technician."],
        "stop_conditions": ["Smoke, sparks, burning odor, or electrical hazard: stop immediately and seek qualified help."],
        "requires_technician": True,
        "is_simulated": True,
    }


@mcp.tool(description="Check simulated warranty evidence for a known appliance record.")
def check_warranty(appliance_id: str = Field(min_length=1)) -> dict[str, Any]:
    appliance_id = _clean(appliance_id, "appliance_id")
    _record_execution("check_warranty")
    record = _WARRANTIES.get(appliance_id)
    if not record:
        return {"status": "not_found", "appliance_id": appliance_id, "eligibility": "unknown", "coverage_end_date": None, "reason": "No appliance record found.", "evidence_source": "simulated_record", "is_simulated": True}
    return {
        "status": "success",
        "appliance_id": appliance_id,
        "eligibility": record.get("eligibility", "eligible"),
        "coverage_end_date": record.get("coverage_end_date"),
        "reason": record.get("reason", "Simulated warranty record."),
        "evidence_source": record.get("evidence_source", "simulated_record"),
        "is_simulated": True,
    }


@mcp.tool(description="Find simulated service availability. Returned slots are not held or booked.")
def find_service_slots(appliance_id: str = Field(min_length=1), location: str = Field(min_length=1), date_from: date = ..., date_to: date = ...) -> dict[str, Any]:
    appliance_id, location = _clean(appliance_id, "appliance_id"), _clean(location, "location")
    _record_execution("find_service_slots")
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")

    # If appliance is known or slots exist
    if _SERVICE_SLOTS:
        # Return synthetic demo slots from service_slots.json
        slots = []
        for s in _SERVICE_SLOTS:
            slot_copy = dict(s)
            slot_copy["is_simulated"] = True
            slots.append(slot_copy)
        return {"status": "success", "availability_type": "estimated", "slots": slots, "is_simulated": True}

    if appliance_id not in _APPLIANCES:
        return {"status": "no_slots", "availability_type": "estimated", "slots": [], "is_simulated": True}
    slot_id = f"sim-{appliance_id}-{date_from.isoformat()}-0900"
    return {"status": "success", "availability_type": "estimated", "slots": [{"slot_id": slot_id, "provider_name": "Simulated Alexa Appliance Service", "start_at": datetime.combine(date_from, time(9)).isoformat(), "end_at": datetime.combine(date_from, time(11)).isoformat(), "estimated_fee": 89.00, "currency": "USD", "is_simulated": True}], "is_simulated": True}


@mcp.tool(description="Create a simulated service request only with a backend-issued confirmation bound to the exact appliance, issue, and slot. This does not book an appointment.")
def create_service_request(appliance_id: str = Field(min_length=1), issue_summary: str = Field(min_length=1), confirmation_id: str = Field(min_length=1), idempotency_key: str = Field(min_length=1), slot_id: str | None = None) -> dict[str, Any]:
    appliance_id, issue_summary = _clean(appliance_id, "appliance_id"), _clean(issue_summary, "issue_summary")
    confirmation_id, idempotency_key = _clean(confirmation_id, "confirmation_id"), _clean(idempotency_key, "idempotency_key")
    _record_execution("create_service_request")
    fingerprint = _confirmation_payload(appliance_id, slot_id, issue_summary)
    existing = _IDEMPOTENT_REQUESTS.get(idempotency_key)
    if existing:
        return _REQUESTS[existing[1]] if existing[0] == fingerprint else _request_failure("confirmation_invalid", slot_id)
    if appliance_id not in _APPLIANCES:
        return _request_failure("not_found", slot_id)
    if not _valid_confirmation(appliance_id, slot_id, issue_summary, confirmation_id):
        return _request_failure("confirmation_invalid", slot_id)
    request_id = f"sim-request-{len(_REQUESTS) + 1:04d}"
    result = {"status": "success", "request_id": request_id, "request_status": "created", "booking_status": "not_booked", "provider_name": "Simulated Alexa Appliance Service" if slot_id else None, "slot_id": slot_id, "confirmation_reference": confirmation_id, "is_simulated": True}
    _REQUESTS[request_id] = {**result, "last_updated": datetime.now().astimezone().isoformat(), "summary": issue_summary}
    _IDEMPOTENT_REQUESTS[idempotency_key] = (fingerprint, request_id)
    return result


@mcp.tool(description="Retrieve the status of a simulated service request.")
def get_case_status(request_id: str = Field(min_length=1)) -> dict[str, Any]:
    request_id = _clean(request_id, "request_id")
    _record_execution("get_case_status")
    request = _REQUESTS.get(request_id)
    if not request and request_id in _CASES:
        request = _CASES[request_id]
        return {
            "status": "success",
            "request_id": request_id,
            "request_status": request.get("request_status", "resolved"),
            "booking_status": request.get("booking_status", "not_booked"),
            "last_updated": request.get("last_updated"),
            "summary": request.get("summary", request.get("issue_summary", "Simulated support case.")),
            "is_simulated": True,
        }
    if not request:
        return {"status": "not_found", "request_id": request_id, "request_status": "unknown", "booking_status": "unknown", "last_updated": None, "summary": "No service request found.", "is_simulated": True}
    return {key: request[key] for key in ("status", "request_id", "request_status", "booking_status", "last_updated", "summary", "is_simulated")}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=int(os.getenv("MCP_PORT", "8001")))
