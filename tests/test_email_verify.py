import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, cast
import unittest
from unittest.mock import patch

from lead_collector.email_verify import (
    EmailVerifyRequest,
    VerificationDetails,
    VerificationError,
    _child_environment,
    _default_binary,
    _parse_result,
    verify_email,
)


EMAIL = "hello@example.test"


def native_response(**changes: object) -> bytes:
    body: dict[str, object] = {
        "version": 1,
        "input": EMAIL,
        "status": "safe",
        "syntaxValid": True,
        "mxAcceptsMail": True,
        "smtpDeliverable": True,
        "catchAll": False,
        "disabled": False,
        "fullInbox": False,
    }
    body.update(changes)
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


class FakeProcess:
    def __init__(self, *, returncode: int = 0) -> None:
        self.stdout = None
        self.returncode = returncode
        self.pid = 31337

    def wait(self, *, timeout: float | None = None) -> int:
        return self.returncode


class EmailVerifyTests(unittest.TestCase):
    def test_accepts_rfc_json_whitespace_from_native_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("binary")
            binary.chmod(0o700)
            with patch("lead_collector.email_verify.subprocess.Popen", return_value=FakeProcess()), patch(
                "lead_collector.email_verify._read_output", return_value=native_response() + b"\n"
            ):
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(result.status, "safe")
        self.assertTrue(result.mailbox_verified)
        self.assertIsNone(result.error)

    def test_missing_or_wrong_type_native_fields_are_sanitized(self) -> None:
        keys = ("version", "input", "status", "syntaxValid", "mxAcceptsMail", "smtpDeliverable", "catchAll", "disabled", "fullInbox")
        for key in keys:
            with self.subTest(missing=key), tempfile.TemporaryDirectory() as temp_dir:
                binary = Path(temp_dir) / "reacher-smtp-check"
                binary.write_text("binary")
                binary.chmod(0o700)
                malformed = json.loads(native_response())
                del malformed[key]
                with patch("lead_collector.email_verify.subprocess.Popen", return_value=FakeProcess()), patch(
                    "lead_collector.email_verify._read_output", return_value=json.dumps(malformed).encode("utf-8")
                ):
                    result = verify_email(EmailVerifyRequest(EMAIL, binary))
                self.assertEqual(result.error, VerificationError("VERIFY_OUTPUT_INVALID", "native verifier response was invalid"))
        for key, value in (("version", True), ("input", 1), ("status", 1), ("syntaxValid", 1)):
            with self.subTest(wrong_type=key), tempfile.TemporaryDirectory() as temp_dir:
                binary = Path(temp_dir) / "reacher-smtp-check"
                binary.write_text("binary")
                binary.chmod(0o700)
                with patch("lead_collector.email_verify.subprocess.Popen", return_value=FakeProcess()), patch(
                    "lead_collector.email_verify._read_output", return_value=native_response(**{key: value})
                ):
                    result = verify_email(EmailVerifyRequest(EMAIL, binary))
                self.assertEqual(result.error, VerificationError("VERIFY_OUTPUT_INVALID", "native verifier response was invalid"))

    def test_actual_flooding_child_is_killed_when_output_exceeds_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("#!/bin/sh\nhead -c 65536 /dev/zero\nsleep 5\n")
            binary.chmod(0o700)
            processes: list[subprocess.Popen[bytes]] = []
            real_popen = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)

            def retain_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process

            with patch("lead_collector.email_verify.subprocess.Popen", side_effect=retain_popen):
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(len(processes), 1)
        self.assertLess(processes[0].returncode, 0)
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.mailbox_verified)
        self.assertEqual(result.error, VerificationError("VERIFY_OUTPUT_INVALID", "native verifier response was invalid"))

    def test_safe_native_schema_marks_only_deliverable_mailbox_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("binary")
            binary.chmod(0o700)
            with patch("lead_collector.email_verify.subprocess.Popen", return_value=FakeProcess()), patch(
                "lead_collector.email_verify._read_output", return_value=native_response()
            ):
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(result.status, "safe")
        self.assertTrue(result.mailbox_verified)
        self.assertEqual(result.details, VerificationDetails(True, True, True, False, False, False))
        self.assertIsNone(result.error)

    def test_non_boolean_native_schema_is_an_unknown_unverified_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("binary")
            binary.chmod(0o700)
            with patch("lead_collector.email_verify.subprocess.Popen", return_value=FakeProcess()), patch(
                "lead_collector.email_verify._read_output", return_value=native_response(syntaxValid=1)
            ):
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.mailbox_verified)
        self.assertEqual(result.error, VerificationError("VERIFY_OUTPUT_INVALID", "native verifier response was invalid"))

    def test_native_mx_failure_category_is_a_sanitized_named_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("binary")
            binary.chmod(0o700)
            response = native_response(status="unknown", mxAcceptsMail=False, smtpDeliverable=False, failureCategory="mx")
            with patch("lead_collector.email_verify.subprocess.Popen", return_value=FakeProcess()), patch(
                "lead_collector.email_verify._read_output", return_value=response
            ):
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.mailbox_verified)
        self.assertEqual(result.error, VerificationError("VERIFY_MX_FAILED", "native MX resolution failed"))

    def test_invalid_results_without_a_failure_category_remain_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("binary")
            binary.chmod(0o700)
            response = native_response(status="invalid", mxAcceptsMail=False, smtpDeliverable=False)
            with patch("lead_collector.email_verify.subprocess.Popen", return_value=FakeProcess()), patch(
                "lead_collector.email_verify._read_output", return_value=response
            ):
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(result.status, "invalid")
        self.assertFalse(result.mailbox_verified)
        self.assertIsNone(result.error)

    def test_deeply_nested_malformed_output_is_sanitized(self) -> None:
        output = b'{"extra":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}'

        result = _parse_result(output, EMAIL)

        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.mailbox_verified)
        self.assertEqual(result.error, VerificationError("VERIFY_OUTPUT_INVALID", "native verifier response was invalid"))

    def test_returned_address_must_exactly_match_requested_address(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("binary")
            binary.chmod(0o700)
            with patch("lead_collector.email_verify.subprocess.Popen", return_value=FakeProcess()), patch(
                "lead_collector.email_verify._read_output", return_value=native_response(input="other@example.test")
            ):
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.mailbox_verified)
        self.assertEqual(result.error, VerificationError("VERIFY_OUTPUT_INVALID", "native verifier response was invalid"))

    def test_contradictory_safe_catch_all_result_is_never_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("binary")
            binary.chmod(0o700)
            with patch("lead_collector.email_verify.subprocess.Popen", return_value=FakeProcess()), patch(
                "lead_collector.email_verify._read_output", return_value=native_response(catchAll=True)
            ):
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.mailbox_verified)
        self.assertEqual(result.error, VerificationError("VERIFY_OUTPUT_INVALID", "native verifier response was invalid"))

    def test_timeout_terminates_only_the_owned_process_group(self) -> None:
        process = FakeProcess()
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("binary")
            binary.chmod(0o700)
            with patch("lead_collector.email_verify.subprocess.Popen", return_value=process), patch(
                "lead_collector.email_verify._read_output", side_effect=TimeoutError
            ), patch("lead_collector.email_verify.os.killpg") as killpg:
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.mailbox_verified)
        self.assertEqual(result.error, VerificationError("VERIFY_TIMEOUT", "native verifier exceeded its deadline"))
        killpg.assert_called_once()

    def test_child_environment_is_minimal_and_drops_proxy_and_provider_values(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with patch.dict("lead_collector.email_verify.os.environ", {"HTTPS_PROXY": "bad", "API_KEY": "bad"}, clear=True):
                environment = _child_environment(home)

        self.assertEqual(environment["HOME"], home)
        self.assertEqual(environment["TMPDIR"], home)
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertNotIn("API_KEY", environment)
        self.assertEqual(set(environment), {"HOME", "TMPDIR", "PATH", "LANG", "NO_COLOR"})

    def test_cost_independent_mode_passes_only_the_requested_address_to_native_binary(self) -> None:
        process = FakeProcess()
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text("binary")
            binary.chmod(0o700)
            with patch("lead_collector.email_verify.subprocess.Popen", return_value=process) as popen, patch(
                "lead_collector.email_verify._read_output", return_value=native_response(status="unknown", smtpDeliverable=False)
            ):
                result = verify_email(EmailVerifyRequest(EMAIL, binary))

        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.mailbox_verified)
        command = popen.call_args.args[0]
        self.assertEqual(command, [str(binary), "--email", EMAIL])
        self.assertNotIn("--proxy-host", command)
        self.assertNotIn("--check-gravatar", command)
        self.assertNotIn("--haveibeenpwned-api-key", command)

    def test_missing_binary_is_a_named_capability_error(self) -> None:
        result = verify_email(EmailVerifyRequest(EMAIL, Path("/missing/reacher-smtp-check")))

        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.mailbox_verified)
        self.assertEqual(result.error, VerificationError("CAPABILITY_UNAVAILABLE", "SMTP verifier runtime is unavailable"))

    def test_invalid_or_untrusted_binary_never_spawns_a_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.write_text("binary", encoding="utf-8")
            source.chmod(0o700)
            linked = root / "linked"
            linked.symlink_to(source)
            hard_linked = root / "hard-linked"
            os.link(source, hard_linked)
            writable = root / "writable"
            writable.write_text("binary", encoding="utf-8")
            writable.chmod(0o722)
            non_executable = root / "non-executable"
            non_executable.write_text("binary", encoding="utf-8")
            non_executable.chmod(0o600)
            with patch("lead_collector.email_verify.subprocess.Popen") as spawn:
                invalid = verify_email(EmailVerifyRequest("not-an-email", source))
                results = [verify_email(EmailVerifyRequest(EMAIL, path)) for path in (linked, hard_linked, writable, non_executable)]

        self.assertEqual(invalid.error, VerificationError("INPUT_INVALID", "email address is invalid"))
        self.assertTrue(all(result.error == VerificationError("CAPABILITY_UNAVAILABLE", "SMTP verifier runtime is unavailable") for result in results))
        spawn.assert_not_called()

    def test_default_binary_stays_outside_the_packaged_scripts_tree(self) -> None:
        self.assertEqual(_default_binary(), Path(__file__).parents[1] / ".tools" / "reacher-smtp-check")

    def test_relative_binary_stays_bound_when_the_child_uses_a_private_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = Path.cwd()
            binary = Path(temp_dir) / "reacher-smtp-check"
            binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{native_response().decode()}'\n", encoding="utf-8")
            binary.chmod(0o700)
            try:
                os.chdir(temp_dir)
                result = verify_email(EmailVerifyRequest(EMAIL, Path("reacher-smtp-check")))
            finally:
                os.chdir(original_cwd)

        self.assertEqual(result.status, "safe")
        self.assertTrue(result.mailbox_verified)

    def test_native_runner_explicitly_forces_smtp_for_all_provider_variants(self) -> None:
        source = (Path(__file__).parents[1] / "scripts" / "reacher_smtp_runner" / "src" / "main.rs").read_text(encoding="utf-8")

        for variant in (
            "GmailVerifMethod::Smtp",
            "HotmailB2BVerifMethod::Smtp",
            "HotmailB2CVerifMethod::Smtp",
            "MimecastVerifMethod::Smtp",
            "ProofpointVerifMethod::Smtp",
            "YahooVerifMethod::Smtp",
            "EverythingElseVerifMethod::Smtp",
        ):
            self.assertIn(variant, source)
        self.assertNotIn("Headless", source)
        self.assertNotIn("::Api", source)
        self.assertIn(".check_gravatar(false)", source)
        self.assertIn(".haveibeenpwned_api_key(None)", source)
