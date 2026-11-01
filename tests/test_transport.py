import http.client
import hashlib
import json
import os
import socket
import subprocess
import time
import unittest
from unittest.mock import patch

from lead_collector.cli import _parser
from lead_collector.fetch_child import FetchFailure, _HopResponse, _PinnedHTTPSConnection, _fetch_target, _read_body, _request_once
from lead_collector.model import FetchRequest, PageDocument, RecipeV1, observed_now
from lead_collector.transport import TransportError, VettedAddress, _canonicalize_target, _resolve_all_public, fetch_page


class TransportPolicyTests(unittest.TestCase):
    def _target(self):
        return _canonicalize_target("https://example.com/directory", ["example.com"], ["/directory"])

    def _assert_dns_unsafe(self, answers):
        with patch("lead_collector.transport.socket.getaddrinfo", return_value=answers):
            with self.assertRaises(TransportError) as caught:
                _resolve_all_public(self._target(), 10**30)
        self.assertEqual(caught.exception.code, "DNS_UNSAFE")

    def test_allowed_path_uses_segment_boundary(self):
        self.assertEqual(_canonicalize_target("https://Example.com/directory/x", ["example.com"], ["/directory"]).hostname, "example.com")
        for url in (
            "https://example.com/directory2",
            "https://example.com/directory/../admin",
            "https://example.com/directory%2Fx",
            "https://example.com/directory/%252e%252e/admin",
            "https://example.com/directory/%25252e%25252e/admin",
        ):
            with self.assertRaises(TransportError) as caught:
                _canonicalize_target(url, ["example.com"], ["/directory"])
            self.assertEqual(caught.exception.code, "INPUT_UNSAFE")

    def test_request_path_preserves_a_trailing_directory_slash_while_scope_normalizes_it(self):
        target = _canonicalize_target("https://example.com/directory/", ["example.com"], ["/directory"])

        self.assertEqual(target.url, "https://example.com/directory/")
        self.assertEqual(target.request_target, "/directory/")

    def test_rejects_unapproved_nondefault_port(self):
        with self.assertRaises(TransportError) as caught:
            _canonicalize_target("https://example.com:8443/directory", ["example.com"], ["/directory"])
        self.assertEqual(caught.exception.code, "INPUT_UNSAFE")

    def test_rejects_backslash_encoded_separator_and_dot_segment(self):
        for url in ("https://example.com\\evil", "https://example.com/a%5Cb", "https://example.com/%2e%2e/admin"):
            with self.assertRaises(TransportError) as caught:
                _canonicalize_target(url, ["example.com"], ["/"])
            self.assertEqual(caught.exception.code, "INPUT_UNSAFE")

    def test_rejects_literal_and_mapped_nonpublic_addresses(self):
        for host in ("127.0.0.1", "2130706433", "127.0.0.1", "::ffff:127.0.0.1"):
            with self.assertRaises(TransportError) as caught:
                _canonicalize_target(f"https://[{host}]/", [host], ["/"]) if ":" in host else _canonicalize_target(f"https://{host}/", [host], ["/"])
            self.assertEqual(caught.exception.code, "INPUT_UNSAFE")

    def test_rejects_mixed_public_private_dns_without_connect(self):
        target = _canonicalize_target("https://example.com/", ["example.com"], ["/"])
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        with patch("lead_collector.transport.socket.getaddrinfo", return_value=answers) as resolver:
            with self.assertRaises(TransportError) as caught:
                _resolve_all_public(target, 10**30)
        resolver.assert_called_once()
        self.assertEqual(caught.exception.code, "DNS_UNSAFE")

    def test_rejects_compatibility_nonpublic_ranges(self):
        for ip in (
            "192.0.0.8",
            "64:ff9b:1::1",
            "2002:7f00:1::1",
            "fec0::1",
            "fec0::2",
            "224.0.0.1",
            "ff02::1",
            "240.0.0.1",
            "0.0.0.0",
        ):
            target = _canonicalize_target("https://example.com/", ["example.com"], ["/"])
            answer = [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0) if ":" in ip else (ip, 443))]
            with patch("lead_collector.transport.socket.getaddrinfo", return_value=answer):
                with self.assertRaises(TransportError) as caught:
                    _resolve_all_public(target, 10**30)
            self.assertEqual(caught.exception.code, "DNS_UNSAFE")

    def test_public_dns_is_deterministic(self):
        target = _canonicalize_target("https://example.com/", ["example.com"], ["/"])
        answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
        ]
        with patch("lead_collector.transport.socket.getaddrinfo", return_value=answer):
            result = _resolve_all_public(target, 10**30)
        self.assertEqual(tuple(item.printable_ip for item in result), ("1.1.1.1", "2606:4700:4700::1111", "8.8.8.8"))

    def test_rejects_local_only_hosts_before_dns(self):
        with patch("lead_collector.transport.socket.getaddrinfo") as resolver:
            for host in ("printer.local", "service.localhost", "host.localdomain", "intranet"):
                with self.subTest(host=host):
                    with self.assertRaises(TransportError) as caught:
                        _canonicalize_target(f"https://{host}/directory", [host], ["/directory"])
                    self.assertEqual(caught.exception.code, "INPUT_UNSAFE")
        resolver.assert_not_called()

    def test_rejects_empty_or_malformed_dns_pins(self):
        invalid_answers = (
            [],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 8443))],
            [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))],
            [(999, socket.SOCK_STREAM, 6, "", ("8.8.4.4", 443, 0))],
            [(socket.AF_INET, socket.SOCK_DGRAM, 6, "", ("8.8.8.8", 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, 17, "", ("8.8.8.8", 443))],
            [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, -1, 0))],
        )
        for answers in invalid_answers:
            with self.subTest(answers=answers):
                self._assert_dns_unsafe(answers)

    def test_rechecks_deadline_after_dns_resolution(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("lead_collector.transport.socket.getaddrinfo", return_value=answers), patch("lead_collector.transport.time.monotonic_ns", side_effect=(1, 3)):
            with self.assertRaises(TransportError) as caught:
                _resolve_all_public(self._target(), 2)
        self.assertEqual(caught.exception.code, "DNS_UNSAFE")

    def test_canonical_request_target_is_ascii_and_http_usable(self):
        target = _canonicalize_target("https://example.com/directory/Grün raw?q=two words&letter=é", ["example.com"], ["/directory"])
        self.assertEqual(target.request_target, "/directory/Gr%C3%BCn%20raw?q=two%20words&letter=%C3%A9")
        self.assertTrue(target.request_target.isascii())
        connection = http.client.HTTPConnection("example.com")
        connection.putrequest("GET", target.request_target)

    def test_rejects_bad_authority_allowlist_and_encoded_query_controls(self):
        cases = (
            ("https://example%2ecom/directory", ["example.com"], ["/directory"]),
            ("https://example.com/directory", ["example.com\n"], ["/directory"]),
            ("https://example.com/directory?q=%0d%0aX-Test:yes", ["example.com"], ["/directory"]),
            ("https://example.com/directory", ["example.com"], ["/directory%252fprivate"]),
        )
        for url, hosts, paths in cases:
            with self.subTest(url=url, hosts=hosts, paths=paths):
                with self.assertRaises(TransportError) as caught:
                    _canonicalize_target(url, hosts, paths)
                self.assertEqual(caught.exception.code, "INPUT_UNSAFE")

    def test_no_production_loopback_or_policy_override(self):
        help_text = _parser().format_help().lower()
        self.assertNotIn("loopback", help_text)
        self.assertNotIn("bypass", help_text)
        recipe = RecipeV1("https://example.com/directory", ("example.com",), ("/directory",), None, {}, {"mode": "none", "selector": None}, {"maxInputs": 1, "maxPagesPerHost": 1, "maxRows": 1, "maxPages": 1})
        self.assertNotIn("allowLoopback", recipe.to_json())
        self.assertFalse(hasattr(recipe, "allow_loopback"))


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b""):
        self.stdout = stdout
        self.stderr = stderr
        self.input: bytes | None = None
        self.pid = os.getpid()
        self.returncode = 0

    def communicate(self, input: bytes | None = None, timeout: float | None = None):
        self.input = input
        return self.stdout, self.stderr


class _FakeSocket:
    def __init__(self):
        self.timeout: float | None = None
        self.destination: tuple[str, int] | tuple[str, int, int, int] | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, address: tuple[str, int] | tuple[str, int, int, int]) -> None:
        self.destination = address


class _StreamingResponse:
    def __init__(self) -> None:
        self.closed = False

    def getheader(self, name: str) -> None:
        return None

    def isclosed(self) -> bool:
        return self.closed

    def read(self, size: int) -> bytes:
        return b"x" * size


class _StuckProcess(_FakeProcess):
    def communicate(self, input: bytes | None = None, timeout: float | None = None):
        if input is not None:
            raise subprocess.TimeoutExpired(["child"], timeout)
        return b"", b""


class TransportFetchTests(unittest.TestCase):
    def _request(self) -> FetchRequest:
        return FetchRequest("https://example.com/directory", ("example.com",), ("/directory",), {"maxInputs": 1, "maxPagesPerHost": 1, "maxRows": 1, "maxPages": 1})

    def _document(self) -> PageDocument:
        html = "<html><body>safe</body></html>"
        return PageDocument("https://example.com/directory", html, observed_now(), hashlib.sha256(html.encode()).hexdigest(), "completed", None)

    def test_parent_uses_trusted_child_protocol_and_anonymous_environment(self):
        process = _FakeProcess(json.dumps({"version": 1, "ok": True, "document": self._document().to_json()}).encode())
        with patch("lead_collector.transport.subprocess.Popen", return_value=process) as popen:
            document = fetch_page(self._request())
        self.assertEqual(document.status, "completed")
        argv = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(argv[1:], ["-E", "-s", "-m", "lead_collector.fetch_child"])
        self.assertTrue(os.path.isabs(argv[0]))
        self.assertFalse(options["shell"])
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["stdin"], subprocess.PIPE)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.PIPE)
        self.assertTrue({"HTTP_PROXY", "HTTPS_PROXY", "COOKIE", "AUTHORIZATION", "PYTHONPATH"}.isdisjoint(options["env"]))
        payload = json.loads(process.input.decode())
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["request"], self._request().to_json())

    def test_parent_accepts_only_a_final_redirect_target_that_remains_in_scope(self):
        html = "<html><body>redirected</body></html>"
        allowed = PageDocument("https://example.com/directory/final", html, observed_now(), hashlib.sha256(html.encode()).hexdigest(), "completed", None)
        denied = PageDocument("https://example.com/other", html, observed_now(), hashlib.sha256(html.encode()).hexdigest(), "completed", None)
        foreign = PageDocument("https://outside.example.test/directory/final", html, observed_now(), hashlib.sha256(html.encode()).hexdigest(), "completed", None)

        with patch("lead_collector.transport.subprocess.Popen", return_value=_FakeProcess(json.dumps({"version": 1, "ok": True, "document": allowed.to_json()}).encode())):
            accepted = fetch_page(self._request())
        with patch("lead_collector.transport.subprocess.Popen", return_value=_FakeProcess(json.dumps({"version": 1, "ok": True, "document": denied.to_json()}).encode())):
            rejected = fetch_page(self._request())
        with patch("lead_collector.transport.subprocess.Popen", return_value=_FakeProcess(json.dumps({"version": 1, "ok": True, "document": foreign.to_json()}).encode())):
            rejected_host = fetch_page(self._request())

        self.assertEqual(accepted.status, "completed")
        self.assertEqual(accepted.url, "https://example.com/directory/final")
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(rejected.error["code"], "RESPONSE_MALFORMED")
        self.assertEqual(rejected_host.status, "failed")
        self.assertEqual(rejected_host.error["code"], "RESPONSE_MALFORMED")

    def test_parent_rejects_protocol_noise_and_oversized_output(self):
        for stdout in (b'{"version":1}\n{"version":1}', b"x" * (12 * 1024 * 1024 + 1)):
            with self.subTest(size=len(stdout)):
                with patch("lead_collector.transport.subprocess.Popen", return_value=_FakeProcess(stdout)):
                    document = fetch_page(self._request())
                self.assertEqual(document.status, "failed")
                self.assertEqual(document.error["code"], "RESPONSE_MALFORMED")

    def test_parent_invalid_target_is_sanitized_and_serializable(self):
        request = FetchRequest("https://user:password@example.com/directory", ("example.com",), ("/directory",), {"maxInputs": 1, "maxPagesPerHost": 1, "maxRows": 1, "maxPages": 1})
        document = fetch_page(request)
        self.assertEqual(document.status, "denied")
        self.assertEqual(document.error["url"], None)
        self.assertNotIn("password", json.dumps(document.to_json()))
        self.assertEqual(PageDocument.from_json(document.to_json()), document)

    def test_https_pins_vetted_ip_with_original_host_and_sni(self):
        target = _canonicalize_target("https://example.com/directory", ["example.com"], ["/directory"])
        vetted = VettedAddress(socket.AF_INET, ("8.8.8.8", 443), "8.8.8.8")
        raw = _FakeSocket()
        context = unittest.mock.MagicMock()
        context.wrap_socket.return_value = raw
        with patch("lead_collector.fetch_child.socket.socket", return_value=raw), patch("lead_collector.fetch_child.ssl.create_default_context", return_value=context):
            connection = _PinnedHTTPSConnection(target, vetted, 1.0)
            connection.connect()
        self.assertEqual(raw.destination, ("8.8.8.8", 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname="example.com")

    def test_fallback_uses_only_pre_vetted_addresses_without_dns_again(self):
        target = _canonicalize_target("https://example.com/directory", ["example.com"], ["/directory"])
        first = VettedAddress(socket.AF_INET, ("1.1.1.1", 443), "1.1.1.1")
        second = VettedAddress(socket.AF_INET, ("8.8.8.8", 443), "8.8.8.8")
        with patch("lead_collector.fetch_child._resolve_all_public", return_value=(first, second)) as resolver, patch("lead_collector.fetch_child._request_once", side_effect=(OSError("connect"), _HopResponse(204, {}, b""))) as request_once:
            document = _fetch_target(target, 10**30)
        self.assertEqual(document.status, "empty")
        resolver.assert_called_once_with(target, 10**30)
        self.assertEqual([call.args[1] for call in request_once.call_args_list], [first, second])

    def test_deadline_is_checked_before_each_child_stage(self):
        target = _canonicalize_target("https://example.com/directory", ["example.com"], ["/directory"])
        with patch("lead_collector.fetch_child.time.monotonic_ns", return_value=10):
            document = _fetch_target(target, 10)
        self.assertEqual(document.status, "timeout")
        self.assertEqual(document.error["code"], "FETCH_TIMEOUT")

    def test_parent_kills_owned_child_when_the_child_stalls(self):
        with patch("lead_collector.transport.subprocess.Popen", return_value=_StuckProcess(b"")), patch("lead_collector.transport.os.killpg") as killpg:
            document = fetch_page(self._request())
        self.assertEqual(document.status, "timeout")
        killpg.assert_called_once_with(os.getpid(), unittest.mock.ANY)

    def test_parent_stops_owned_child_when_deadline_expires_before_communicate(self):
        process = _FakeProcess(b"")
        with patch("lead_collector.transport.subprocess.Popen", return_value=process), patch("lead_collector.transport.time.monotonic_ns", side_effect=(1, 1 + 15 * 1_000_000_000)), patch("lead_collector.transport.os.killpg") as killpg:
            document = fetch_page(self._request())
        self.assertEqual(document.status, "timeout")
        killpg.assert_called_once_with(os.getpid(), unittest.mock.ANY)

    def test_body_limit_streams_without_content_length_allocation(self):
        connection = unittest.mock.MagicMock()
        connection.sock = _FakeSocket()
        with self.assertRaises(FetchFailure) as caught:
            _read_body(connection, _StreamingResponse(), 10**30)
        self.assertEqual(caught.exception.code, "CONTENT_TRUNCATED")

    def test_http10_connection_close_response_keeps_owned_socket_readable(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        server.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\nContent-Length: 17\r\nConnection: close\r\n\r\n<html>safe</html>")
        server.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(client)

        class DetachingConnection:
            def __init__(self):
                self.sock = client

            def putrequest(self, *args, **kwargs):
                return None

            def putheader(self, *args, **kwargs):
                return None

            def endheaders(self):
                return None

            def getresponse(self):
                response.begin()
                self.sock = None
                return response

            def close(self):
                return None

        target = _canonicalize_target("http://example.com/directory", ["example.com"], ["/directory"])
        vetted = VettedAddress(socket.AF_INET, ("8.8.8.8", 80), "8.8.8.8")
        with patch("lead_collector.fetch_child._PinnedHTTPConnection", return_value=DetachingConnection()):
            result = _request_once(target, vetted, time.monotonic_ns() + 1_000_000_000)
        self.assertEqual(result.body, b"<html>safe</html>")

    def test_content_length_eof_does_not_touch_a_closed_owned_stream(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        server.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 17\r\nConnection: close\r\n\r\n<html>safe</html>")
        server.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(client)
        response.begin()

        class ClosingAwareSocket:
            def settimeout(self, timeout: float) -> None:
                if response.isclosed():
                    raise OSError(9, "Bad file descriptor")
                client.settimeout(timeout)

        self.assertEqual(_read_body(ClosingAwareSocket(), response, time.monotonic_ns() + 1_000_000_000), b"<html>safe</html>")

    def test_chunked_connection_close_eof_does_not_reconfigure_closed_socket(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n11\r\n<html>safe</html>\r\n0\r\n\r\n")
        server.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(client)
        response.begin()

        class ClosingAwareSocket:
            def settimeout(self, timeout: float) -> None:
                if response.isclosed():
                    raise OSError(9, "Bad file descriptor")
                client.settimeout(timeout)

        body = _read_body(ClosingAwareSocket(), response, time.monotonic_ns() + 1_000_000_000)
        self.assertEqual(body, b"<html>safe</html>")
        self.assertTrue(response.isclosed())

    def test_premature_content_length_eof_is_malformed(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        server.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 20\r\nConnection: close\r\n\r\n<html>safe</html>")
        server.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(client)
        response.begin()

        with self.assertRaises(FetchFailure) as caught:
            _read_body(client, response, time.monotonic_ns() + 1_000_000_000)
        self.assertEqual(caught.exception.code, "RESPONSE_MALFORMED")

    def test_redirect_limit_is_five_and_each_hop_remains_scoped(self):
        target = _canonicalize_target("https://example.com/directory", ["example.com"], ["/directory"])
        redirect = _HopResponse(302, {"location": "/directory"}, b"")
        with patch("lead_collector.fetch_child._fetch_hop", side_effect=(redirect,) * 6) as fetch_hop:
            document = _fetch_target(target, 10**30, self._request())
        self.assertEqual(document.status, "denied")
        self.assertEqual(document.error["code"], "REDIRECT_DENIED")
        self.assertEqual(fetch_hop.call_count, 6)

    def test_directory_redirect_preserves_the_final_network_request_target(self):
        target = _canonicalize_target("https://example.com/directory", ["example.com"], ["/directory"])
        redirect = _HopResponse(302, {"location": "/directory/"}, b"")
        complete = _HopResponse(200, {"content-encoding": "", "content-type": "text/html"}, b"<html>ok</html>")

        with patch("lead_collector.fetch_child._fetch_hop", side_effect=(redirect, complete)) as fetch_hop:
            document = _fetch_target(target, 10**30, self._request())

        self.assertEqual(document.status, "completed")
        self.assertEqual(document.url, "https://example.com/directory/")
        self.assertEqual(fetch_hop.call_args_list[1].args[0].request_target, "/directory/")

    def test_malformed_child_headers_remain_distinct_from_transport_failure(self):
        target = _canonicalize_target("https://example.com/directory", ["example.com"], ["/directory"])
        with patch("lead_collector.fetch_child._fetch_hop", side_effect=FetchFailure("RESPONSE_MALFORMED", "invalid content length")):
            document = _fetch_target(target, 10**30)
        self.assertEqual(document.status, "malformed")
        self.assertEqual(document.error["code"], "RESPONSE_MALFORMED")


if __name__ == "__main__":
    unittest.main()
