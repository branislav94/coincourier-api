from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import Mock, patch

import openverse_probe as probe


def response(status=200, payload=None, *, headers=None):
    result = Mock(status_code=status, headers=headers or {"Content-Type": "application/json"})
    result.json.return_value = payload if payload is not None else {"results": []}
    return result


class OpenverseProbeTests(unittest.TestCase):
    def setUp(self):
        settings = patch.multiple(
            probe, OPENVERSE_API_BASE_URL="https://relay.example/v1/",
            OPENVERSE_TOKEN_URL="https://relay.example/v1/auth_tokens/token/",
            OPENVERSE_HTTPS_PROXY="http://approved-proxy.example:8080",
            OPENVERSE_ACCESS_TOKEN="temporary-secret-token",
            OPENVERSE_CLIENT_ID="private-client-id", OPENVERSE_CLIENT_SECRET="private-client-secret",
        )
        settings.start()
        self.addCleanup(settings.stop)

    def test_direct_anonymous_ignores_configured_routes_and_credentials(self):
        session = Mock()
        session.request.return_value = response()
        records = probe.run_probes(path="direct", mode="anonymous", session=session)
        args, kwargs = session.request.call_args
        self.assertEqual(args, ("GET", "https://api.openverse.org/v1/images/"))
        self.assertEqual(kwargs["proxies"], {"http": "", "https": "", "all": ""})
        self.assertEqual(kwargs["headers"], {})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(records[0]["outcome"], "ok")
        self.assertEqual(records[0]["http_status"], 200)

    def test_configured_path_uses_explicit_approved_proxy_and_endpoint(self):
        session = Mock()
        session.request.return_value = response()
        record = probe.run_probes(path="configured", mode="anonymous", session=session)[0]
        self.assertEqual(session.request.call_args.args[1], "https://relay.example/v1/images/")
        self.assertEqual(session.request.call_args.kwargs["proxies"]["https"], "http://approved-proxy.example:8080")
        self.assertEqual(record["outcome"], "ok")

    def test_oauth_reports_presence_without_returning_or_printing_token(self):
        session = Mock()
        session.request.return_value = response(payload={"access_token": "returned-secret-token"})
        with patch.object(probe.requests, "Session", return_value=session), redirect_stdout(io.StringIO()) as output:
            status = probe.main(["--path", "direct", "--mode", "oauth"])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["token_received"])
        self.assertEqual(session.request.call_args.args[0], "POST")
        for secret in ("private-client-id", "private-client-secret", "temporary-secret-token", "returned-secret-token"):
            self.assertNotIn(secret, output.getvalue())

    def test_bearer_probe_skips_oauth_and_never_caches(self):
        session = Mock()
        session.request.return_value = response()
        with patch("image_search.cache.Path.write_text") as cache_write, patch("image_search.cache.Path.mkdir") as mkdir:
            records = probe.run_probes(path="configured", mode="bearer", session=session)
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(session.request.call_args.args[0], "GET")
        self.assertEqual(session.request.call_args.kwargs["headers"], {"Authorization": "Bearer temporary-secret-token"})
        self.assertNotIn("temporary-secret-token", json.dumps(records))
        cache_write.assert_not_called()
        mkdir.assert_not_called()

    def test_missing_credentials_or_bearer_are_explicit_skips(self):
        session = Mock()
        session.request.return_value = response()
        with patch.multiple(probe, OPENVERSE_CLIENT_ID="", OPENVERSE_CLIENT_SECRET="", OPENVERSE_ACCESS_TOKEN=""):
            records = probe.run_probes(path="direct", mode="all", session=session)
        # Only anonymous search is attempted; supply its fixture explicitly.
        self.assertEqual(records[0]["reason"], "missing_credentials")
        self.assertEqual(records[-1]["reason"], "missing_access_token")
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(records[1]["outcome"], "ok")

    def test_invalid_cli_arguments_never_echo_a_supplied_secret(self):
        with redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as stopped:
            probe.main(["--access-token", "accidental-secret-token"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertNotIn("accidental-secret-token", output.getvalue())

    def test_edge_diagnostics_exclude_html_cookies_ray_value_and_secrets(self):
        session = Mock()
        result = response(403, headers={
            "Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare",
            "CF-Ray": "private-ray-value", "Set-Cookie": "private-cookie",
        })
        result.json.side_effect = AssertionError("HTML must not be decoded")
        result.text = "<html>private-body private-client-id</html>"
        session.request.return_value = result
        records = probe.run_probes(path="direct", mode="oauth", session=session)
        self.assertEqual(records[0]["reason"], "edge_access_denied")
        self.assertTrue(records[0]["server_cloudflare"])
        self.assertTrue(records[0]["cf_ray_present"])
        result.json.assert_not_called()
        for forbidden in ("private-ray-value", "private-cookie", "private-body", "private-client-id", "private-client-secret"):
            self.assertNotIn(forbidden, json.dumps(records))

    def test_json_401_remains_authentication_failure(self):
        session = Mock()
        session.request.return_value = response(401, payload={"detail": "private response"})
        record = probe.run_probes(path="direct", mode="oauth", session=session)[0]
        self.assertEqual(record["reason"], "authentication_failed")
        self.assertEqual(record["http_status"], 401)
        self.assertNotIn("private", json.dumps(record))

    def test_invalid_configuration_and_transport_messages_are_not_exposed(self):
        session = Mock()
        with patch.object(probe, "OPENVERSE_HTTPS_PROXY", "https://private-user:private-password@proxy.example"):
            records = probe.run_probes(path="configured", mode="anonymous", session=session)
        self.assertEqual(records[0]["reason"], "invalid_configuration")
        session.request.assert_not_called()
        session.request.side_effect = RuntimeError("private transport details")
        records = probe.run_probes(path="direct", mode="anonymous", session=session)
        self.assertNotIn("private", json.dumps(records))


if __name__ == "__main__":
    unittest.main()
