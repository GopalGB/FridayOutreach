import unittest
import math

import lead_collector.model as model


class ModelContractTests(unittest.TestCase):
    def test_json_decoder_rejects_nonfinite_nested_and_nonstring_shapes(self):
        """Persisted contract data must stay representable by strict JSON values."""
        for value in (math.inf, math.nan, {1: "value"}, {"nested": {"bad": object()}}, ("tuple",)):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    model.json_value(value)

    def test_selection_rejects_invalid_selector_columns_samples_and_mapping(self):
        cases = (
            {"selector": 1, "fingerprint": "x", "columns": [], "sampleRows": [], "columnSelectors": {}},
            {"selector": ".card", "fingerprint": 1, "columns": [], "sampleRows": [], "columnSelectors": {}},
            {"selector": ".card", "fingerprint": "x", "columns": [1], "sampleRows": [], "columnSelectors": {}},
            {"selector": ".card", "fingerprint": "x", "columns": [], "sampleRows": [[1]], "columnSelectors": {}},
            {"selector": ".card", "fingerprint": "x", "columns": [], "sampleRows": [], "columnSelectors": {"Name": 1}},
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    model.Selection.from_json(value)
    def test_list_observation_maps_only_declared_fields_and_preserves_unmapped_generic(self):
        """Dropping generic source fields during mapping must fail this test."""
        self.assertTrue(hasattr(model, "ListObservation"))
        self.assertTrue(hasattr(model, "map_observation"))
        observation = model.ListObservation(
            source_url="https://directory.example.test/list",
            observed_at="2026-10-31T00:00:00Z",
            fields={"Name": (" Acme ",), "Region": ("West",)},
        )

        row = model.map_observation(observation, {"Name": "company"})

        self.assertEqual(row.company.normalized, "Acme")
        self.assertEqual(row.generic["Region"][0].source_url, "https://directory.example.test/list")
        self.assertEqual(row.generic["Region"][0].observed_at, "2026-10-31T00:00:00Z")

    def test_job_snapshot_from_json_rebuilds_recipe_and_evidence(self):
        """Dropping nested recipe or evidence fields during checkpoint decoding must fail this test."""
        snapshot = model.JobSnapshot.from_json(
            {
                "id": "job-1",
                "revision": 3,
                "generation": 1,
                "state": "completed",
                "progress": {"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1},
                "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
                "rows": [{"id": "lead-1", "company": {"raw": "Acme"}}],
                "errors": [{"code": "CONTENT_TRUNCATED"}],
                "recipe": {
                    "version": 1,
                    "startUrl": "https://directory.example.test/list",
                    "allowedHosts": ["directory.example.test"],
                    "allowedPaths": ["/list"],
                    "selection": {"selector": "table", "fingerprint": "shape", "columns": ["Name"], "sampleRows": [["Acme"]], "columnSelectors": {"Name": "th"}},
                    "mapping": {"Name": "company"},
                    "pagination": {"mode": "none", "selector": None},
                    "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
                },
            }
        )

        self.assertEqual(snapshot.recipe.allowed_hosts, ("directory.example.test",))
        self.assertEqual(snapshot.recipe.selection.sample_rows, (("Acme",),))
        self.assertEqual(snapshot.rows, ({"id": "lead-1", "company": {"raw": "Acme"}},))

    def test_recipe_from_json_rejects_boolean_limit(self):
        """Accepting JSON true as a numeric limit would disable bounded collection safeguards."""
        with self.assertRaises(ValueError):
            model.RecipeV1.from_json(
                {
                    "version": 1,
                    "startUrl": "https://directory.example.test/list",
                    "allowedHosts": ["directory.example.test"],
                    "allowedPaths": ["/list"],
                    "selection": None,
                    "mapping": {"Name": "company"},
                    "pagination": {"mode": "none"},
                    "limits": {"maxRows": True},
                }
            )

    def test_recipe_from_json_rejects_tampered_semantic_contract_values(self):
        """Accepting arbitrary recipe semantics would make stored collection bounds unenforceable."""
        base = {
            "version": 1,
            "startUrl": "https://directory.example.test/list",
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/list"],
            "selection": None,
            "mapping": {"Name": "company"},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        invalid_values = (
            {**base, "version": 2},
            {**base, "mapping": {"Name": "lead"}},
            {**base, "pagination": {"mode": "cursor", "selector": None}},
            {**base, "limits": {"maxInputs": 0, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100}},
            {**base, "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1001, "maxPages": 100}},
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    model.RecipeV1.from_json(value)

    def test_snapshot_from_json_rejects_invalid_state_counter_progress_and_row_status(self):
        """A syntactically valid tampered checkpoint must still be rejected at the model boundary."""
        base = {
            "id": "job-1",
            "revision": 1,
            "generation": 0,
            "state": "completed",
            "progress": {"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
            "rows": [{"id": "lead-1", "status": "pending"}],
            "errors": [],
            "recipe": None,
        }
        invalid_values = (
            {**base, "state": "unknown"},
            {**base, "revision": -1},
            {**base, "generation": -1},
            {**base, "progress": {"attemptedPages": 1, "completedInputs": 1}},
            {**base, "progress": {"attemptedPages": -1, "completedInputs": 1, "totalInputs": 1}},
            {**base, "rows": [{"id": "lead-1", "status": "unknown"}]},
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    model.JobSnapshot.from_json(value)

    def test_snapshot_from_json_accepts_every_frozen_state_and_row_status(self):
        """Rejecting a frozen lifecycle value would make a valid persisted job unrecoverable."""
        base = {
            "id": "job-1",
            "revision": 1,
            "generation": 0,
            "state": "completed",
            "progress": {"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
            "rows": [{"id": "lead-1", "status": "pending"}],
            "errors": [],
            "recipe": None,
        }
        states = ("idle", "running", "pausing", "paused", "stopped", "interrupted", "completed", "failed")
        statuses = ("pending", "completed", "empty", "failed", "skipped")

        for state in states:
            with self.subTest(state=state):
                self.assertEqual(model.JobSnapshot.from_json({**base, "state": state}).state, state)
        for status in statuses:
            with self.subTest(status=status):
                snapshot = model.JobSnapshot.from_json({**base, "rows": [{"id": "lead-1", "status": status}]})
                self.assertEqual(snapshot.rows[0]["status"], status)

    def test_snapshot_from_json_rejects_invented_and_nonstring_lifecycle_values(self):
        """Persisting an invented lifecycle state would violate the frozen public contract."""
        base = {
            "id": "job-1",
            "revision": 1,
            "generation": 0,
            "state": "completed",
            "progress": {"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
            "rows": [{"id": "lead-1", "status": "pending"}],
            "errors": [],
            "recipe": None,
        }
        invalid_states = ("collecting", "cancelled", "accepted", "rejected", "excluded", "unknown", 1)
        invalid_statuses = ("collecting", "cancelled", "accepted", "rejected", "excluded", "unknown", 1)

        for state in invalid_states:
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    model.JobSnapshot.from_json({**base, "state": state})
        for status in invalid_statuses:
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    model.JobSnapshot.from_json({**base, "rows": [{"id": "lead-1", "status": status}]})

    def test_recipe_from_json_rejects_a_sixth_page_per_host(self):
        """Allowing a sixth per-host page would violate the frozen contact cap."""
        base = {
            "version": 1,
            "startUrl": "https://directory.example.test/list",
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/list"],
            "selection": None,
            "mapping": {"Name": "company"},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }

        self.assertEqual(model.RecipeV1.from_json(base).limits["maxPagesPerHost"], 5)
        with self.assertRaises(ValueError):
            model.RecipeV1.from_json({**base, "limits": {**base["limits"], "maxPagesPerHost": 6}})

    def test_checkpoint_round_trips_scroll_stability_and_keeps_legacy_checkpoint_readable(self):
        base = {
            "mode": "collect",
            "inputUrls": ["https://directory.example.test/list"],
            "sourceRows": {"input:0": ["https://directory.example.test/list"]},
            "pendingUrls": ["https://directory.example.test/list"],
            "attemptedByHost": {},
            "seenPageHashes": [],
            "cursors": [],
            "renderer": {"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"},
        }

        self.assertEqual(model.JobCheckpoint.from_json(base).unchanged_scrolls, 0)
        checkpoint = model.JobCheckpoint.from_json({**base, "unchangedScrolls": 1})
        self.assertEqual(model.JobCheckpoint.from_json(checkpoint.to_json()).unchanged_scrolls, 1)
        with self.assertRaises(ValueError):
            model.JobCheckpoint.from_json({**base, "unchangedScrolls": -1})

    def test_lead_row_round_trip_preserves_optional_singular_evidence_and_seeds_legacy_payload(self):
        """Omitting or collapsing singular observations would lose durable source evidence."""
        later = model.ObservedValue("Acme", "Acme", "https://later.example.test", "2026-10-31T02:00:00Z")
        earlier = model.ObservedValue("Acme Ltd", "Acme", "https://earlier.example.test", "2026-10-31T01:00:00Z")
        row = model.LeadRow("lead-1", company=earlier, evidence={"company": (earlier, later)})

        round_tripped = model.LeadRow.from_json(row.to_json())
        legacy_payload = row.to_json()
        legacy_payload.pop("evidence")
        legacy = model.LeadRow.from_json(legacy_payload)

        self.assertEqual(round_tripped.evidence["company"], (earlier, later))
        self.assertEqual(legacy.evidence["company"], (earlier,))

    def test_merge_returns_branches_and_selects_earliest_singular_observation_without_losing_edits(self):
        """Discarding a different identity or a prior edit would irreversibly lose user data."""
        later = model.ObservedValue("Acme", "Acme", "https://later.example.test", "2026-10-31T02:00:00Z")
        earlier = model.ObservedValue("Acme Ltd", "Acme", "https://earlier.example.test", "2026-10-31T01:00:00Z")
        existing = model.LeadRow("lead-1", company=later, edits={"company": "User choice"})
        incoming = model.LeadRow("lead-1", company=earlier, edits={"website": "Keep"})
        branch = model.LeadRow("lead-2", company=earlier)

        merged = model.merge_lead_rows(existing, incoming)
        branches = model.merge_lead_rows(existing, branch)

        self.assertEqual(merged[0].company, earlier)
        self.assertEqual({item.source_url for item in merged[0].evidence["company"]}, {"https://later.example.test", "https://earlier.example.test"})
        self.assertEqual(merged[0].edits, {"company": "User choice", "website": "Keep"})
        self.assertEqual([item.id for item in branches], ["lead-1", "lead-2"])


if __name__ == "__main__":
    unittest.main()
