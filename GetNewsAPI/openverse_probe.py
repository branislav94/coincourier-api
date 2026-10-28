"""Explicit operator-only Openverse reachability checks; no DB or WordPress I/O.

Run separately from offline tests. Credentials are read from runtime configuration,
never command-line arguments. Responses and tokens are never written to disk.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import requests

from config import (
    OPENVERSE_ACCESS_TOKEN,
    OPENVERSE_API_BASE_URL,
    OPENVERSE_CLIENT_ID,
    OPENVERSE_CLIENT_SECRET,
    OPENVERSE_HTTPS_PROXY,
    OPENVERSE_TOKEN_URL,
)
from image_search.cache import CachedHttpClient
from image_search.provider import ProviderUnavailable
from image_search.providers.openverse import OpenverseProvider


OFFICIAL_API_BASE_URL = "https://api.openverse.org/v1/"
OFFICIAL_TOKEN_URL = "https://api.openverse.org/v1/auth_tokens/token/"


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Unknown arguments can include an accidentally supplied credential.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid probe arguments; use --help. Supply credentials through runtime configuration only.\n")


class _DiagnosticSession:
    def __init__(self, session: Any) -> None:
        self.session = session
        self.last: dict[str, Any] = {}

    def request(self, *args: Any, **kwargs: Any) -> Any:
        response = self.session.request(*args, **kwargs)
        self.last = {
            "http_status": int(response.status_code),
            **CachedHttpClient._safe_response_diagnostics(response),
        }
        return response


def run_probes(*, path: str, mode: str, query: str = "bitcoin", session: Any = None) -> list[dict[str, Any]]:
    """Return allowlisted diagnostics only; injected sessions support offline tests."""
    http_session = session if session is not None else requests.Session()
    transport = _DiagnosticSession(http_session)
    records: list[dict[str, Any]] = []
    paths = ("direct", "configured") if path == "both" else (path,)
    operations = ("oauth", "anonymous", "bearer") if mode == "all" else (mode,)
    try:
        for selected_path in paths:
            for operation in operations:
                record: dict[str, Any] = {"provider": "openverse", "path": selected_path, "operation": operation}
                transport.last = {}
                if operation == "oauth" and not OPENVERSE_CLIENT_ID and not OPENVERSE_CLIENT_SECRET:
                    records.append({**record, "outcome": "skipped", "reason": "missing_credentials"})
                    continue
                if operation == "bearer" and not OPENVERSE_ACCESS_TOKEN:
                    records.append({**record, "outcome": "skipped", "reason": "missing_access_token"})
                    continue
                try:
                    # A single uncached request per operation: no token/cache
                    # persistence, automatic egress failover or repeated probing.
                    client = CachedHttpClient(session=transport, cache_hours=0, max_retries=0)
                    provider = OpenverseProvider(
                        client=client, enabled=True, per_page=1,
                        client_id=OPENVERSE_CLIENT_ID if operation == "oauth" else None,
                        client_secret=OPENVERSE_CLIENT_SECRET if operation == "oauth" else None,
                        access_token=OPENVERSE_ACCESS_TOKEN if operation == "bearer" else None,
                        api_base_url=OFFICIAL_API_BASE_URL if selected_path == "direct" else OPENVERSE_API_BASE_URL,
                        token_url=OFFICIAL_TOKEN_URL if selected_path == "direct" else OPENVERSE_TOKEN_URL,
                        https_proxy="" if selected_path == "direct" else OPENVERSE_HTTPS_PROXY,
                    )
                    if operation == "oauth":
                        headers = provider._headers()
                        record["token_received"] = bool(headers.get("Authorization"))
                    else:
                        items = provider.search(query)
                        record["result_count"] = provider.last_search_stats["raw"]
                        record["normalized_count"] = len(items)
                    record.update(transport.last)
                    record["outcome"] = "ok"
                except ProviderUnavailable as exc:
                    record.update(transport.last)
                    record.update(outcome="failed", reason=exc.reason)
                    if exc.status_code is not None:
                        record["http_status"] = exc.status_code
                    if exc.content_type is not None:
                        record.update(content_type=exc.content_type, server_cloudflare=exc.server_cloudflare,
                                      cf_ray_present=exc.cf_ray_present)
                except Exception:
                    # Never render exception messages: transport/configuration
                    # exceptions may contain credentials, endpoints or headers.
                    record.update(outcome="failed", reason="probe_error")
                records.append(record)
    finally:
        if session is None:
            http_session.close()
    return records


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(description="Read-only Openverse probes with sanitized JSON output.")
    parser.add_argument("--path", choices=("direct", "configured", "both"), default="direct")
    parser.add_argument("--mode", choices=("anonymous", "oauth", "bearer", "all"), default="anonymous")
    parser.add_argument("--query", default="bitcoin")
    args = parser.parse_args(argv)
    if not args.query.strip() or len(args.query) > 200:
        parser.error("query must contain between 1 and 200 characters")
    records = run_probes(path=args.path, mode=args.mode, query=args.query)
    for record in records:
        print(json.dumps(record, sort_keys=True))
    if any(record["outcome"] == "failed" for record in records):
        return 1
    return 0 if any(record["outcome"] == "ok" for record in records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
