# -*- coding: utf-8 -*-
"""
admin_ai.schema — strict JSON response contract for the AdminBot AI
assistant, and its structural validator.

The model is constrained (in a later local milestone, via the provider's
structured-output config) to choose `capability_ids` ONLY from the enum
built here out of the currently-loaded, currently-VALID capability
registry — it has no field for callback data, controller commands,
manager/source keys, DB ids, or file paths anywhere in this schema. That
is the core containment mechanism: `validate_response` drops any id that
is not in the live `valid_capability_ids` set regardless of what the raw
model output claims, so an unknown/stale/disabled id can never reach a
button.

Pure stdlib. No panel_bot/main/telethon imports.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Sequence

ALLOWED_CONFIDENCES: FrozenSet[str] = frozenset({"low", "medium", "high"})

# Entity-slot vocabulary this package resolves (see entities.py). A
# capability in capabilities.json may only declare entity_slots drawn from
# this set (enforced in registry.py); the model may only tag raw_text
# hints against these same slot names.
ALLOWED_ENTITY_SLOTS: FrozenSet[str] = frozenset({
    "manager_key", "source_key", "date", "period", "lease_id", "process_name",
})

MAX_MESSAGE_CHARS = 700
MAX_INTENT_CHARS = 120
MAX_RAW_TEXT_CHARS = 200
MAX_WARNING_CHARS = 300
DEFAULT_MAX_CAPABILITY_IDS = 5


def build_response_schema(capability_ids: Sequence[str], max_capability_ids: int = DEFAULT_MAX_CAPABILITY_IDS) -> Dict[str, Any]:
    """JSON-schema for the assistant's response (Anthropic structured-output
    compatible: plain JSON Schema, `additionalProperties: False`).

    `capability_ids` MUST be the currently enabled set from
    registry.RegistryLoadResult.enabled_ids() — never the full curated
    file, or a disabled/invalid entry would become selectable.
    """
    ids = sorted(set(capability_ids))
    id_schema: Dict[str, Any] = {"type": "string", "enum": ids} if ids else {"type": "string", "enum": ["__no_capabilities_available__"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "confidence", "message", "capability_ids", "needs_clarification"],
        "properties": {
            "intent": {"type": "string", "maxLength": MAX_INTENT_CHARS},
            "confidence": {"type": "string", "enum": sorted(ALLOWED_CONFIDENCES)},
            "message": {"type": "string", "maxLength": MAX_MESSAGE_CHARS},
            "capability_ids": {
                "type": "array",
                "maxItems": max_capability_ids,
                "items": id_schema,
            },
            "entity_queries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["slot", "raw_text"],
                    "properties": {
                        "slot": {"type": "string", "enum": sorted(ALLOWED_ENTITY_SLOTS)},
                        "raw_text": {"type": "string", "maxLength": MAX_RAW_TEXT_CHARS},
                    },
                },
            },
            "needs_clarification": {"type": "boolean"},
            "warnings": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_WARNING_CHARS},
            },
        },
    }


def validate_response(raw: Any, valid_capability_ids: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Structural validation + containment. Returns a clean, trimmed dict if
    the response is usable at all, else None (caller must fall back to a
    deterministic message). This is the ONLY function that decides whether
    a model response may be acted on — never bypass it.
    """
    if not isinstance(raw, dict):
        return None

    valid_ids = set(valid_capability_ids)

    confidence = raw.get("confidence")
    if confidence not in ALLOWED_CONFIDENCES:
        return None

    message = raw.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    message = message[:MAX_MESSAGE_CHARS]

    intent = raw.get("intent")
    intent = intent[:MAX_INTENT_CHARS] if isinstance(intent, str) else ""

    cap_ids_raw = raw.get("capability_ids")
    if cap_ids_raw is None:
        cap_ids_raw = []
    if not isinstance(cap_ids_raw, list):
        return None
    clean_ids: List[str] = []
    for cid in cap_ids_raw:
        if not isinstance(cid, str):
            continue  # never trust a non-string id (containment backstop)
        if cid not in valid_ids:
            continue  # CONTAINMENT: silently drop anything not currently enabled
        if cid not in clean_ids:
            clean_ids.append(cid)

    needs_clarification = bool(raw.get("needs_clarification", False))
    if confidence == "low":
        needs_clarification = True  # low confidence always forces clarification, regardless of model claim

    entity_queries: List[Dict[str, str]] = []
    eq_raw = raw.get("entity_queries")
    if isinstance(eq_raw, list):
        for eq in eq_raw:
            if not isinstance(eq, dict):
                continue
            slot = eq.get("slot")
            raw_text = eq.get("raw_text")
            if slot not in ALLOWED_ENTITY_SLOTS:
                continue
            if not isinstance(raw_text, str) or not raw_text.strip():
                continue
            entity_queries.append({"slot": slot, "raw_text": raw_text[:MAX_RAW_TEXT_CHARS]})

    warnings: List[str] = []
    warnings_raw = raw.get("warnings")
    if isinstance(warnings_raw, list):
        for w in warnings_raw:
            if isinstance(w, str) and w.strip():
                warnings.append(w[:MAX_WARNING_CHARS])

    return {
        "intent": intent,
        "confidence": confidence,
        "message": message,
        "capability_ids": clean_ids,
        "entity_queries": entity_queries,
        "needs_clarification": needs_clarification,
        "warnings": warnings,
    }
