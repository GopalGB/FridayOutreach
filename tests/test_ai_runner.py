import json
import io
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from lead_collector.ai import AiSource
from lead_collector.ai_runner import (
    AssistInput,
    _child_environment,
    _ollama_json,
    _probe_local_model,
    _result_from_worker,
    _run_worker,
    _is_local_model,
    _validate_local_model_metadata,
    _worker_command,
    assist,
    sanitize_html,
)


MODEL = "ollama/qwen3:0.6b"
SOURCE = AiSource("https://example.test/directory", "2026-10-31T00:00:00Z", "offline")
HTML = "<html><body><p>Acme Example</p><a href=\"https://acme.test\">acme.test</a></body></html>"
WORKER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_runner_worker.py"
OPTIONAL_AI_RUNTIME = Path(__file__).resolve().parents[1] / ".venv-ai" / "bin" / "python"
WORKER_SPEC = spec_from_file_location("lead_collector_ai_runner_worker", WORKER_PATH)
if WORKER_SPEC is None or WORKER_SPEC.loader is None:
    raise RuntimeError("worker script is unavailable")
WORKER = module_from_spec(WORKER_SPEC)
WORKER_SPEC.loader.exec_module(WORKER)


def _response(fields: dict[str, list[str]], *, attempts: list[dict[str, str]] | None = None) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "status": "completed",
            "model": MODEL,
            "fields": fields,
            "usage": None,
            "networkAttempts": [] if attempts is None else attempts,
            "error": None,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _input(*, source: AiSource = SOURCE, html: str = HTML, model: str | None = MODEL, cancelled=None) -> AssistInput:
    return AssistInput(source, html, "scrapegraph", model, cancelled)


class AiRunnerBoundaryTests(unittest.TestCase):
    def test_off_and_non_scrapegraph_modes_do_not_import_or_spawn_optional_sdk(self):
        with patch("lead_collector.ai_runner.subprocess.Popen") as popen:
            self.assertEqual(assist(AssistInput(SOURCE, HTML, "off", None)).status, "disabled")
            self.assertEqual(assist(AssistInput(SOURCE, HTML, "jev", None)).error.code, "COST_POLICY")
        popen.assert_not_called()

    def test_invalid_provider_is_rejected_before_model_probe_or_child_creation(self):
        with patch("lead_collector.ai_runner._probe_local_model") as probe, patch("lead_collector.ai_runner.subprocess.Popen") as popen:
            result = assist(AssistInput(SOURCE, HTML, "scrapegraph", "openai/gpt-5"))
        self.assertEqual(result.error.code, "INPUT_INVALID")
        probe.assert_not_called()
        popen.assert_not_called()

    def test_missing_isolated_runtime_is_reported_without_a_child(self):
        with patch("lead_collector.ai_runner._venv_python", return_value=Path("/missing/python")), patch("lead_collector.ai_runner.subprocess.Popen") as popen:
            result = assist(AssistInput(SOURCE, HTML, "scrapegraph", MODEL))
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.error.code, "SDK_UNAVAILABLE")
        popen.assert_not_called()

    def test_child_environment_is_minimal_and_excludes_keys_tracing_and_proxy_values(self):
        environment = _child_environment("/tmp/lead-collector-ai-home")

        self.assertEqual(environment["HOME"], "/tmp/lead-collector-ai-home")
        self.assertEqual(environment["PYTHON_DOTENV_DISABLED"], "1")
        self.assertEqual(environment["SCRAPEGRAPHAI_TELEMETRY_ENABLED"], "false")
        self.assertEqual(environment["OPENBLAS_NUM_THREADS"], "1")
        self.assertEqual(environment["OMP_NUM_THREADS"], "1")
        self.assertEqual(environment["VECLIB_MAXIMUM_THREADS"], "1")
        self.assertEqual(environment["TOKENIZERS_PARALLELISM"], "false")
        self.assertFalse(any("key" in key.lower() or "proxy" in key.lower() or "trace" in key.lower() for key in environment))

    def test_sanitized_html_uses_utf8_byte_limit_and_removes_active_markup(self):
        sanitized = sanitize_html("<script>ignore</script><p>Acme</p>")

        self.assertIn("<p>Acme</p>", sanitized)
        self.assertNotIn("ignore", sanitized)
        with self.assertRaises(ValueError):
            sanitize_html("é" * (32 * 1024))

    def test_loopback_model_probe_uses_only_tags_and_show_metadata(self):
        tags = {"models": [{"name": "qwen3:0.6b"}]}
        show = {"details": {}}
        with patch("lead_collector.ai_runner._ollama_json", side_effect=[tags, show]) as metadata:
            self.assertIsNone(_probe_local_model(MODEL))

        self.assertEqual(metadata.call_args_list[0].args[:2], ("GET", "/api/tags"))
        self.assertEqual(metadata.call_args_list[1].args[:2], ("POST", "/api/show"))
        with patch("lead_collector.ai_runner._ollama_json", side_effect=OSError("unavailable")):
            self.assertEqual(_probe_local_model(MODEL).code, "CAPABILITY_UNAVAILABLE")

    def test_loopback_json_request_uses_fixed_host_without_a_proxy(self):
        response = MagicMock(status=200)
        response.read.return_value = b'{"models":[]}'
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch("lead_collector.ai_runner.http.client.HTTPConnection", return_value=connection) as http_connection:
            body = _ollama_json("POST", "/api/show", {"name": "qwen3:0.6b"})

        self.assertEqual(body, {"models": []})
        http_connection.assert_called_once_with("127.0.0.1", 11434, timeout=2)
        self.assertEqual(connection.request.call_args.args[:2], ("POST", "/api/show"))
        connection.close.assert_called_once()

    def test_cloud_and_remote_model_metadata_are_rejected_before_inference(self):
        tags = {"models": [{"name": "qwen3:0.6b", "remote_model": "qwen3:0.6b"}]}
        show = {"details": {"remote_host": "cloud.ollama.com"}}
        cloud_tags = {"models": [{"name": "qwen3:0.6b", "cloud_model": "qwen3:0.6b"}]}
        malformed_remote = {"details": {"remote_host": True}}

        with self.assertRaises(ValueError):
            _validate_local_model_metadata(MODEL, tags, show)
        with self.assertRaises(ValueError):
            _validate_local_model_metadata(MODEL, cloud_tags, {})
        with self.assertRaises(ValueError):
            _validate_local_model_metadata(MODEL, {"models": [{"name": "qwen3:0.6b"}]}, malformed_remote)

    def test_model_name_rejects_cloud_tags_before_metadata_probe(self):
        self.assertFalse(_is_local_model("ollama/qwen3:cloud"))
        self.assertTrue(_is_local_model(MODEL))

    def test_worker_output_rejects_noise_oversize_unknown_fields_and_hallucinated_values(self):
        valid = _response({"company": ["Acme Example"], "website": ["https://acme.test"], "address": [], "email": [], "phone": []})
        noisy = b"diagnostic\n" + valid
        oversized = b"x" * (64 * 1024 + 1)
        unknown = _response({"company": ["Acme Example"], "website": [], "address": [], "email": [], "phone": [], "founders": ["Eve"]})
        invented = _response({"company": ["Imagined Corp"], "website": [], "address": [], "email": [], "phone": []})

        self.assertEqual(_result_from_worker(noisy, _input(), HTML).error.code, "AI_OUTPUT_INVALID")
        self.assertEqual(_result_from_worker(oversized, _input(), HTML).error.code, "AI_OUTPUT_INVALID")
        self.assertEqual(_result_from_worker(unknown, _input(), HTML).error.code, "AI_OUTPUT_INVALID")
        self.assertEqual(_result_from_worker(invented, _input(), HTML).error.code, "AI_OUTPUT_INVALID")
        accepted = _result_from_worker(valid, _input(), HTML).accepted
        self.assertEqual({item.field for item in accepted}, {"company", "website"})
        self.assertTrue(all(item.observation.source_url == SOURCE.url for item in accepted))
        self.assertTrue(all(item.choice_confidence is None and item.probabilities is None for item in accepted))

    def test_source_evidence_excludes_wrapper_hidden_text_and_supports_decoded_visible_text(self):
        wrapper = sanitize_html("<p>Acme</p>")
        hidden = sanitize_html("<p hidden>Imagined</p><p>Acme</p>")
        escaped = sanitize_html("<p>Acme &amp; Sons</p>")

        self.assertEqual(_result_from_worker(_response({"company": ["body"], "website": [], "address": [], "email": [], "phone": []}), _input(), wrapper).error.code, "AI_OUTPUT_INVALID")
        self.assertEqual(_result_from_worker(_response({"company": ["Imagined"], "website": [], "address": [], "email": [], "phone": []}), _input(), hidden).error.code, "AI_OUTPUT_INVALID")
        self.assertEqual(_result_from_worker(_response({"company": ["Acme & Sons"], "website": [], "address": [], "email": [], "phone": []}), _input(), escaped).status, "completed")

    def test_worker_protocol_rejects_boolean_version_and_duplicate_json_keys(self):
        boolean_version = json.loads(_response({"company": ["Acme Example"], "website": [], "address": [], "email": [], "phone": []}))
        boolean_version["version"] = True
        duplicate_version = b'{"version":1,"version":1,"status":"completed","model":"ollama/qwen3:0.6b","fields":{"company":[],"website":[],"address":[],"email":[],"phone":[]},"usage":null,"networkAttempts":[],"error":null}'

        self.assertEqual(_result_from_worker(json.dumps(boolean_version).encode("utf-8"), _input(), HTML).error.code, "AI_OUTPUT_INVALID")
        self.assertEqual(_result_from_worker(duplicate_version, _input(), HTML).error.code, "AI_OUTPUT_INVALID")

    def test_worker_protocol_rejects_external_socket_audit_attempts(self):
        response = _response(
            {"company": ["Acme Example"], "website": [], "address": [], "email": [], "phone": []},
            attempts=[{"event": "socket.connect", "destination": "198.51.100.7:443"}],
        )

        result = _result_from_worker(response, _input(), HTML)

        self.assertEqual(result.error.code, "AI_OUTPUT_INVALID")

    def test_socket_audit_guard_records_and_rejects_a_real_connect_event_shape(self):
        attempts: list[dict[str, str]] = []

        with self.assertRaises(PermissionError):
            WORKER._audit_guard("socket.connect", (None, ("198.51.100.7", 443)), attempts)

        self.assertEqual(attempts, [{"event": "socket.connect", "destination": "198.51.100.7:443"}])

    def test_worker_request_rejects_size_json_schema_html_and_model_boundaries(self):
        cases = (
            (
                "oversized protocol",
                b"x" * (WORKER.MAX_PROTOCOL_BYTES + 1),
                ValueError,
                "protocol input exceeds bound",
            ),
            ("malformed json", b"{", json.JSONDecodeError, None),
            (
                "unexpected protocol key",
                json.dumps({"version": 1, "html": HTML, "model": MODEL, "extra": 1}).encode("utf-8"),
                ValueError,
                "invalid protocol input",
            ),
            (
                "non-string model",
                json.dumps({"version": 1, "html": HTML, "model": 1}).encode("utf-8"),
                ValueError,
                "invalid protocol input",
            ),
            (
                "remote model",
                json.dumps({"version": 1, "html": HTML, "model": "openai/gpt-5"}).encode("utf-8"),
                ValueError,
                "invalid protocol input",
            ),
            (
                "URL instead of captured html",
                json.dumps({"version": 1, "html": "https://example.test", "model": MODEL}).encode("utf-8"),
                ValueError,
                "invalid captured HTML",
            ),
        )

        for label, raw, error_type, expected_message in cases:
            with self.subTest(label=label), patch.object(
                WORKER.sys,
                "stdin",
                SimpleNamespace(buffer=io.BytesIO(raw)),
            ):
                with self.assertRaises(error_type) as caught:
                    WORKER._request()
            if expected_message is not None:
                self.assertEqual(str(caught.exception), expected_message)

    def test_socket_audit_guard_handles_ignored_malformed_and_loopback_events(self):
        cases = (
            ("ignored", "subprocess.Popen", (), False, []),
            (
                "malformed connect",
                "socket.connect",
                (None,),
                True,
                [{"event": "socket.connect", "destination": "invalid:0"}],
            ),
            (
                "malformed getaddrinfo",
                "socket.getaddrinfo",
                ("example.test",),
                True,
                [{"event": "socket.getaddrinfo", "destination": "invalid:0"}],
            ),
            (
                "approved loopback",
                "socket.connect",
                (None, ("127.0.0.1", 11434)),
                False,
                [{"event": "socket.connect", "destination": "127.0.0.1:11434"}],
            ),
            (
                "approved loopback resolution",
                "socket.getaddrinfo",
                ("127.0.0.1", 11434),
                False,
                [{"event": "socket.getaddrinfo", "destination": "127.0.0.1:11434"}],
            ),
        )

        for label, event, arguments, denied, expected in cases:
            attempts: list[dict[str, str]] = []
            with self.subTest(label=label):
                if denied:
                    with self.assertRaises(PermissionError):
                        WORKER._audit_guard(event, arguments, attempts)
                else:
                    WORKER._audit_guard(event, arguments, attempts)
            self.assertEqual(attempts, expected)

    def test_bounded_chat_model_rejects_missing_or_malformed_request_builders(self):
        class NoBuilder:
            _chat_params = None

            def __init__(self, **_kwargs):
                pass

        def chat_class(result):
            class BadChat:
                def __init__(self, **_kwargs):
                    pass

                def _chat_params(self, _messages, stop=None, **_kwargs):
                    return result

            return BadChat

        for label, chat_type, expected_message in (
            ("not a class", object(), "ChatOllama class is unavailable"),
            ("missing builder", NoBuilder, "ChatOllama request builder is unavailable"),
        ):
            with self.subTest(label=label), self.assertRaises(RuntimeError) as caught:
                WORKER._bounded_chat_model(chat_type, MODEL)
            self.assertEqual(str(caught.exception), expected_message)

        for label, result, expected_message in (
            ("non-mapping request", [], "ChatOllama request builder returned an invalid request"),
            ("non-mapping options", {"options": []}, "ChatOllama request options are unavailable"),
        ):
            client = WORKER._bounded_chat_model(chat_class(result), MODEL)
            with self.subTest(label=label), self.assertRaises(RuntimeError) as caught:
                client._chat_params([], None)
            self.assertEqual(str(caught.exception), expected_message)

    def test_worker_schema_values_and_protocol_output_are_strictly_bounded(self):
        self.assertEqual(WORKER._strings([]), [])
        for label, value in (("tuple", ("Acme",)), ("mixed list", ["Acme", 1])):
            with self.subTest(label=label), self.assertRaises(ValueError) as caught:
                WORKER._strings(value)
            self.assertEqual(str(caught.exception), "invalid local schema output")

        oversized_fields = {
            "company": ["x" * WORKER.MAX_PROTOCOL_BYTES],
            "website": [],
            "address": [],
            "email": [],
            "phone": [],
        }
        response = WORKER._response(MODEL, oversized_fields, [])
        decoded = json.loads(response)

        self.assertLessEqual(len(response), WORKER.MAX_PROTOCOL_BYTES)
        self.assertEqual(decoded["status"], "failed")
        self.assertEqual(decoded["error"], "AI_EXECUTION_FAILED")
        self.assertIsNone(decoded["fields"])

    def test_worker_refuses_invalid_schema_or_enabled_telemetry_before_graph_execution(self):
        invalid_pydantic = SimpleNamespace(
            Field=lambda *_args, **_kwargs: None,
            ConfigDict=lambda **_kwargs: {},
            create_model=lambda *_args, **_kwargs: object(),
        )
        with self.assertRaises(RuntimeError) as caught:
            WORKER._fields_schema(invalid_pydantic)
        self.assertEqual(str(caught.exception), "LocalFields schema is unavailable")

        schema = type(
            "Schema",
            (),
            {
                "model_json_schema": staticmethod(lambda: {}),
                "model_validate": staticmethod(lambda value: value),
            },
        )
        pydantic = SimpleNamespace(
            Field=lambda *_args, **_kwargs: None,
            ConfigDict=lambda **_kwargs: {},
            create_model=lambda *_args, **_kwargs: schema,
        )
        telemetry = SimpleNamespace(disable_telemetry=lambda: None, is_telemetry_enabled=lambda: True)
        modules = {"pydantic": pydantic, "scrapegraphai.telemetry.telemetry": telemetry}
        with patch.object(WORKER.importlib, "import_module", side_effect=modules.__getitem__) as imports:
            with self.assertRaises(RuntimeError) as caught:
                WORKER._extract(HTML, MODEL)
        self.assertEqual(str(caught.exception), "telemetry remained enabled")
        self.assertEqual([call.args[0] for call in imports.call_args_list], list(modules))

    def test_worker_final_wire_options_reject_missing_or_unbounded_values(self):
        expected = {
            "num_ctx": WORKER.MODEL_CONTEXT_TOKENS,
            "num_predict": WORKER.MODEL_OUTPUT_TOKENS,
            "num_batch": WORKER.MODEL_BATCH_SIZE,
            "num_thread": WORKER.MODEL_THREADS,
            "num_gpu": WORKER.MODEL_GPU_LAYERS,
        }
        self.assertEqual(WORKER._wire_options({"options": expected}), expected)
        for label, request, message in (
            ("missing options mapping", {"options": []}, "final Ollama request had no options"),
            ("unbounded options", {"options": {}}, "final Ollama request did not preserve bounded options"),
        ):
            with self.subTest(label=label), self.assertRaises(RuntimeError) as caught:
                WORKER._wire_options(request)
            self.assertEqual(str(caught.exception), message)

    def test_worker_main_returns_a_sanitized_protocol_failure_for_bad_input(self):
        output = io.BytesIO()
        stdout = SimpleNamespace(buffer=output)
        with (
            patch.object(WORKER.sys, "addaudithook"),
            patch.object(WORKER.sys, "argv", [str(WORKER_PATH)]),
            patch.object(WORKER.sys, "stdout", stdout),
            patch.object(WORKER, "_request", side_effect=ValueError("untrusted <html>")),
        ):
            self.assertEqual(WORKER.main(), 0)

        response = json.loads(output.getvalue())
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"], "AI_EXECUTION_FAILED")
        self.assertIsNone(response["fields"])
        self.assertNotIn("untrusted", output.getvalue().decode("utf-8"))

    def test_timeout_and_cancellation_kill_only_the_owned_child(self):
        process = MagicMock()
        process.communicate.side_effect = subprocess.TimeoutExpired("worker", 0.1)
        process.poll.return_value = None
        cancelled = MagicMock(side_effect=[False, True])
        with patch("lead_collector.ai_runner.subprocess.Popen", return_value=process), patch("lead_collector.ai_runner._terminate_owned") as terminate:
            outcome = _run_worker(Path("/trusted/.venv-ai/bin/python"), _input(cancelled=cancelled), HTML)

        self.assertEqual(outcome.error.code, "CANCELLED")
        terminate.assert_called_once_with(process)

    def test_worker_execution_error_cleans_up_the_owned_child(self):
        process = MagicMock()
        process.communicate.side_effect = OSError("broken pipe")
        process.poll.return_value = None
        with patch("lead_collector.ai_runner.subprocess.Popen", return_value=process), patch("lead_collector.ai_runner._terminate_owned") as terminate:
            outcome = _run_worker(Path("/trusted/.venv-ai/bin/python"), _input(), HTML)

        self.assertEqual(outcome.error.code, "AI_EXECUTION_FAILED")
        terminate.assert_called_once_with(process)

    def test_worker_success_uses_a_temporary_cwd_and_allowlist_environment(self):
        process = MagicMock()
        process.communicate.return_value = (b"{}", b"")
        process.returncode = 0
        process.poll.return_value = 0
        with patch("lead_collector.ai_runner.subprocess.Popen", return_value=process) as popen:
            outcome = _run_worker(Path("/trusted/.venv-ai/bin/python"), _input(), HTML)

        self.assertEqual(outcome.output, b"{}")
        options = popen.call_args.kwargs
        self.assertNotEqual(options["cwd"], str(Path.cwd()))
        self.assertEqual(options["env"]["PYTHON_DOTENV_DISABLED"], "1")
        self.assertFalse(any("proxy" in key.lower() or "key" in key.lower() for key in options["env"]))

    def test_worker_interrupt_cleans_up_the_owned_child_with_a_named_result(self):
        process = MagicMock()
        process.communicate.side_effect = KeyboardInterrupt
        process.poll.return_value = None
        with patch("lead_collector.ai_runner.subprocess.Popen", return_value=process), patch("lead_collector.ai_runner._terminate_owned") as terminate:
            outcome = _run_worker(Path("/trusted/.venv-ai/bin/python"), _input(), HTML)

        self.assertEqual(outcome.error.code, "CANCELLED")
        terminate.assert_called_once_with(process)

    def test_worker_command_uses_isolated_python_and_an_absolute_trusted_worker_path(self):
        command = _worker_command(Path("/trusted/.venv-ai/bin/python"))

        self.assertEqual(command[:2], ("/trusted/.venv-ai/bin/python", "-I"))
        self.assertTrue(Path(command[2]).is_absolute())
        self.assertEqual(Path(command[2]).name, "ai_runner_worker.py")
        self.assertEqual(Path(command[2]).parent.name, "scripts")

    def test_worker_protocol_input_response_and_sdk_configuration_are_bounded(self):
        class FakeGraph:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def run(self):
                return {"company": ["Acme Example"], "website": [], "address": [], "email": [], "phone": []}

        captured: dict[str, object] = {}
        chat_options: dict[str, object] = {}

        def graph_factory(**kwargs):
            captured.update(kwargs)
            return FakeGraph(**kwargs)

        class FakeChat:
            def __init__(self, **kwargs):
                chat_options.update(kwargs)

            def _chat_params(self, _messages, stop=None, **kwargs):
                return {"options": dict(kwargs.get("options", {})), "stop": stop}

        def schema_factory(*_args, **_kwargs):
            return type(
                "Schema",
                (),
                {
                    "model_json_schema": staticmethod(lambda: {}),
                    "model_validate": staticmethod(lambda value: SimpleNamespace(**value)),
                },
            )

        telemetry = SimpleNamespace(disable_telemetry=lambda: None, is_telemetry_enabled=lambda: False)
        modules = {
            "pydantic": SimpleNamespace(Field=lambda *_args, **kwargs: kwargs, ConfigDict=lambda **kwargs: kwargs, create_model=schema_factory),
            "scrapegraphai.telemetry.telemetry": telemetry,
            "langchain_ollama": SimpleNamespace(ChatOllama=FakeChat),
            "scrapegraphai.graphs": SimpleNamespace(SmartScraperGraph=graph_factory),
        }
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps({"version": 1, "html": HTML, "model": MODEL}).encode("utf-8")))
        with patch.object(WORKER.sys, "stdin", stdin), patch.object(WORKER.importlib, "import_module", side_effect=modules.__getitem__):
            request = WORKER._request()
            values = WORKER._extract(*request)

        self.assertEqual(request, (HTML, MODEL))
        self.assertEqual(values["company"], ["Acme Example"])
        self.assertEqual(captured["source"], HTML)
        self.assertEqual(captured["config"]["llm"]["model_tokens"], 2048)
        client = captured["config"]["llm"]["model_instance"]
        options = client._chat_params([], None)["options"]
        self.assertNotIn("num_batch", chat_options)
        self.assertEqual(options["num_batch"], 32)
        self.assertEqual(chat_options["num_ctx"], 2048)
        self.assertEqual(chat_options["num_predict"], 256)
        self.assertEqual(chat_options["num_thread"], 1)
        self.assertEqual(chat_options["num_gpu"], 0)
        response = WORKER._response(MODEL, values, [])
        self.assertEqual(json.loads(response)["usage"], None)

    def test_worker_main_emits_a_single_protocol_message_without_sdk_stdout(self):
        output = io.BytesIO()
        stdout = SimpleNamespace(buffer=output)
        values = {"company": ["Acme Example"], "website": [], "address": [], "email": [], "phone": []}
        with (
            patch.object(WORKER.sys, "addaudithook"),
            patch.object(WORKER.sys, "stdout", stdout),
            patch.object(WORKER, "_request", return_value=(HTML, MODEL)),
            patch.object(WORKER, "_extract", return_value=values),
        ):
            self.assertEqual(WORKER.main(), 0)

        self.assertEqual(json.loads(output.getvalue())["fields"], values)

    @unittest.skipUnless(
        OPTIONAL_AI_RUNTIME.is_file(),
        "optional ScrapeGraphAI runtime is not installed",
    )
    def test_actual_sdk_final_client_boundary_carries_the_bounded_batch_option(self):
        runtime = OPTIONAL_AI_RUNTIME
        with tempfile.TemporaryDirectory(prefix="lead-collector-wire-") as home:
            completed = subprocess.run(
                [str(runtime), "-I", str(WORKER_PATH), "--wire-fixture"],
                cwd=home,
                env={
                    "HOME": home,
                    "TMPDIR": home,
                    "PATH": os.defpath,
                    "PYTHON_DOTENV_DISABLED": "1",
                    "PYTHONNOUSERSITE": "1",
                    "SCRAPEGRAPHAI_TELEMETRY_ENABLED": "false",
                },
                capture_output=True,
                timeout=30,
            )

        self.assertEqual(completed.returncode, 0)
        fixture = json.loads(completed.stdout)
        self.assertTrue(fixture["ok"])
        self.assertTrue(fixture["isChatOllama"])
        self.assertEqual(fixture["directOptions"]["num_batch"], 32)
        self.assertEqual(fixture["structuredOptions"]["num_batch"], 32)
        self.assertEqual(fixture["networkAttempts"], [])

    @unittest.skipUnless(
        OPTIONAL_AI_RUNTIME.is_file(),
        "optional ScrapeGraphAI runtime is not installed",
    )
    def test_actual_sdk_schema_requires_every_field_and_describes_literal_values(self):
        runtime = OPTIONAL_AI_RUNTIME
        with tempfile.TemporaryDirectory(prefix="lead-collector-schema-") as home:
            completed = subprocess.run(
                [str(runtime), "-I", str(WORKER_PATH), "--schema-fixture"],
                cwd=home,
                env={
                    "HOME": home,
                    "TMPDIR": home,
                    "PATH": os.defpath,
                    "PYTHON_DOTENV_DISABLED": "1",
                    "PYTHONNOUSERSITE": "1",
                    "SCRAPEGRAPHAI_TELEMETRY_ENABLED": "false",
                },
                capture_output=True,
                timeout=30,
            )

        self.assertEqual(completed.returncode, 0)
        fixture = json.loads(completed.stdout)
        self.assertTrue(fixture["ok"])
        self.assertTrue(fixture["missingFieldRejected"])
        self.assertEqual(fixture["required"], ["company", "website", "address", "email", "phone"])
        self.assertEqual(fixture["describedFields"], ["company", "website", "address", "email", "phone"])
        self.assertEqual(fixture["networkAttempts"], [])


if __name__ == "__main__":
    unittest.main()
