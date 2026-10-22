"""Alexa Conversational Intelligence & Evidence-Aware Synthesis Layer.

Provides structured intent classification, missing-model synthesis with explicit
evidence categories, and deterministic conversational fallbacks for Alexa.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

# Explicit evidence provenance categories required by specification
VERIFIED_MANUFACTURER_DOCUMENTATION = "verified_manufacturer_documentation"
VERIFIED_CATALOGUE_RECORD = "verified_catalogue_record"
HOUSEHOLD_RECORD = "household_record"
SYNTHETIC_DEMO_RECORD = "synthetic_demo_record"
GENERIC_APPLIANCE_KNOWLEDGE = "generic_appliance_knowledge"
MODEL_INFERENCE = "model_inference"


def classify_conversational_intent(text: str) -> str | None:
    """Detect general conversational intents that must NOT trigger MCP appliance tools."""
    clean = re.sub(r"[^\w\s]", "", text.lower()).strip()
    words = clean.split()
    if not words:
        return None

    # 1. Greetings
    greeting_tokens = {
        "hello", "helloo", "hellooo", "hi", "hey", "heya", "howdy", "greetings",
        "good morning", "good afternoon", "good evening", "hi there", "hello there"
    }
    if clean in greeting_tokens or (len(words) <= 2 and words[0] in {"hello", "hi", "hey", "howdy"}):
        # Ensure it's not a greeting followed by an appliance issue (e.g. "Hello my fridge is broken")
        appliance_keywords = {"refrigerator", "fridge", "washer", "washing", "dryer", "dishwasher", "oven", "ac", "microwave", "heater", "purifier", "tv", "broken", "leak", "cooling", "error"}
        if not any(k in words for k in appliance_keywords):
            return "greeting"

    # 2. Wellbeing ("How are you?")
    wellbeing_patterns = [
        "how are you", "how are you doing", "how do you do", "how is it going",
        "hows it going", "how are things", "hope you are well"
    ]
    if any(clean.startswith(p) or clean == p for p in wellbeing_patterns):
        return "wellbeing"

    # 3. Capabilities ("What can you do?")
    capability_patterns = [
        "what can you do", "what do you do", "who are you", "help me", "what are your features",
        "what are your capabilities", "what can i ask", "how can you help"
    ]
    if any(p in clean for p in capability_patterns) and not any(k in clean for k in ("refrigerator", "washer", "dryer", "oven", "dishwasher")):
        return "capabilities"

    # 4. Gratitude ("Thank you")
    gratitude_tokens = [
        "thank you", "thanks", "thanks a lot", "thank you so much", "thx", "appreciate it", "many thanks"
    ]
    if clean in gratitude_tokens or any(clean.startswith(g) for g in ("thank you", "thanks")):
        if not any(k in clean for k in ("troubleshoot", "repair", "service", "warranty")):
            return "gratitude"

    # 5. Humor ("Tell me a joke")
    joke_patterns = [
        "tell me a joke", "tell a joke", "make me laugh", "say something funny", "do you know any jokes", "crack a joke"
    ]
    if any(p in clean for p in joke_patterns):
        return "humor"

    # 6. Conversation history query ("What did we discuss earlier?")
    history_patterns = [
        "what did we discuss earlier", "what did we talk about", "what was the previous topic",
        "what did i say earlier", "what were we discussing"
    ]
    if any(p in clean for p in history_patterns):
        return "history_query"

    return None


def get_conversational_response(intent: str, prior_responses: list[dict[str, Any]] | None = None) -> str:
    """Return a natural, concise, conversational response from Alexa."""
    if intent == "greeting":
        return "Hello! I'm Alexa. How can I help you today?"
    if intent == "wellbeing":
        return "I'm doing well, thank you for asking! What can I help you with today?"
    if intent == "capabilities":
        return (
            "I'm Alexa, your conversational assistant. You can chat with me naturally, ask questions, "
            "or ask for help managing and troubleshooting your household appliances—like refrigerators, "
            "washers, dryers, dishwashers, and ovens. How can I assist you right now?"
        )
    if intent == "gratitude":
        return "You're very welcome! Let me know if there's anything else I can do for you."
    if intent == "humor":
        return "Why did the refrigerator stop running? Because it needed to catch its breath! What can I help you with?"
    if intent == "history_query":
        if prior_responses:
            turns = []
            for resp in prior_responses[-3:]:
                u_content = resp.get("user_message", {}).get("content")
                if u_content:
                    turns.append(f"• You asked: \"{u_content}\"")
            if turns:
                return "Earlier in our conversation, we discussed:\n" + "\n".join(turns) + "\n\nWould you like to continue with any of these?"
        return "We haven't discussed any specific appliances or questions yet in this conversation. What would you like to explore?"

    return "I'm here to help. What's on your mind?"


def synthesize_missing_model_guidance(
    model_id: str,
    symptom: str,
    appliance_type: str = "appliance",
    brand: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """
    Dedicated synthesis layer for missing or unverified appliance models.
    Produces safe, qualified generic guidance with explicit evidence categories
    and plain uncertainty disclosures without inventing citations.
    """
    brand_str = f"{brand} " if brand else ""
    app_type_clean = appliance_type.replace("_", " ")

    # Category-specific safe checks
    safe_checks: list[str] = []
    stop_conditions: list[str] = [
        "Smoke, sparks, burning odor, or damaged power cords: stop immediately, disconnect power safely, and call a qualified technician.",
        "Active water leakage near electrical sockets or behind wall panels: turn off the main water valve and seek professional assistance.",
        "Never remove service panels, handle refrigerant lines, or attempt internal electrical repairs yourself."
    ]

    lowered_sym = symptom.lower()
    lowered_type = appliance_type.lower()

    if "refrigerat" in lowered_type or "cooling" in lowered_sym or "fridge" in lowered_sym:
        safe_checks = [
            "Check temperature controls without removing panels (recommended: 37°F / 3°C for fresh food, 0°F / -18°C for freezer).",
            "Verify that internal cold-air vents are not blocked by food containers or ice buildup.",
            "Inspect the door perimeter gaskets to ensure a tight magnetic seal without tears or gaps.",
            "If the refrigerator was recently installed or stocked with groceries, allow up to 24 hours for internal temperatures to stabilize."
        ]
        probable_causes = [
            "Thermostat dial bumped or set too warm.",
            "Internal air circulation obstructed by packed items.",
            "Door opened frequently or not sealing completely."
        ]
    elif "wash" in lowered_type or "drain" in lowered_sym or "e21" in (error_code or "").lower():
        safe_checks = [
            "Check that the drain hose is not kinked, crushed, or positioned higher than 8 feet off the floor.",
            "Inspect the drain pump filter access door (if accessible from the front base) for trapped coins, lint, or debris.",
            "Ensure the cold water supply valves behind the washer are fully open."
        ]
        probable_causes = [
            "Drain filter or drain hose blockage.",
            "Temporary cycle interruption.",
            "Water inlet flow restriction."
        ]
    elif "dish" in lowered_type:
        safe_checks = [
            "Twist and lift the cylindrical filter at the bottom of the tub; rinse away trapped food debris.",
            "Check that spray arms spin freely by hand and spray nozzles are not clogged with hard water scale.",
            "Ensure the sink drain and garbage disposal knockout are clear of blockages."
        ]
        probable_causes = [
            "Clogged bottom sump filter.",
            "Blocked spray arm jets.",
            "Drain line restriction."
        ]
    elif "air_cond" in lowered_type or "ac" in lowered_type:
        safe_checks = [
            "Slide out washable return-air mesh filters and clean with water; let dry thoroughly.",
            "Verify remote control is set to 'Cool' mode with temperature set below current room temperature.",
            "Ensure outdoor condenser unit has at least 12 inches of clearance around coils."
        ]
        probable_causes = [
            "Dirty air filters restricting airflow.",
            "Incorrect thermostat or mode setting.",
            "Outdoor heat exchanger airflow obstruction."
        ]
    else:
        safe_checks = [
            "Perform a soft reset: turn off the appliance, disconnect power for 60 seconds, and reconnect.",
            "Inspect power outlet and circuit breaker to confirm uninterrupted electrical supply.",
            "Check that external vents, filters, or intake grilles are free of visible dust."
        ]
        probable_causes = [
            "Temporary microprocessor lockup.",
            "External power supply fluctuation.",
            "Restricted airflow or mechanical obstruction."
        ]

    explanation = (
        f"I couldn't locate an exact-model manual for {brand_str}{model_id}. "
        f"However, based on standard {app_type_clean} architecture, here are safe, non-invasive checks "
        f"you can perform right now. You can also provide the model number from the rating plate on the appliance "
        f"for more specific guidance."
    )

    return {
        "status": "model_unverified",
        "model_id": model_id,
        "exact_model_match": False,
        "document_found": False,
        "evidence_category": GENERIC_APPLIANCE_KNOWLEDGE,
        "documentation_scope": "generic appliance safety guidance (exact model record missing)",
        "document_reference": {"title": None, "page": None, "url": None},
        "documented_error_meaning": None,
        "probable_causes": probable_causes,
        "safe_steps": safe_checks,
        "stop_conditions": stop_conditions,
        "explanation": explanation,
        "requires_technician": True,
        "is_simulated": True,
        "missing_model": True,
    }
