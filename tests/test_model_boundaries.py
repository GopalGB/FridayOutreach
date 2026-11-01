"""Malformed persisted contracts fail at their public decoding boundary."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import unittest

from lead_collector import model


URL = 'https://directory.example.test/list'
HOST = 'directory.example.test'
STAMP = '2026-10-31T00:00:00Z'
HTML = '<main>Fictional Cedar</main>'
DIGEST = hashlib.sha256(HTML.encode()).hexdigest()
LIMITS = {'maxInputs': 100, 'maxPagesPerHost': 5, 'maxRows': 1000, 'maxPages': 100}
ERROR = {'code': 'FETCH_FAILED', 'message': 'fixture failed', 'url': None}
RENDERER = {'kind': 'ego', 'taskSpaceId': 7, 'pageLabel': 'p1'}


def checkpoint():
    return model.JobCheckpoint('collect', (URL,), {'input:0': (URL,)}, (URL,), {HOST: 1}, (DIGEST,), (URL,))


class PersistedBoundaryTests(unittest.TestCase):
    def assert_rejected_changes(self, decoder, baseline, changes):
        self.assertEqual(decoder(deepcopy(baseline)).to_json(), baseline)
        for change in changes:
            with self.subTest(change=change):
                candidate = {**deepcopy(baseline), **deepcopy(change)}
                with self.assertRaises(ValueError):
                    decoder(candidate)

    def test_recipe_containers_and_pagination_cannot_erase_limits(self):
        base = model.RecipeV1(URL, (HOST,), ('/list',), None, {'Name': 'company'}, {'mode': 'none', 'selector': None}, LIMITS).to_json()
        changes = [
            {'mapping': []}, {'mapping': {'Name': 1}}, {'selection': []},
            {'limits': []}, {'limits': {'maxRows': 5}}, {'limits': {**LIMITS, 'maxRows': True}},
            {'pagination': {'mode': 'none', 'selector': '.next'}},
            {'pagination': {'mode': 'next', 'selector': None}},
            {'pagination': {'mode': 'loadMore', 'selector': ''}},
            {'pagination': {'mode': 'scroll', 'selector': 1}},
        ]
        self.assert_rejected_changes(model.RecipeV1.from_json, base, changes)
        value = model.RecipeV1.from_json({**base, 'pagination': {'mode': 'scroll', 'selector': '.list'}})
        self.assertEqual(value.pagination['selector'], '.list')
        with self.assertRaises(ValueError):
            model.RecipeV1.from_json({key: value for key, value in base.items() if key != 'mapping'})

    def test_selection_requires_arrays_and_string_selector_maps(self):
        base = model.Selection('.card', '', ('Name',), (('Cedar',),), {'Name': '.name'}).to_json()
        self.assert_rejected_changes(model.Selection.from_json, base, [
            {'columns': {}}, {'sampleRows': {}}, {'sampleRows': [False]}, {'columnSelectors': []},
        ])
        with self.assertRaises(ValueError):
            model.json_object([])

    def test_fetch_request_requires_complete_bounded_shape(self):
        base = model.FetchRequest(URL, (HOST,), ('/list',), LIMITS).to_json()
        self.assert_rejected_changes(model.FetchRequest.from_json, base, [
            {'extra': True}, {'url': ''}, {'allowedHosts': []}, {'allowedHosts': {}},
            {'allowedPaths': []}, {'allowedPaths': [None]}, {'limits': False},
        ])
        with self.assertRaises(ValueError):
            model.FetchRequest.from_json({key: value for key, value in base.items() if key != 'url'})

    def test_document_outcomes_cannot_disguise_failure_as_success(self):
        base = model.PageDocument(URL, HTML, STAMP, DIGEST, 'completed', None).to_json()
        self.assert_rejected_changes(model.PageDocument.from_json, base, [
            {'extra': 1}, {'url': ''}, {'observedAt': ''}, {'sha256': 'g' * 64},
            {'sha256': ''}, {'status': 'invented'}, {'error': ERROR},
            {'status': 'failed'}, {'error': []}, {'error': {}},
            {'error': {**ERROR, 'code': ''}}, {'error': {**ERROR, 'message': ''}},
            {'error': {**ERROR, 'url': 42}},
        ])
        failed = {**base, 'status': 'failed', 'error': ERROR, 'html': ''}
        self.assertEqual(model.PageDocument.from_json(failed).to_json(), failed)
        with self.assertRaises(ValueError):
            model.PageDocument.from_json({key: value for key, value in base.items() if key != 'error'})

    def test_contact_outcomes_and_observations_remain_well_typed(self):
        base = model.ContactResult('completed', (), (), (), (), None).to_json()
        self.assert_rejected_changes(model.ContactResult.from_json, base, [
            {'extra': 1}, {'status': 'unknown'}, {'error': ERROR},
            {'status': 'denied'}, {'emails': None}, {'phones': {}},
            {'socials': [None]}, {'candidates': {}},
        ])
        for status in ('empty', 'denied'):
            body = {**base, 'status': status, 'error': None if status == 'empty' else ERROR}
            self.assertEqual(model.ContactResult.from_json(body).to_json(), body)

    def test_lead_import_cannot_change_evidence_or_exclusion_types(self):
        observation = model.ObservedValue('Cedar', 'Cedar', URL, STAMP)
        base = model.LeadRow('row-1', company=observation).to_json()
        self.assert_rejected_changes(model.LeadRow.from_json, base, [
            {'excluded': 1}, {'status': 'unknown'}, {'evidence': {'invented': [observation.to_json()]}},
            {'evidence': []}, {'generic': []}, {'edits': []}, {'emails': {}},
            {'generic': {'Region': None}}, {'generic': {'Region': [None]}},
            {'company': 'not an observation'}, {'company': {}}, {'id': ''},
        ])
        self.assertEqual(model.LeadRow.from_json(base).company, observation)

    def test_checkpoint_rejects_invalid_queue_owners_hashes_and_cursors(self):
        base = checkpoint().to_json()
        self.assert_rejected_changes(model.JobCheckpoint.from_json, base, [
            {'extra': 1}, {'mode': 'unknown'}, {'inputUrls': []}, {'inputUrls': ['']},
            {'sourceRows': []}, {'sourceRows': {'': [URL]}}, {'sourceRows': {'owner': []}},
            {'sourceRows': {'owner': ['']}}, {'pendingUrls': ['']},
            {'attemptedByHost': {'': 1}}, {'attemptedByHost': {HOST: -1}},
            {'seenPageHashes': ['g' * 64]}, {'cursors': ['']}, {'unchangedScrolls': -1},
            {'mode': 'contacts', 'inputUrls': [URL] * 101},
        ])
        with self.assertRaises(ValueError):
            model.JobCheckpoint.from_json({key: value for key, value in base.items() if key != 'cursors'})
        with self.assertRaises(ValueError):
            replace(checkpoint(), unchanged_scrolls=-1).to_json()

    def test_checkpoint_renderer_cannot_grant_invalid_browser_ownership(self):
        base = checkpoint().to_json()
        valid = {**base, 'renderer': RENDERER}
        self.assertEqual(model.JobCheckpoint.from_json(valid).to_json(), valid)
        invalid = [
            [], {}, {**RENDERER, 'extra': True}, {**RENDERER, 'kind': 'other'},
            {**RENDERER, 'taskSpaceId': True}, {**RENDERER, 'taskSpaceId': 0},
            {**RENDERER, 'pageLabel': ' '}, {**RENDERER, 'pageLabel': 0},
        ]
        self.assert_rejected_changes(model.JobCheckpoint.from_json, base, [{'renderer': value} for value in invalid])
        self.assert_rejected_changes(model.JobCheckpoint.from_json, base, [{'mode': 'contacts', 'renderer': RENDERER}])

    def test_snapshot_rejects_wrong_row_error_counter_and_nested_shapes(self):
        base = model.JobSnapshot('job-1').to_json()
        self.assert_rejected_changes(model.JobSnapshot.from_json, base, [
            {'rows': {}}, {'rows': [None]}, {'errors': {}}, {'errors': [None]},
            {'progress': []}, {'progress': {'attemptedPages': True, 'completedInputs': 0, 'totalInputs': 1}},
            {'checkpoint': []}, {'recipe': []},
        ])


if __name__ == '__main__':
    unittest.main()
