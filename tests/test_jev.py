import json
import io
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from lead_collector.ai import AiCandidate, AiSource
from lead_collector.jev import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    JEV_MODEL,
    JevContractError,
    _CHILD_PERMIT,
    _child_environment,
    _child_main,
    _child_response,
    _is_public_source,
    _provider_response,
    build_jev_request,
    invoke_jev,
    parse_jev_response,
    replay_jev_response,
)
from lead_collector.model import ObservedValue


FIXTURE = Path(__file__).parent / "fixtures" / "jev-eval-spec.json"
PUBLIC_URL = "https://example.test/directory"
OBSERVED_AT = "2026-10-31T00:00:00Z"


def _candidate(identifier: str, field: str, raw: str, source: str | None = None) -> AiCandidate:
    source = source or "Listed business: Acme Example."
    if source == "Listed business: Acme Example." and raw not in source:
        source = f"Listed business: {raw}."
    return AiCandidate(identifier, field, ObservedValue(raw, raw, PUBLIC_URL, OBSERVED_AT), source)


def _source(kind: str = "synthetic") -> AiSource:
    return AiSource(PUBLIC_URL, OBSERVED_AT, kind)


def _response(request: object, *, choices: dict[str, str], support: dict[str, float]) -> bytes:
    questions = getattr(request, "payload")["questions"]
    answers: dict[str, object] = {}
    for question_id, question in questions.items():
        if question["type"] == "choice":
            probabilities = {criterion: 0.0 for criterion in question["criteria"]}
            probabilities[choices[question_id]] = 1.0
            answers[question_id] = {
                "type": "choice",
                "choice": choices[question_id],
                "confidence": 1.0,
                "probabilities": probabilities,
            }
        else:
            candidate_id = question_id.removeprefix("support:")
            answers[question_id] = {"type": "noul", "noul": support[candidate_id]}
    return json.dumps({"model": JEV_MODEL, "answers": answers, "usage": {"input_tokens": 12, "output_tokens": 3}}).encode("utf-8")


class JevContractTests(unittest.TestCase):
    def test_request_uses_pinned_choice_none_and_support_questions(self):
        request = build_jev_request(
            _source(),
            (_candidate("c0", "company", "Acme Example"), _candidate("c1", "company", "Other Example")),
        )

        self.assertEqual(request.payload["model"], JEV_MODEL)
        questions = request.payload["questions"]
        self.assertEqual(set(questions), {"choice:company", "support:c0", "support:c1"})
        self.assertEqual(set(questions["choice:company"]["criteria"]), {"c0", "c1", "none"})
        self.assertEqual(questions["support:c0"]["type"], "noul")
        self.assertLessEqual(len(request.body), 32 * 1024)
        self.assertIn("c0", questions["choice:company"]["criteria"]["c0"])
        self.assertIn("state.candidates[0]", questions["support:c0"]["instructions"])
        self.assertNotIn("Acme Example", str(questions))
        self.assertIn("Acme Example", str(request.payload["state"]))

    def test_questions_reference_global_state_indexes_for_mixed_fields_without_raw_values(self):
        candidates = (
            _candidate("c0", "company", "Northwind"),
            _candidate("c1", "email", "hello@example.test"),
            _candidate("c2", "company", "Contoso"),
        )

        request = build_jev_request(_source(), candidates)
        questions = request.payload["questions"]

        self.assertIn("state.candidates[1]", questions["support:c1"]["instructions"])
        self.assertIn("state.candidates[2]", questions["choice:company"]["criteria"]["c2"])
        question_text = str(questions)
        self.assertNotIn("Northwind", question_text)
        self.assertNotIn("hello@example.test", question_text)

    def test_child_refuses_key_access_without_internal_metered_permit(self):
        with patch("lead_collector.jev.os.getenv") as getenv:
            self.assertEqual(_child_main(), 2)
        getenv.assert_not_called()

    def test_actual_child_from_another_directory_denies_key_lookup_and_sockets_without_permit(self):
        request = build_jev_request(_source(), (_candidate("c0", "company", "Acme Example"),))
        with tempfile.TemporaryDirectory(prefix="lead-collector-jev-child-") as directory:
            root = Path(directory)
            hooks = root / "hooks"
            hooks.mkdir()
            (hooks / "sitecustomize.py").write_text(
                "import lead_collector.jev as _jev\n"
                "import os as _os\n"
                "import socket as _socket\n"
                "def _deny(*_args, **_kwargs):\n"
                "    raise RuntimeError('socket denied')\n"
                "_socket.socket = _deny\n"
                "_getenv = _os.getenv\n"
                "def _guard(name, default=None):\n"
                "    if name == 'TYPESAFE_API_KEY':\n"
                "        raise RuntimeError('key lookup denied')\n"
                "    return _getenv(name, default)\n"
                "_os.getenv = _guard\n",
                encoding="utf-8",
            )
            environment = {
                "HOME": str(root),
                "TMPDIR": str(root),
                "PATH": os.defpath,
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": os.pathsep.join((str(hooks), str(Path(__file__).resolve().parents[1]))),
            }
            result = subprocess.run(
                [sys.executable, "-m", "lead_collector.jev", "--child"],
                cwd=root,
                env=environment,
                input=request.body,
                capture_output=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 2, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(result.stdout, b"")

    def test_provider_accepts_json_media_type_parameters(self):
        response = MagicMock()
        response.status = 200
        response.getheader.return_value = "application/json; charset=utf-8"
        response.read.return_value = b"{}"
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch("lead_collector.jev.http.client.HTTPSConnection", return_value=connection):
            self.assertEqual(_provider_response(b"{}", "test-key"), b"{}")
        connection.close.assert_called_once()

    def test_provider_rejects_a_non_json_media_type_after_reading_a_bounded_body(self):
        response = MagicMock()
        response.status = 200
        response.getheader.return_value = "text/plain; charset=utf-8"
        response.read.return_value = b"{}"
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch("lead_collector.jev.http.client.HTTPSConnection", return_value=connection), self.assertRaises(ValueError):
            _provider_response(b"{}", "test-key")
        response.read.assert_called_once()
        connection.close.assert_called_once()

    def test_preview_contract_rejects_an_unsupported_candidate_before_live_path(self):
        source = _source()
        unsupported = (_candidate("c0", "company", "Imagined Example", "Company name is not published."),)

        with patch("lead_collector.jev.os.getenv") as getenv:
            with self.assertRaises(JevContractError) as caught:
                build_jev_request(source, unsupported)
        self.assertEqual(caught.exception.code, "INPUT_UNSAFE")
        getenv.assert_not_called()

    def test_replay_returns_labelled_review_suggestions_without_mutating_observations(self):
        observation = ObservedValue("Acme Example", "Acme Example", PUBLIC_URL, OBSERVED_AT)
        source = _source()
        request = build_jev_request(source, (AiCandidate("c0", "company", observation, "Company: Acme Example."),))

        result = replay_jev_response(source, (AiCandidate("c0", "company", observation, "Company: Acme Example."),), _response(request, choices={"choice:company": "c0"}, support={"c0": 0.97}))

        self.assertTrue(result.contract_only)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.model, JEV_MODEL)
        self.assertEqual(result.accepted[0].observation, observation)
        self.assertEqual(result.accepted[0].support_probability, 0.97)

    def test_rejects_wrong_model_extra_answer_duplicate_keys_and_non_max_choice(self):
        source = _source()
        candidates = (_candidate("c0", "company", "Acme Example"), _candidate("c1", "company", "Other Example"))
        request = build_jev_request(source, candidates)
        valid = json.loads(_response(request, choices={"choice:company": "c0"}, support={"c0": 1.0, "c1": 0.0}))
        malformed = (
            {**valid, "model": "jev-latest"},
            {**valid, "answers": {**valid["answers"], "unexpected": {"type": "noul", "noul": 0.1}}},
            {**valid, "answers": {**valid["answers"], "choice:company": {"type": "choice", "choice": "c1", "confidence": 0.4, "probabilities": {"c0": 0.6, "c1": 0.4, "none": 0.0}}}},
        )
        for body in malformed:
            with self.subTest(body=body):
                with self.assertRaises(JevContractError) as caught:
                    parse_jev_response(request, json.dumps(body).encode("utf-8"))
                self.assertEqual(caught.exception.code, "AI_OUTPUT_INVALID")
        duplicate = b'{"model":"jev-1.14.0","model":"jev-1.14.0","answers":{},"usage":{"input_tokens":0,"output_tokens":0}}'
        with self.assertRaises(JevContractError) as caught:
            parse_jev_response(request, duplicate)
        self.assertEqual(caught.exception.code, "AI_OUTPUT_INVALID")

    def test_cost_guard_precedes_key_lookup_socket_and_child_creation(self):
        request = build_jev_request(_source(), (_candidate("c0", "company", "Acme Example"),))
        with patch("lead_collector.jev.os.getenv") as getenv, patch("lead_collector.jev.subprocess.Popen") as popen:
            result = invoke_jev(request, allow_metered=False)

        self.assertEqual(result.status, "blocked")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "COST_POLICY")
        getenv.assert_not_called()
        popen.assert_not_called()

    def test_live_path_revalidates_replaced_candidates_before_key_lookup(self):
        source = AiSource(PUBLIC_URL, OBSERVED_AT, "anonymous-http")
        valid = _candidate("c0", "company", "Acme Example")
        unsupported = AiCandidate("c0", "company", valid.observation, "No company value is published.")
        forged = replace(build_jev_request(source, (valid,)), candidates=(unsupported,))

        with patch("lead_collector.jev.os.getenv") as getenv, patch("lead_collector.jev._child_response") as child:
            result = invoke_jev(forged, allow_metered=True)

        self.assertEqual(result.status, "blocked")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "INPUT_UNSAFE")
        getenv.assert_not_called()
        child.assert_not_called()

    def test_live_path_rebuilds_forged_request_payload_before_invoking_its_child(self):
        source = AiSource(PUBLIC_URL, OBSERVED_AT, "anonymous-http")
        expected = build_jev_request(source, (_candidate("c0", "company", "Acme Example"),))
        forged = replace(expected, payload={"model": "forged"}, body=b'{"model":"forged"}', choice_options={}, choice_fields={}, support_questions={})
        response = _response(expected, choices={"choice:company": "c0"}, support={"c0": 1.0})

        with patch("lead_collector.jev.os.getenv", return_value="test-key"), patch("lead_collector.jev._child_response", return_value=response) as child:
            result = invoke_jev(forged, allow_metered=True)

        self.assertEqual(result.status, "completed")
        self.assertEqual(child.call_args.args[0], expected)
        self.assertEqual(child.call_args.args[1], "test-key")

    def test_live_path_rejects_offline_browser_and_private_sources_before_key_lookup(self):
        for kind, url in (("offline", PUBLIC_URL), ("browser", PUBLIC_URL), ("anonymous-http", "https://127.0.0.1/")):
            with self.subTest(kind=kind, url=url):
                source = AiSource(url, OBSERVED_AT, kind)
                candidate = AiCandidate("c0", "company", ObservedValue("Acme Example", "Acme Example", url, OBSERVED_AT), "Listed business: Acme Example.")
                with patch("lead_collector.jev.os.getenv") as getenv:
                    result = invoke_jev(build_jev_request(source, (candidate,)), allow_metered=True)
                self.assertEqual(result.status, "blocked")
                self.assertIsNotNone(result.error)
                self.assertEqual(result.error.code, "INPUT_UNSAFE")
                getenv.assert_not_called()

    def test_live_path_reuses_transport_policy_for_literal_encoded_and_local_targets(self):
        unsafe_urls = (
            "https://224.0.0.1/",
            "https://8.8.8.8/",
            "https://0177.0.0.1/",
            "https://%31%32%37.0.0.1/",
            "https://[::ffff:127.0.0.1]/",
            "https://example.test/path#fragment",
            "https://localhost/",
        )
        for url in unsafe_urls:
            with self.subTest(url=url), patch("lead_collector.jev.os.getenv") as getenv:
                self.assertFalse(_is_public_source(AiSource(url, OBSERVED_AT, "anonymous-http")))
                candidate = AiCandidate("c0", "company", ObservedValue("Acme Example", "Acme Example", url, OBSERVED_AT), "Listed business: Acme Example.")
                request = build_jev_request(AiSource(url, OBSERVED_AT, "anonymous-http"), (candidate,))
                result = invoke_jev(request, allow_metered=True)
            self.assertEqual(result.error.code, "INPUT_UNSAFE")
            getenv.assert_not_called()

    def test_child_environment_is_minimal_and_binds_home_to_the_owned_directory(self):
        environment = _child_environment("test-key", "/tmp/owned-jev")

        self.assertEqual(environment["HOME"], "/tmp/owned-jev")
        self.assertEqual(environment["TMPDIR"], "/tmp/owned-jev")
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertFalse(any("proxy" in name.lower() or "trace" in name.lower() for name in environment))

    def test_child_creation_and_communication_failures_are_named_and_cleanup_is_owned(self):
        request = build_jev_request(_source(), (_candidate("c0", "company", "Acme Example"),))
        with patch("lead_collector.jev.subprocess.Popen") as cancelled_popen:
            with self.assertRaises(JevContractError) as cancelled:
                _child_response(request, "test-key", lambda: True)
        self.assertEqual(cancelled.exception.code, "CANCELLED")
        cancelled_popen.assert_not_called()

        with patch("lead_collector.jev.subprocess.Popen", side_effect=OSError):
            with self.assertRaises(JevContractError) as created:
                _child_response(request, "test-key", None)
        self.assertEqual(created.exception.code, "CAPABILITY_UNAVAILABLE")

        process = MagicMock()
        process.communicate.side_effect = OSError
        process.poll.return_value = None
        with patch("lead_collector.jev.subprocess.Popen", return_value=process):
            with self.assertRaises(JevContractError) as communicated:
                _child_response(request, "test-key", None)
        self.assertEqual(communicated.exception.code, "CAPABILITY_UNAVAILABLE")
        process.kill.assert_called_once()

    def test_request_rejects_invalid_source_and_candidate_contracts(self):
        valid_source = _source()
        valid_candidate = _candidate("c0", "company", "Acme Example")
        wrong_provenance = AiCandidate(
            "c0",
            "company",
            ObservedValue("Acme Example", "Acme Example", "https://other.test/", OBSERVED_AT),
            "Company: Acme Example.",
        )
        unsupported = AiCandidate(
            "c0",
            "company",
            ObservedValue("Acme Example", "Acme Example", PUBLIC_URL, OBSERVED_AT),
            "No company value is published.",
        )
        candidate_cases = (
            ("empty", (), "at least one candidate is required"),
            ("bad id", (_candidate("company", "company", "Acme Example"),), "invalid candidate identifier"),
            (
                "duplicate id",
                (valid_candidate, _candidate("c0", "email", "hello@example.test")),
                "invalid candidate identifier",
            ),
            ("unknown field", (_candidate("c0", "founder", "Acme Example"),), "invalid candidate"),
            ("wrong provenance", (wrong_provenance,), "candidate provenance mismatch"),
            ("unsupported fragment", (unsupported,), "unsupported candidate"),
        )
        for label, candidates, message in candidate_cases:
            with self.subTest(label=label), self.assertRaises(JevContractError) as caught:
                build_jev_request(valid_source, candidates)
            self.assertEqual(caught.exception.code, "INPUT_UNSAFE")
            self.assertEqual(caught.exception.message, message)

        for label, source in (
            ("missing scheme", AiSource("example.test", OBSERVED_AT, "synthetic")),
            ("credentialed url", AiSource("https://user:pass@example.test/", OBSERVED_AT, "synthetic")),
            ("missing timestamp", AiSource(PUBLIC_URL, "", "synthetic")),
        ):
            with self.subTest(label=label), self.assertRaises(JevContractError) as caught:
                build_jev_request(source, (valid_candidate,))
            self.assertEqual(caught.exception.code, "INPUT_UNSAFE")
            self.assertEqual(caught.exception.message, "invalid source provenance")

    def test_request_refuses_an_oversized_candidate_payload_before_any_provider_work(self):
        oversized = _candidate("c0", "company", "x" * MAX_REQUEST_BYTES)

        with self.assertRaises(JevContractError) as caught:
            build_jev_request(_source(), (oversized,))

        self.assertEqual(caught.exception.code, "INPUT_UNSAFE")
        self.assertEqual(caught.exception.message, "Jev request exceeds the input limit")

    def test_parser_rejects_invalid_probabilities_usage_model_and_answer_shapes(self):
        request = build_jev_request(_source(), (_candidate("c0", "company", "Acme Example"),))
        baseline = json.loads(
            _response(request, choices={"choice:company": "c0"}, support={"c0": 1.0})
        )
        cases = (
            ("boolean confidence", ("answers", "choice:company", "confidence"), True),
            ("out-of-range probability", ("answers", "choice:company", "probabilities", "c0"), 1.1),
            ("non-finite probability", ("answers", "choice:company", "probabilities", "c0"), float("nan")),
            ("string support", ("answers", "support:c0", "noul"), "1.0"),
            ("boolean input tokens", ("usage", "input_tokens"), True),
            ("unexpected usage shape", ("usage",), {"input_tokens": 1, "output_tokens": 1, "extra": 0}),
            ("wrong model", ("model",), "jev-latest"),
        )
        for label, path, value in cases:
            payload = json.loads(json.dumps(baseline))
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(label=label), self.assertRaises(JevContractError) as caught:
                parse_jev_response(request, json.dumps(payload).encode("utf-8"))
            self.assertEqual(caught.exception.code, "AI_OUTPUT_INVALID")

    def test_parser_rejects_bounded_invalid_choice_and_support_response_shapes(self):
        request = build_jev_request(_source(), (_candidate("c0", "company", "Acme Example"),))
        baseline = json.loads(
            _response(request, choices={"choice:company": "c0"}, support={"c0": 1.0})
        )
        cases = (
            ("answers object", ("answers",), []),
            ("choice answer keys", ("answers", "choice:company"), {"type": "choice"}),
            ("unknown choice", ("answers", "choice:company", "choice"), "c99"),
            ("choice probability keys", ("answers", "choice:company", "probabilities"), {"c0": 1.0}),
            (
                "choice probability sum",
                ("answers", "choice:company", "probabilities"),
                {"c0": 0.5, "none": 0.4},
            ),
            ("support answer type", ("answers", "support:c0"), {"type": "choice", "noul": 1.0}),
        )
        for label, path, value in cases:
            payload = json.loads(json.dumps(baseline))
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(label=label), self.assertRaises(JevContractError) as caught:
                parse_jev_response(request, json.dumps(payload).encode("utf-8"))
            self.assertEqual(caught.exception.code, "AI_OUTPUT_INVALID")

        with self.assertRaises(JevContractError) as caught:
            parse_jev_response(request, b"x" * (MAX_RESPONSE_BYTES + 1))
        self.assertEqual(caught.exception.code, "AI_OUTPUT_INVALID")

    def test_child_response_times_out_or_cancels_after_start_and_kills_only_its_child(self):
        request = build_jev_request(_source(), (_candidate("c0", "company", "Acme Example"),))

        timed_out = MagicMock()
        timed_out.poll.return_value = None
        with (
            patch("lead_collector.jev.subprocess.Popen", return_value=timed_out),
            patch("lead_collector.jev.time.monotonic", side_effect=(10.0, 40.1)),
            self.assertRaises(JevContractError) as caught,
        ):
            _child_response(request, "test-key", None)
        self.assertEqual(caught.exception.code, "AI_TIMEOUT")
        timed_out.kill.assert_called_once()
        timed_out.communicate.assert_called_once_with(timeout=1)

        cancelled = MagicMock()
        cancelled.poll.return_value = None
        cancelled.communicate.side_effect = (
            subprocess.TimeoutExpired("jev-child", 0.25),
            (b"", b""),
        )
        check_cancelled = MagicMock(side_effect=(False, False, True))
        with (
            patch("lead_collector.jev.subprocess.Popen", return_value=cancelled),
            self.assertRaises(JevContractError) as caught,
        ):
            _child_response(request, "test-key", check_cancelled)
        self.assertEqual(caught.exception.code, "CANCELLED")
        cancelled.kill.assert_called_once()
        self.assertEqual(cancelled.communicate.call_count, 2)
        self.assertEqual(cancelled.communicate.call_args_list[0].kwargs["input"], request.body)

    def test_child_response_nonzero_exit_is_named_without_exposing_child_output(self):
        request = build_jev_request(_source(), (_candidate("c0", "company", "Acme Example"),))
        process = MagicMock()
        process.communicate.return_value = (b"untrusted provider failure", b"")
        process.returncode = 2
        process.poll.return_value = 2

        with patch("lead_collector.jev.subprocess.Popen", return_value=process), self.assertRaises(
            JevContractError
        ) as caught:
            _child_response(request, "test-key", None)

        self.assertEqual(caught.exception.code, "CAPABILITY_UNAVAILABLE")
        self.assertNotIn("untrusted", caught.exception.message)
        process.kill.assert_not_called()

    def test_child_response_returns_owned_success_output_without_a_provider_connection(self):
        request = build_jev_request(_source(), (_candidate("c0", "company", "Acme Example"),))
        process = MagicMock()
        process.communicate.return_value = (b'{"model":"jev-1.14.0"}', b"")
        process.returncode = 0
        process.poll.return_value = 0

        with patch("lead_collector.jev.subprocess.Popen", return_value=process):
            response = _child_response(request, "test-key", None)

        self.assertEqual(response, b'{"model":"jev-1.14.0"}')
        process.kill.assert_not_called()
        self.assertEqual(process.communicate.call_count, 2)

    def test_live_path_refuses_missing_key_and_child_permit_refuses_keyless_provider_call(self):
        candidate = _candidate("c0", "company", "Acme Example")
        public_source = AiSource(PUBLIC_URL, OBSERVED_AT, "anonymous-http")
        with (
            patch("lead_collector.jev.os.getenv", return_value=None) as getenv,
            patch("lead_collector.jev.subprocess.Popen") as popen,
        ):
            result = invoke_jev(build_jev_request(public_source, (candidate,)), allow_metered=True)
        self.assertEqual(result.status, "blocked")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "CAPABILITY_UNAVAILABLE")
        popen.assert_not_called()
        self.assertIn("TYPESAFE_API_KEY", [call.args[0] for call in getenv.call_args_list])

        def child_environment(name: str) -> str | None:
            return "1" if name == _CHILD_PERMIT else None

        with (
            patch("lead_collector.jev._read_child_request", return_value=b"{}"),
            patch("lead_collector.jev.os.getenv", side_effect=child_environment),
            patch("lead_collector.jev._provider_response") as provider,
        ):
            self.assertEqual(_child_main(), 2)
        provider.assert_not_called()

    def test_live_path_cancellation_precedes_key_lookup_and_mocked_child_result_is_parsed(self):
        candidate = _candidate("c0", "company", "Acme Example")
        source = AiSource(PUBLIC_URL, OBSERVED_AT, "anonymous-http")
        request = build_jev_request(source, (candidate,))
        with patch("lead_collector.jev.os.getenv") as getenv:
            cancelled = invoke_jev(request, allow_metered=True, cancelled=lambda: True)
        self.assertEqual(cancelled.status, "blocked")
        self.assertIsNotNone(cancelled.error)
        self.assertEqual(cancelled.error.code, "CANCELLED")
        getenv.assert_not_called()

        provider_response = _response(request, choices={"choice:company": "c0"}, support={"c0": 1.0})
        with (
            patch("lead_collector.jev.os.getenv", return_value="test-key"),
            patch("lead_collector.jev._child_response", return_value=provider_response) as child,
        ):
            result = invoke_jev(request, allow_metered=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual([item.candidate_id for item in result.accepted], ["c0"])
        child.assert_called_once()

    def test_child_main_writes_only_mocked_provider_bytes_after_permit_and_key_checks(self):
        output = io.BytesIO()
        stdout = SimpleNamespace(buffer=output)

        def child_environment(name: str) -> str | None:
            if name == _CHILD_PERMIT:
                return "1"
            if name == "TYPESAFE_API_KEY":
                return "test-key"
            return None

        with (
            patch("lead_collector.jev._read_child_request", return_value=b"{}"),
            patch("lead_collector.jev.os.getenv", side_effect=child_environment),
            patch("lead_collector.jev._provider_response", return_value=b'{"ok":true}') as provider,
            patch("lead_collector.jev.sys.stdout", stdout),
        ):
            self.assertEqual(_child_main(), 0)

        provider.assert_called_once_with(b"{}", "test-key")
        self.assertEqual(output.getvalue(), b'{"ok":true}')

    def test_all_fifty_labelled_cases_replay_the_complete_choice_noul_contract(self):
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        self.assertEqual(len(cases), 50)
        for case in cases:
            with self.subTest(case=case["id"]):
                source = AiSource(PUBLIC_URL, OBSERVED_AT, "synthetic")
                candidates = tuple(_candidate(item["id"], case["field"], item["raw"], case["source"]) for item in case["candidates"])
                try:
                    request = build_jev_request(source, candidates)
                except JevContractError:
                    continue
                result = replay_jev_response(
                    source,
                    candidates,
                    _response(request, choices={f"choice:{case['field']}": case["expected_choice"]}, support={identifier: 1.0 if expected else 0.0 for identifier, expected in case["expected_support"].items()}),
                )
                suggestions = {item.candidate_id: item for item in (*result.accepted, *result.rejected)}
                self.assertEqual({identifier: item.support_probability >= 0.5 for identifier, item in suggestions.items()}, case["expected_support"])
                selected = [item.candidate_id for item in result.accepted if item.selected]
                expected = [] if case["expected_choice"] == "none" else [case["expected_choice"]]
                self.assertEqual(selected, expected)


if __name__ == "__main__":
    unittest.main()
