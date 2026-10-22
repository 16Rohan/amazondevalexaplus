"""Tests for Alexa Conversational Intelligence, System Prompt, Missing-Model Synthesis, and 100+ Dataset Records."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm.main import (
    _deterministic_intent,
    _deterministic_mcp_message,
    send_message,
    ConversationStore,
    LlmConfig,
)
from llm.synthesis import (
    classify_conversational_intent,
    get_conversational_response,
    synthesize_missing_model_guidance,
    VERIFIED_MANUFACTURER_DOCUMENTATION,
    GENERIC_APPLIANCE_KNOWLEDGE,
    SYNTHETIC_DEMO_RECORD,
    HOUSEHOLD_RECORD,
    MODEL_INFERENCE,
)

mcp_path = ROOT / "mcp" / "main.py"
spec = importlib.util.spec_from_file_location("local_mcp_main", mcp_path)
mcp_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp_module)

_APPLIANCES = mcp_module._APPLIANCES
_WARRANTIES = mcp_module._WARRANTIES
_TROUBLESHOOTING = mcp_module._TROUBLESHOOTING
get_appliance_inventory = mcp_module.get_appliance_inventory
find_product = mcp_module.find_product
get_troubleshooting_steps = mcp_module.get_troubleshooting_steps
check_warranty = mcp_module.check_warranty


class TestAlexaConversationalIntelligence(unittest.TestCase):
    """Verify general conversational modes without tool execution."""

    def test_greeting_intents(self) -> None:
        for greeting in ("Hello", "Helloo", "helloo", "Hey", "Hi there", "Good morning"):
            intent = classify_conversational_intent(greeting)
            self.assertEqual(intent, "greeting", f"Failed for '{greeting}'")
            resp = get_conversational_response(intent)
            self.assertIn("Alexa", resp)

    def test_wellbeing_intent(self) -> None:
        for phrase in ("How are you?", "how are you doing", "how is it going"):
            intent = classify_conversational_intent(phrase)
            self.assertEqual(intent, "wellbeing", f"Failed for '{phrase}'")
            resp = get_conversational_response(intent)
            self.assertIn("doing well", resp.lower())

    def test_capabilities_intent(self) -> None:
        for phrase in ("What can you do?", "what do you do", "help me"):
            intent = classify_conversational_intent(phrase)
            self.assertEqual(intent, "capabilities", f"Failed for '{phrase}'")
            resp = get_conversational_response(intent)
            self.assertIn("Alexa", resp)
            self.assertIn("appliances", resp.lower())

    def test_gratitude_intent(self) -> None:
        for phrase in ("Thank you", "thanks", "thank you so much", "thanks a lot"):
            intent = classify_conversational_intent(phrase)
            self.assertEqual(intent, "gratitude", f"Failed for '{phrase}'")
            resp = get_conversational_response(intent)
            self.assertIn("welcome", resp.lower())

    def test_humor_intent(self) -> None:
        for phrase in ("Tell me a joke", "make me laugh", "say something funny"):
            intent = classify_conversational_intent(phrase)
            self.assertEqual(intent, "humor", f"Failed for '{phrase}'")
            resp = get_conversational_response(intent)
            self.assertTrue(len(resp) > 10)

    def test_history_query_intent(self) -> None:
        intent = classify_conversational_intent("What did we discuss earlier?")
        self.assertEqual(intent, "history_query")
        # Empty history
        resp_empty = get_conversational_response(intent, prior_responses=[])
        self.assertIn("haven't discussed", resp_empty.lower())
        # With prior history
        prior = [{"user_message": {"content": "My refrigerator is not cooling"}}]
        resp_prior = get_conversational_response(intent, prior_responses=prior)
        self.assertIn("refrigerator is not cooling", resp_prior)

    def test_appliance_queries_not_misclassified_as_greetings(self) -> None:
        """Appliance questions with greetings should not bypass appliance support."""
        self.assertIsNone(classify_conversational_intent("Hello my refrigerator is not cooling"))
        self.assertIsNone(classify_conversational_intent("Hi my washer won't drain"))
        self.assertIsNone(classify_conversational_intent("Check my washing machine warranty"))
        self.assertIsNone(classify_conversational_intent("Find a technician for my oven"))

    def test_deterministic_intent_routing_conversational_vs_appliance(self) -> None:
        """General conversation must route to conv_ intents and not appliance tools."""
        i1 = _deterministic_intent("Helloo")
        self.assertIsNotNone(i1)
        self.assertTrue(i1[0].startswith("conv_"), f"Got {i1}")

        i2 = _deterministic_intent("How are you?")
        self.assertIsNotNone(i2)
        self.assertEqual(i2[0], "conv_wellbeing")

        i3 = _deterministic_intent("Tell me a joke")
        self.assertIsNotNone(i3)
        self.assertEqual(i3[0], "conv_humor")

        # Appliance query should route to appliance tool
        i4 = _deterministic_intent("My refrigerator is not cooling")
        self.assertIsNotNone(i4)
        self.assertEqual(i4[0], "get_appliance_inventory")


class TestMissingModelSynthesis(unittest.TestCase):
    """Verify evidence-aware synthesis for missing or unverified appliance models."""

    def test_synthesis_structure_and_categories(self) -> None:
        synth = synthesize_missing_model_guidance(
            model_id="UNKNOWN-FRIDGE-999",
            symptom="not cooling",
            appliance_type="refrigerator",
            brand="Samsung",
        )
        self.assertEqual(synth["status"], "model_unverified")
        self.assertFalse(synth["exact_model_match"])
        self.assertFalse(synth["document_found"])
        self.assertEqual(synth["evidence_category"], GENERIC_APPLIANCE_KNOWLEDGE)
        self.assertTrue(len(synth["safe_steps"]) >= 2)
        self.assertTrue(len(synth["stop_conditions"]) >= 1)
        # Explains uncertainty plainly
        self.assertIn("couldn't locate an exact-model manual", synth["explanation"])
        self.assertIn("rating plate", synth["explanation"])
        # Stop conditions mention safety
        self.assertTrue(any("smoke" in s.lower() or "hazard" in s.lower() for s in synth["stop_conditions"]))


class TestSyntheticDatasetIntegrity(unittest.TestCase):
    """Verify at least 100 distinct appliance ownership records."""

    def test_dataset_size_and_uniqueness(self) -> None:
        self.assertGreaterEqual(len(_APPLIANCES), 100, "Dataset must have at least 100 appliance records")
        self.assertGreaterEqual(len(_WARRANTIES), 100, "Dataset must have at least 100 warranty records")

        # Stable unique IDs
        app_ids = list(_APPLIANCES.keys())
        self.assertEqual(len(app_ids), len(set(app_ids)), "Duplicate appliance IDs found!")

        # Check required fields
        categories = set()
        brands = set()
        for aid, rec in _APPLIANCES.items():
            self.assertIn("appliance_id", rec)
            self.assertIn("brand", rec)
            self.assertIn("model_number", rec)
            self.assertIn("appliance_type", rec)
            self.assertIn("purchase_date", rec)
            self.assertIn("ownership_status", rec)
            self.assertTrue(rec.get("is_synthetic", False) or rec.get("is_simulated", False))
            categories.add(rec["appliance_type"])
            brands.add(rec["brand"])

        # Check category diversity (refrigerators, washers, dryers, dishwashers, ac, ovens, etc.)
        self.assertGreaterEqual(len(categories), 8, f"Insufficient category diversity: {categories}")
        self.assertGreaterEqual(len(brands), 10, f"Insufficient brand diversity: {brands}")

    def test_mcp_inventory_and_catalogue_distinction(self) -> None:
        """Verify get_appliance_inventory queries ownership, find_product queries catalogue."""
        inv = get_appliance_inventory(appliance_type="refrigerator")
        self.assertEqual(inv["status"], "success")
        self.assertGreater(len(inv["records"]), 0)

        cat = find_product(query="LG")
        self.assertEqual(cat["status"], "success")
        self.assertGreater(len(cat["matches"]), 0)

    def test_troubleshooting_category_matching(self) -> None:
        """Verify category-level guidance is returned from expanded records."""
        # Dishwasher
        dw = get_troubleshooting_steps(model_id="MDB-GENERIC", symptom="dishwasher not cleaning dishes")
        self.assertEqual(dw["status"], "success")
        self.assertTrue(dw["document_found"])
        self.assertFalse(dw["exact_model_match"])
        self.assertTrue(len(dw["safe_steps"]) > 0)

        # Oven
        oven = get_troubleshooting_steps(model_id="OVEN-GENERIC", symptom="oven not heating evenly")
        self.assertEqual(oven["status"], "success")
        self.assertTrue(oven["document_found"])


class TestBrandingRegression(unittest.TestCase):
    """Ensure no unintended user-facing HomeCare Concierge branding remains."""

    def test_runtime_system_prompt_identity(self) -> None:
        """Ensure the runtime system prompt establishes Alexa identity."""
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        content = main_py.read_text(encoding="utf-8")
        self.assertIn("You are Alexa", content)
        self.assertNotIn("You are HomeCare Concierge", content)


if __name__ == "__main__":
    unittest.main()
