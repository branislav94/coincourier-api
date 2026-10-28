"""Offline fixtures for approved Openverse routing and safe failure handling."""

from __future__ import annotations

import logging
import unittest
from unittest.mock import Mock, patch

import requests

from image_search.cache import CachedHttpClient
from image_search.provider import ProviderUnavailable
from image_search.providers.openverse import OpenverseProvider
from image_search.providers.pexels import PexelsProvider
from image_search.providers.pixabay import PixabayProvider
from image_search.providers.wikimedia import WikimediaProvider
from image_search.selection import search_images


OFFICIAL_BASE = "https://api.openverse.org/v1/"
OFFICIAL_TOKEN = OFFICIAL_BASE + "auth_tokens/token/"
DIRECT_PROXIES = {"http": "", "https": "", "all": ""}


def response(status=200, payload=None, *, headers=None):
    item = Mock(status_code=status, headers=headers or {})
    item.json.return_value = {"results": []} if payload is None else payload
    # Failure diagnostics must never inspect response text/content.
    type(item).text = property(lambda _self: (_ for _ in ()).throw(AssertionError("body read")))
    return item


def provider_fixture(responses, **overrides):
    session = Mock()
    session.proxies = {"https": "https://other-provider-proxy.example"}
    session.trust_env = True
    session.request.side_effect = responses
    client = CachedHttpClient(session=session, cache_hours=0, max_retries=0)
    options = {
        "client": client, "client_id": None, "client_secret": None,
        "api_base_url": OFFICIAL_BASE, "token_url": OFFICIAL_TOKEN,
        "https_proxy": "", "access_token": "", "enabled": True,
    }
    options.update(overrides)
    return OpenverseProvider(**options), client, session


class OpenverseTransportTests(unittest.TestCase):
    def test_missing_credentials_use_direct_anonymous_official_api(self):
        provider, _client, session = provider_fixture([response()])
        self.assertEqual(provider.search("bitcoin"), [])
        args, kwargs = session.request.call_args
        self.assertEqual(args, ("GET", OFFICIAL_BASE + "images/"))
        self.assertEqual(kwargs["headers"], {})
        self.assertEqual(kwargs["proxies"], DIRECT_PROXIES)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["params"]["q"], "bitcoin")

    def test_oauth_credentials_get_token_then_send_bearer(self):
        provider, client, session = provider_fixture([
            response(payload={"access_token": "oauth-access-token"}), response(), response(),
        ], client_id="configured-client-id", client_secret="configured-client-secret")
        with patch.object(client, "_write_cache") as write:
            provider.search("bitcoin")
            provider.search("ethereum")
        first, second, third = session.request.call_args_list
        self.assertEqual(first.args, ("POST", OFFICIAL_TOKEN))
        self.assertEqual(first.kwargs["data"], {
            "client_id": "configured-client-id", "client_secret": "configured-client-secret",
            "grant_type": "client_credentials",
        })
        self.assertEqual(second.kwargs["headers"], {"Authorization": "Bearer oauth-access-token"})
        self.assertEqual(third.kwargs["headers"], second.kwargs["headers"])
        self.assertFalse(first.kwargs["allow_redirects"])
        self.assertNotIn("oauth-access-token", repr(write.call_args_list))

    def test_static_token_skips_oauth_acquisition_and_is_not_cached(self):
        provider, client, session = provider_fixture(
            [response()], access_token="temporary-diagnostic-token",
            client_id="configured-id", client_secret="configured-secret",
        )
        with patch.object(client, "_write_cache") as write:
            provider.search("bitcoin")
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(session.request.call_args.args[0], "GET")
        self.assertEqual(session.request.call_args.kwargs["headers"], {
            "Authorization": "Bearer temporary-diagnostic-token",
        })
        self.assertNotIn("temporary-diagnostic-token", repr(write.call_args))

    def test_partial_credentials_fail_without_anonymous_request(self):
        for credentials in ({"client_id": "id"}, {"client_secret": "secret"}):
            with self.subTest(credentials=tuple(credentials)):
                provider, _client, session = provider_fixture([], **credentials)
                for query in ("bitcoin", "ethereum"):
                    with self.assertRaises(ProviderUnavailable) as failure:
                        provider.search(query)
                    self.assertEqual(failure.exception.reason, "authentication_failed")
                session.request.assert_not_called()

    def test_oauth_json_401_is_sticky_authentication_failure(self):
        provider, _client, session = provider_fixture([
            response(401, {"error": "invalid_client", "detail": "secret-body"},
                     headers={"Content-Type": "application/json"}),
        ], client_id="secret-id", client_secret="secret-password")
        for query in ("bitcoin", "ethereum"):
            with self.assertRaises(ProviderUnavailable) as failure:
                provider.search(query)
            self.assertEqual(failure.exception.reason, "authentication_failed")
            self.assertEqual(failure.exception.status_code, 401)
            self.assertNotIn("secret", str(failure.exception))
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(session.request.call_args.args[0], "POST")

    def test_invalid_successful_token_payload_never_searches_anonymously(self):
        for payload in ({}, {"access_token": ""}, {"access_token": None},
                        {"access_token": "bad\r\nAuthorization: leaked"},
                        {"error": "invalid_client", "access_token": "not-valid"}):
            with self.subTest(payload_keys=tuple(payload)):
                provider, _client, session = provider_fixture(
                    [response(payload=payload)], client_id="id", client_secret="secret",
                )
                for _ in range(2):
                    with self.assertRaises(ProviderUnavailable) as failure:
                        provider.search("bitcoin")
                    self.assertEqual(failure.exception.reason, "authentication_failed")
                self.assertEqual(session.request.call_count, 1)

    def test_html_403_is_edge_denial_for_oauth_and_anonymous_search(self):
        headers = {
            "Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare",
            "CF-Ray": "do-not-print-raw-ray", "Set-Cookie": "private-cookie",
        }
        for oauth in (False, True):
            with self.subTest(oauth=oauth):
                denied = response(403, {"html": "private-challenge-body"}, headers=headers)
                provider, client, session = provider_fixture(
                    [denied],
                    **({"client_id": "private-id", "client_secret": "private-secret"} if oauth else {}),
                )
                with patch.object(client, "_write_cache") as write:
                    with self.assertRaises(ProviderUnavailable) as failure:
                        provider.search("bitcoin")
                error = failure.exception
                self.assertEqual(error.reason, "edge_access_denied")
                self.assertEqual(error.status_code, 403)
                self.assertEqual(error.content_type, "text/html")
                self.assertTrue(error.server_cloudflare)
                self.assertTrue(error.cf_ray_present)
                for private in ("private", "do-not-print-raw-ray", "Set-Cookie", "Authorization"):
                    self.assertNotIn(private, str(error))
                denied.json.assert_not_called()
                write.assert_not_called()

    def test_custom_api_base_and_token_urls_are_both_respected(self):
        provider, _client, session = provider_fixture([
            response(payload={"access_token": "token"}), response(),
        ], api_base_url="https://approved-relay.example/openverse/v1",
            token_url="https://approved-auth.example/token/", client_id="id", client_secret="secret")
        provider.search("bitcoin")
        self.assertEqual(session.request.call_args_list[0].args[1], "https://approved-auth.example/token/")
        self.assertEqual(session.request.call_args_list[1].args[1], "https://approved-relay.example/openverse/v1/images/")

    def test_trusted_proxy_is_request_scoped_for_oauth_and_search(self):
        for proxy in ("http://approved-proxy.example:3128", "https://approved-proxy.example:8443"):
            with self.subTest(proxy_scheme=proxy.partition(":")[0]):
                provider, _client, session = provider_fixture([
                    response(payload={"access_token": "token"}), response(),
                ], client_id="id", client_secret="secret", https_proxy=proxy)
                provider.search("bitcoin")
                for call in session.request.call_args_list:
                    self.assertEqual(call.kwargs["proxies"], {"http": "", "https": proxy, "all": ""})
                    self.assertFalse(call.kwargs["allow_redirects"])
                self.assertEqual(session.proxies, {"https": "https://other-provider-proxy.example"})
                self.assertTrue(session.trust_env)

    def test_other_providers_do_not_receive_openverse_proxy_options(self):
        provider, client, session = provider_fixture([
            response(), response(payload={"photos": []}), response(payload={"hits": []}),
            response(payload={"batchcomplete": True}),
        ], https_proxy="https://approved-proxy.example")
        provider.search("bitcoin")
        PexelsProvider(client=client, api_key="fixture-key").search("bitcoin")
        PixabayProvider(client=client, api_key="fixture-key").search("bitcoin")
        WikimediaProvider(client=client, enabled=True).search("bitcoin")
        for call in session.request.call_args_list[1:]:
            self.assertNotIn("proxies", call.kwargs)
            self.assertNotIn("allow_redirects", call.kwargs)
        self.assertEqual(session.proxies, {"https": "https://other-provider-proxy.example"})

    def test_direct_request_overrides_ambient_proxy_without_changing_session(self):
        provider, _client, _session = provider_fixture([])
        real_session = requests.Session()
        real_session.proxies = {"https": "https://shared-proxy.example"}
        with patch.dict("os.environ", {
            "HTTPS_PROXY": "https://ambient-proxy.example", "ALL_PROXY": "https://ambient-all.example",
        }):
            settings = real_session.merge_environment_settings(
                OFFICIAL_BASE + "images/", dict(provider.request_proxies), False, True, None,
            )
        self.assertEqual(settings["proxies"]["https"], "")
        self.assertEqual(real_session.proxies, {"https": "https://shared-proxy.example"})

    def test_endpoint_and_proxy_validation_is_sanitized_and_sticky(self):
        invalid = (
            {"api_base_url": "http://insecure.example/v1/"},
            {"api_base_url": "https://private-user:private-secret@relay.example/v1/"},
            {"api_base_url": "https://relay.example/v1/?token=private-token"},
            {"token_url": "https://relay.example/token/#private-token"},
            {"https_proxy": "http://private-user:private-secret@proxy.example"},
            {"https_proxy": "socks5://proxy.example"},
        )
        for options in invalid:
            with self.subTest(setting=tuple(options)):
                provider, _client, session = provider_fixture([], **options)
                for _ in range(2):
                    with self.assertRaises(ProviderUnavailable) as failure:
                        provider.search("bitcoin")
                    self.assertEqual(failure.exception.reason, "invalid_configuration")
                    self.assertNotIn("private", str(failure.exception))
                session.request.assert_not_called()

    def test_custom_endpoint_cache_is_isolated_and_contains_no_credentials(self):
        provider, client, session = provider_fixture([
            response(), response(),
        ], access_token="private-diagnostic-token")
        cached = {}
        with (
            patch.object(client, "_read_cache", side_effect=lambda path: cached.get(path)),
            patch.object(client, "_write_cache", side_effect=lambda path, data: cached.update({path: data})),
        ):
            provider.search("bitcoin")
            alternate = OpenverseProvider(
                client=client, client_id=None, client_secret=None, access_token="private-diagnostic-token",
                api_base_url="https://relay.example/v1/", token_url=OFFICIAL_TOKEN, https_proxy="",
            )
            alternate.search("bitcoin")
        self.assertEqual(len(cached), 2)
        self.assertEqual(session.request.call_count, 2)
        self.assertNotIn("private", repr(cached))

    def test_other_provider_cache_filenames_remain_compatible(self):
        client = CachedHttpClient(session=Mock())
        legacy_names = {
            "pexels": "c0d09c757daf56c06dcad6f8db319cce89d90e47fbdff24099d2c1d934f4c7e3.json",
            "pixabay": "e0f49e8380a980b24c253b8ff8dee2df975b23eac22ace0c8ebeb48fd62c9ff8.json",
            "wikimedia": "aeb8d8250d2e41353f2c1a5010b5ad3f8418f16c63a6f32339cca06f02195777.json",
        }
        for provider, legacy_name in legacy_names.items():
            with self.subTest(provider=provider), patch.object(client, "_read_cache", return_value={"cached": True}) as read:
                self.assertEqual(client.get_json(provider, "bitcoin", "https://provider.example/search"), {"cached": True})
                self.assertEqual(read.call_args.args[0].name, legacy_name)
        client.session.request.assert_not_called()

    def test_proxy_transport_exception_cannot_leak_credentials(self):
        provider, _client, _session = provider_fixture([
            requests.exceptions.ProxyError("http://private-user:private-password@proxy.example token=private-token"),
        ], https_proxy="https://approved-proxy.example")
        with self.assertRaises(ProviderUnavailable) as failure:
            provider.search("bitcoin")
        self.assertEqual(failure.exception.reason, "connection_failure")
        self.assertNotIn("private", str(failure.exception))

    def test_edge_failure_logging_is_sanitized_and_generation_remains_blocked(self):
        provider, _client, _session = provider_fixture([
            response(403, {"private-body": "private-secret"}, headers={
                "Content-Type": "text/html; charset=private-secret", "Server": "cloudflare",
                "CF-Ray": "private-ray", "Set-Cookie": "private-cookie",
            }),
        ], client_id="private-id", client_secret="private-secret")
        with self.assertLogs("image_search.selection", level=logging.INFO) as logs:
            result = search_images({"id": 123, "title": "Bitcoin news"}, providers=[provider],
                                   local_usage=[], recent_wp_usage=[])
        self.assertIsNone(result.candidate)
        self.assertFalse(result.all_available_providers_exhausted)
        self.assertFalse(result.generation_allowed)
        self.assertIn("edge_access_denied", result.provider_failures["openverse"])
        self.assertNotIn("private", " ".join(logs.output))

    def test_diagnostic_headers_cannot_retain_arbitrary_values(self):
        error = ProviderUnavailable("openverse", "edge_access_denied", status_code=403,
                                    content_type="private-content-type")
        self.assertIsNone(error.content_type)
        self.assertNotIn("private", str(error))


if __name__ == "__main__":
    unittest.main()
