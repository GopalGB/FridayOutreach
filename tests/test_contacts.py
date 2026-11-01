import hashlib
import json
import socket
import unittest
import warnings
from unittest.mock import patch

from bs4 import MarkupResemblesLocatorWarning

from lead_collector.extract import parse_contacts
from lead_collector.model import PageDocument


SOURCE = "https://example.com/directory"
OBSERVED_AT = "2026-10-31T00:00:00Z"


def document(status: str, html: str = "", error: dict[str, str | None] | None = None) -> PageDocument:
    return PageDocument(SOURCE, html, OBSERVED_AT, hashlib.sha256(html.encode()).hexdigest(), status, error)


class ContactExtractionTests(unittest.TestCase):
    def test_rejects_recursively_nested_json_ld_without_raising(self):
        payload = "[" * 1500 + '{"email":"deep@company.co"}' + "]" * 1500

        result = parse_contacts(document("completed", f"<script type='application/ld+json'>{payload}</script>"))

        self.assertEqual(result.emails, ())

    def test_hidden_cloudflare_and_escaped_mailto_contacts_are_not_observed(self):
        encoded = "127b7c6677607c776652717d7f62737c6b3c717d"
        payload = r'{"markup":"\u003Cspan hidden\u003E\u003Ca href=\"mailto:hidden@company.co\"\u003EInfo\u003C/a\u003E\u003C/span\u003E"}'
        html = f"<span hidden data-cfemail='{encoded}'>protected</span><script type='application/json'>{payload}</script>"

        result = parse_contacts(document("completed", html))

        self.assertEqual(result.emails, ())

    def test_plain_url_in_application_json_is_not_parsed_as_markup_or_warned(self):
        payload = json.dumps({"url": "https://example.com/contact"})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = parse_contacts(document("completed", f"<script type='application/json'>{payload}</script>"))

        self.assertEqual(result.emails, ())
        self.assertFalse(any(isinstance(item.message, MarkupResemblesLocatorWarning) for item in caught))

    def test_json_string_leaves_are_bounded_before_late_email_field(self):
        body = {f"x{index}": "x" for index in range(4096)}
        body["email"] = "late@company.co"

        result = parse_contacts(document("completed", f"<script type='application/ld+json'>{json.dumps(body)}</script>"))

        self.assertEqual(result.emails, ())

    def test_extracts_bounded_json_ld_email_fields_with_document_provenance(self):
        structured = json.dumps(
            {"@context": "https://schema.org", "@type": "Organization", "email": "slero@company.co"}
        )

        result = parse_contacts(document("completed", f"<script type='application/ld+json'>{structured}</script>"))

        self.assertEqual([item.normalized for item in result.emails], ["slero@company.co"])
        self.assertEqual(result.emails[0].source_url, SOURCE)
        self.assertEqual(result.emails[0].observed_at, OBSERVED_AT)

    def test_extracts_explicit_next_flight_email_and_mailto_properties(self):
        flight = json.dumps('1:{"email":"brian@company.co","href":"mailto:team@company.co","trackingLocation":"contact-brian"}')
        page = f"<script>self.__next_f.push([1,{flight}])</script>"

        result = parse_contacts(document("completed", page))

        self.assertEqual([item.normalized for item in result.emails], ["brian@company.co", "team@company.co"])

    def test_extracts_escaped_mailto_markup_only_from_application_json_payloads(self):
        payload = r'{"markup":"\u003Ca href=\"mailto:info@company.co\"\u003EInfo\u003C/a\u003E"}'

        result = parse_contacts(document("completed", f"<script type='application/json'>{payload}</script>"))

        self.assertEqual([item.normalized for item in result.emails], ["info@company.co"])

    def test_decodes_cloudflare_published_email_attribute_without_executing_script(self):
        encoded = "127b7c6677607c776652717d7f62737c6b3c717d"

        result = parse_contacts(document("completed", f"<span data-cfemail='{encoded}'>protected</span>"))

        self.assertEqual([item.normalized for item in result.emails], ["internet@company.co"])

    def test_ignores_reserved_or_generic_script_text_and_deep_structured_payloads(self):
        deep: object = {"email": "deep@company.co"}
        for _ in range(20):
            deep = [deep]
        ld_json = json.dumps({"email": "user@example.com"})
        application_json = json.dumps({"copy": "user@example.com 32hex@sentry.io flags@2x.png"})
        deep_json = json.dumps(deep)
        html = (
            "<script>const copied = 'user@example.com 32hex@sentry.io';</script>"
            f"<script type='application/ld+json'>{ld_json}</script>"
            f"<script type='application/json'>{application_json}</script>"
            f"<script type='application/ld+json'>{deep_json}</script>"
        )

        result = parse_contacts(document("completed", html))

        self.assertEqual(result.emails, ())

    def test_extracts_published_contacts_with_per_value_source(self):
        page = document(
            "completed",
            """
            <a href='mailto:Sales@Example.com'>Email us</a>
            <a href='tel:+1 (212) 555-0100'>Call</a>
            <p>Support: support@example.com, +44 20 7946 0958</p>
            <a href='https://www.linkedin.com/company/example'>LinkedIn</a>
            """,
        )

        result = parse_contacts(page)

        self.assertEqual(result.status, "completed")
        self.assertEqual({item.normalized for item in result.emails}, {"sales@example.com", "support@example.com"})
        self.assertEqual({item.normalized for item in result.phones}, {"+12125550100", "+442079460958"})
        self.assertEqual(result.socials[0].normalized, "https://www.linkedin.com/company/example")
        for item in (*result.emails, *result.phones, *result.socials):
            self.assertEqual(item.source_url, SOURCE)
            self.assertEqual(item.observed_at, OBSERVED_AT)

    def test_does_not_guess_or_claim_deliverability(self):
        page = document(
            "completed",
            """
            <p>Write sales [at] example [dot] com. ISBN 978-1-4028-9462-6.</p>
            <p>Order number 2025550104. No phone is published.</p>
            <p>Price: 123456.78 USD.</p><p>Postal code 12345678. Example Lane.</p>
            <p>Price 2024-10-31. Address 123-456-789.</p>
            """,
        )

        result = parse_contacts(page)

        self.assertEqual(result.emails, ())
        self.assertEqual(result.phones, ())
        self.assertNotIn("deliverable", result.to_json())

    def test_candidates_are_same_host_contact_or_about_only(self):
        page = document(
            "completed",
            """
            <a href='/about'>About</a><a href='/contact'>Contact</a>
            <a href='https://example.com/team'>Team</a>
            <a href='https://outside.example/contact'>External contact</a>
            <a href='https://linkedin.com/company/example'>LinkedIn</a>
            <a href='mailto:help@example.com'>Mail</a><a href='javascript:alert(1)'>Script</a>
            """,
        )

        result = parse_contacts(page)

        self.assertEqual(result.candidates, ("https://example.com/about", "https://example.com/contact", "https://example.com/team"))

    def test_empty_denied_malformed_and_timeout_remain_distinct(self):
        for status in ("empty", "denied", "malformed", "timeout"):
            with self.subTest(status=status):
                error = None if status == "empty" else {"code": "FETCH_FAILED", "message": "safe", "url": SOURCE}
                result = parse_contacts(document(status, "<a href='mailto:ignored@example.com'>ignored</a>", error))
                self.assertEqual(result.status, status)
                self.assertEqual(result.emails, ())
                self.assertEqual(result.error, error)

    def test_never_visits_social_links(self):
        page = document("completed", "<a href='https://www.instagram.com/example'>Instagram</a>")

        with patch.object(socket, "socket", side_effect=AssertionError("contact parser must stay offline")):
            result = parse_contacts(page)

        self.assertEqual(result.socials[0].normalized, "https://www.instagram.com/example")


if __name__ == "__main__":
    unittest.main()
