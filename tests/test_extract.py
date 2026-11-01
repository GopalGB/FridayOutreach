import hashlib
import json
from pathlib import Path
import unittest

from lead_collector.extract import extract_rows, parse_contacts, preview_document
from lead_collector.model import PageDocument, RecipeV1, Selection, canonical


FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = "https://directory.example.test/list"
MAPPING = {"Name": "company", "Website": "website", "Address": "address"}


def document(html: str) -> PageDocument:
    return PageDocument(SOURCE, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), "completed", None)


class ExtractTests(unittest.TestCase):
    def test_table_fixture_keeps_thirty_source_linked_rows_and_raw_href_evidence(self):
        """Removing a row, its evidence, or its raw href must fail this test."""
        rows = extract_rows(document((FIXTURES / "table_30.html").read_text()), MAPPING)

        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0].company.normalized, "Company 01")
        self.assertEqual(rows[0].website.raw, "/company-01")
        self.assertEqual(rows[0].website.normalized, "https://directory.example.test/company-01")
        self.assertEqual(rows[0].address.normalized, "01 Main Street")
        self.assertEqual(rows[0].generic["Region"][0].source_url, SOURCE)
        self.assertTrue(rows[0].company.observed_at.endswith("Z"))

    def test_card_fixture_keeps_thirty_rows_when_selected_and_mapped(self):
        """Dropping card extraction or field mapping must fail this test."""
        selection = {
            "selector": ".card",
            "columnSelectors": {
                "Name": ".name",
                "Website": ".website",
                "Address": ".address",
                "Region": ".region",
            },
        }
        rows = extract_rows(document((FIXTURES / "cards_30.html").read_text()), MAPPING, selection=selection)

        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[29].company.normalized, "Card Company 30")
        self.assertEqual(rows[29].website.raw, "/card-30")
        self.assertEqual(rows[29].generic["Region"][0].normalized, "North")

    def test_hidden_content_is_not_observed_and_headerless_table_keeps_first_data_row(self):
        """Extracting hidden data or dropping a headerless first row must fail this test."""
        hidden = """
        <table><tr><td>Visible One</td><td>One Road</td></tr>
        <tr hidden><td>Hidden One</td><td>Secret Road</td></tr>
        <tr style='display: none'><td>Hidden Two</td><td>Secret Road</td></tr></table>
        """
        rows = extract_rows(document(hidden), {"column1": "company", "column2": "address"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].company.normalized, "Visible One")
        self.assertEqual(rows[0].address.normalized, "One Road")

    def test_exact_repeats_deduplicate_but_same_domain_distinct_addresses_remain(self):
        """Replacing observation merge with a domain merge must fail this test."""
        html = """
        <table><tr><th>Name</th><th>Website</th><th>Address</th></tr>
        <tr><td>Acme</td><td><a href='https://acme.example'>A</a></td><td>1 Main</td></tr>
        <tr><td>Acme</td><td><a href='https://acme.example'>A</a></td><td>1 Main</td></tr>
        <tr><td>Acme</td><td><a href='https://acme.example'>A</a></td><td>2 Main</td></tr></table>
        """
        rows = extract_rows(document(html), MAPPING)

        self.assertEqual(len(rows), 2)
        self.assertEqual({row.address.normalized for row in rows}, {"1 Main", "2 Main"})

    def test_escaped_entity_text_is_decoded_as_observed_evidence(self):
        """Treating HTML entities as literal text would corrupt evidence and stable identities."""
        html = "<table><tr><th>Name</th></tr><tr><td>Gr&uuml;n &amp; Co</td></tr></table>"

        rows = extract_rows(document(html), {"Name": "company"})

        self.assertEqual(rows[0].company.raw, "Grün & Co")
        self.assertEqual(rows[0].company.normalized, "Grün & Co")

    def test_contact_markup_keeps_link_observations_before_visible_text(self):
        html = "<p>text@example.test Phone: +1 202-555-0110</p><a href='mailto:link@example.test'>Email</a><a href='tel:+12025550111'>Call</a>"

        contacts = parse_contacts(document(html))

        self.assertEqual([item.raw for item in contacts.emails], ["link@example.test", "text@example.test"])
        self.assertEqual([item.normalized for item in contacts.phones], ["+12025550111", "+12025550110"])

    def test_card_selection_keeps_row_when_an_optional_column_is_absent(self):
        """A missing optional card field must not make ordinary lead-list extraction crash."""
        selection = Selection(
            ".card",
            "",
            ("Name", "Site"),
            (),
            {"Name": "h2", "Site": ".website"},
        )

        rows = extract_rows(document("<div class='card'><h2>Acme Example</h2></div>"), {"Name": "company", "Site": "website"}, selection)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].company.normalized, "Acme Example")
        self.assertIsNone(rows[0].website)
        self.assertEqual(rows[0].generic, {})

    def test_card_columns_keep_frozen_order_after_canonical_recipe_round_trip(self):
        """Sorted JSON keys must not change a selected card layout or its fingerprint."""
        html = """
        <div class='card'>
          <h2>Acme Example</h2><a class='website' href='/acme'>Acme</a><span class='price'>$10</span>
        </div>
        """
        initial = Selection(
            ".card",
            "",
            ("Name", "Website", "Price"),
            (),
            {"Name": "h2", "Website": ".website", "Price": ".price"},
        )
        _, frozen = preview_document(document(html), {"Name": "company", "Website": "website"}, initial)
        recipe = RecipeV1(
            SOURCE,
            ("directory.example.test",),
            ("/",),
            frozen,
            {"Name": "company", "Website": "website"},
            {"mode": "none", "selector": None},
            {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        )
        reopened = RecipeV1.from_json(json.loads(canonical(recipe.to_json())))

        rows = extract_rows(document(html), reopened.mapping, reopened.selection)

        self.assertEqual(len(rows), 1)
        self.assertEqual(reopened.selection.columns, ("Name", "Website", "Price"))
        self.assertEqual(rows[0].company.normalized, "Acme Example")
        self.assertEqual(rows[0].website.raw, "/acme")
        self.assertEqual(rows[0].generic["Price"][0].normalized, "$10")


if __name__ == "__main__":
    unittest.main()
