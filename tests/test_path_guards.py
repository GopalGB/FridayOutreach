import unittest
from pathlib import Path

from lead_collector.cli import _unsafe_name
from lead_collector.exports import _unsafe_component


class PathGuardTests(unittest.TestCase):
    def test_protected_prefixes_and_directories_are_rejected(self) -> None:
        for value in (".env", ".env.local", ".git/config", ".ssh/known_hosts", ".aws/config", ".codex/config.toml", ".agents/skills/example.html"):
            with self.subTest(value=value):
                self.assertTrue(_unsafe_name(Path(value)))

    def test_protected_suffixes_and_secret_names_are_rejected(self) -> None:
        for value in ("settings.key", "certificate.pem", "service-credentials.json", "refresh-token.txt", "id_rsa"):
            with self.subTest(value=value):
                self.assertTrue(_unsafe_component(value))

    def test_protection_is_case_insensitive(self) -> None:
        for value in (".ENV", "Config.KEY", "PRIVATE.PEM", "Client-CREDENTIALS.json", ".SSH/known_hosts"):
            with self.subTest(value=value):
                self.assertTrue(_unsafe_name(Path(value)))

    def test_ordinary_public_names_are_allowed(self) -> None:
        for value in ("leads.csv", "report.json", "exports/review.html", "notes.txt", ".github/workflows/public.html", ".gitignore"):
            with self.subTest(value=value):
                self.assertFalse(_unsafe_name(Path(value)))
                self.assertFalse(_unsafe_component(value))


if __name__ == "__main__":
    unittest.main()
