import unittest

from lead_collector.ai import AiCandidate, AiError, AiResult, AiSource, AiSuggestion, AiUsage, validate_ai_mode
from lead_collector.assist import reindex_suggestions
from lead_collector.model import ObservedValue


class AiValueContractTests(unittest.TestCase):
    def test_mode_validation_is_explicit_and_rejects_fallback_names(self):
        self.assertEqual(validate_ai_mode("off"), "off")
        self.assertEqual(validate_ai_mode("scrapegraph"), "scrapegraph")
        self.assertEqual(validate_ai_mode("jev"), "jev")
        self.assertEqual(validate_ai_mode("hybrid"), "hybrid")
        with self.assertRaises(ValueError):
            validate_ai_mode("auto")

    def test_result_serializes_review_only_suggestions_without_source_fragments(self):
        observation = ObservedValue("Acme", "Acme", "https://example.test/", "2026-10-31T00:00:00Z")
        candidate = AiCandidate("c0", "company", observation, "Company: Acme")
        suggestion = AiSuggestion("c0", "company", observation, True, 0.82, {"c0": 0.82, "none": 0.18}, 0.91)
        result = AiResult("completed", (suggestion,), (), "jev-1.14.0", AiUsage(12, 3, "0.000000504"), None, contract_only=True)

        body = result.to_json()

        self.assertEqual(candidate.observation, observation)
        self.assertTrue(body["contractOnly"])
        self.assertEqual(body["accepted"][0]["candidateId"], "c0")
        self.assertNotIn("Company: Acme", str(body))
        self.assertEqual(body["usage"]["estimatedCostUsd"], "0.000000504")

    def test_source_and_error_are_typed_values_for_adapter_boundaries(self):
        source = AiSource("https://example.test/", "2026-10-31T00:00:00Z", "anonymous-http")
        result = AiResult("blocked", (), (), None, None, AiError("COST_POLICY", "metered Jev is disabled"))

        self.assertEqual(source.kind, "anonymous-http")
        self.assertEqual(result.to_json()["error"]["code"], "COST_POLICY")

    def test_local_adapter_suggestions_do_not_invent_provider_probabilities(self):
        observation = ObservedValue("Acme", "Acme", "https://example.test/", "2026-10-31T00:00:00Z")
        suggestion = AiSuggestion("c0", "company", observation, True, None, None, None)

        body = suggestion.to_json()

        self.assertIsNone(body["choiceConfidence"])
        self.assertIsNone(body["probabilities"])
        self.assertIsNone(body["supportProbability"])

    def test_local_suggestion_hashes_are_reindexed_before_a_future_jev_handoff(self):
        observation = ObservedValue("Acme", "Acme", "https://example.test/", "2026-10-31T00:00:00Z")
        local = AiResult(
            "completed",
            (AiSuggestion("a1b2c3d4", "company", observation, True, None, None, None),),
            (AiSuggestion("deadbeef", "email", observation, False, None, None, None),),
            "ollama/qwen3:0.6b",
            None,
            None,
        )

        reindexed = reindex_suggestions(local)

        self.assertEqual([item.candidate_id for item in reindexed.accepted], ["c0"])
        self.assertEqual([item.candidate_id for item in reindexed.rejected], ["c1"])


if __name__ == "__main__":
    unittest.main()
