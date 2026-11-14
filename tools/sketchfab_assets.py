#!/usr/bin/env python3
"""Read Sketchfab model metadata for human licensing review; never download assets.

Authentication is read only from SKETCHFAB_API_TOKEN. See docs/SKETCHFAB_ASSETS.md.
"""
import argparse
import datetime
import http.client
import json
import os
import re
import sys
import time
from urllib.parse import quote, urlencode, urlsplit

HOST = "api.sketchfab.com"
MAX_BYTES = 2 * 1024 * 1024
UID = re.compile(r"[a-fA-F0-9]{32}\Z")


class PreflightError(Exception):
    """Safe, fixed diagnostic text suitable for terminal output."""


def model_uid(value):
    if not isinstance(value, str) or not UID.fullmatch(value):
        raise PreflightError("Model UID must contain exactly 32 hexadecimal characters.")
    return value.lower()


def search_query(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120 or any(ord(c) < 32 for c in value):
        raise PreflightError("Search text must contain 1 to 120 characters without control characters.")
    return value.strip()


def clean_text(value, limit=500):
    if not isinstance(value, str):
        return None
    return "".join(c for c in value[:limit] if ord(c) >= 32 and ord(c) != 127)


def license_url(value):
    """Preserve only public license links, never credentials, query strings or arbitrary hosts."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or parsed.hostname not in
                ("creativecommons.org", "www.creativecommons.org", "sketchfab.com") or
                parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
            return None
        return "https://" + parsed.hostname + quote(parsed.path, safe="/%-._~")[:500]
    except ValueError:
        return None


def redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {k: redact(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    return value


class Client:
    def __init__(self, timeout=15):
        self._token = os.environ.get("SKETCHFAB_API_TOKEN", "")
        if not self._token:
            raise PreflightError("Set SKETCHFAB_API_TOKEN in this process environment before running the tool.")
        if not re.fullmatch(r"[A-Za-z0-9._~-]{8,512}", self._token):
            raise PreflightError("SKETCHFAB_API_TOKEN has an invalid format.")
        if not 1 <= timeout <= 30:
            raise PreflightError("Timeout must be between 1 and 30 seconds.")
        self.timeout = timeout

    def _get(self, path):
        # Only callers below construct paths. Neither API pagination links nor model URLs are followed.
        if not (re.fullmatch(r"/v3/models/[a-f0-9]{32}", path) or path.startswith("/v3/search?")):
            raise PreflightError("Unsupported metadata endpoint.")
        connection = http.client.HTTPSConnection(HOST, timeout=self.timeout)
        try:
            deadline = time.monotonic() + self.timeout
            connection.request("GET", path, headers={"Authorization": "Token " + self._token,
                               "Accept": "application/json", "User-Agent": "BeamNG-Earth-Asset-Preflight/1"})
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise PreflightError("Sketchfab redirected the request; credentials were not forwarded.")
            if response.status == 429:
                raise PreflightError("Sketchfab rate limit reached. Try again later; no automatic retry was made.")
            if response.status in (401, 403):
                raise PreflightError("Sketchfab denied access. Check the token and account permissions.")
            if response.status != 200:
                raise PreflightError("Sketchfab returned an unsuccessful response.")
            if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise PreflightError("Sketchfab returned an unexpected content type.")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                raise PreflightError("Sketchfab metadata response exceeds the size limit or has an invalid length.")
            body = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise PreflightError("Sketchfab metadata request timed out.")
                if connection.sock is not None:
                    connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise PreflightError("Sketchfab metadata response exceeds the size limit.")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise PreflightError("Sketchfab metadata response has an unexpected structure.")
            return redact(data, self._token)    # redact before any field truncation or normalization
        except PreflightError:
            raise
        except (OSError, http.client.HTTPException):
            raise PreflightError("Sketchfab metadata request failed or timed out.") from None
        except (ValueError, RecursionError):
            raise PreflightError("Sketchfab returned invalid JSON metadata.") from None
        finally:
            connection.close()

    def model(self, uid):
        uid = model_uid(uid)
        data = self._get("/v3/models/" + uid)
        if model_uid(data.get("uid")) != uid:
            raise PreflightError("Sketchfab returned metadata for an unexpected model.")
        return redact(preflight(data), self._token)

    def search(self, query, limit=10):
        query = search_query(query)
        if not isinstance(limit, int) or not 1 <= limit <= 24:
            raise PreflightError("Result limit must be between 1 and 24.")
        data = self._get("/v3/search?" + urlencode({"type": "models", "q": query, "downloadable": "true"}))
        results = data.get("results")
        if not isinstance(results, list):
            raise PreflightError("Sketchfab search response has an unexpected structure.")
        return redact({"query": query, "results": [preflight(item, search=True) for item in results[:limit]],
                       "more_results_available": bool(data.get("next")) or len(results) > limit,
                       "pagination": "first page only; no pagination URLs followed"}, self._token)


def preflight(data, search=False):
    if not isinstance(data, dict):
        raise PreflightError("Sketchfab model metadata has an unexpected structure.")
    uid = model_uid(data.get("uid"))
    author = data.get("user") if isinstance(data.get("user"), dict) else {}
    raw_license = data.get("license")
    lic = raw_license if isinstance(raw_license, dict) else {"label": raw_license}
    label = clean_text(lic.get("label"))
    slug = clean_text(lic.get("slug"), 100)
    link = license_url(lic.get("url"))
    downloadable = data.get("isDownloadable") is True
    status = "manual-review-required"
    reasons = ["Download entitlement, intended use, attribution and redistribution permission are not verified."]
    if not downloadable:
        status = "blocked-not-downloadable"
        reasons.append("API metadata does not identify this model as downloadable.")
    elif not link or not (label or slug):
        status = "blocked-license-incomplete"
        reasons.append("A usable license label and public license URL are required for review.")
    def count(name):
        value = data.get(name)
        return value if type(value) is int and value >= 0 else None
    return {"uid": uid, "name": clean_text(data.get("name")),
            "model_url": "https://sketchfab.com/3d-models/" + uid,
            "author": {"username": clean_text(author.get("username")),
                       "display_name": clean_text(author.get("displayName"))},
            "license": {"label": label, "slug": slug, "url": link},
            "is_downloadable": downloadable, "face_count": count("faceCount"),
            "vertex_count": count("vertexCount"), "review_status": status, "review_reasons": reasons,
            "download_authorized": False, "redistribution_authorized": False,
            "runtime_imported": False,
            "metadata_source": "search-result" if search else "model-detail",
            "source_endpoint": "https://" + HOST + ("/v3/search" if search else "/v3/models/" + uid)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=15)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Inspect one page of downloadable-model metadata")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    model = commands.add_parser("model", help="Inspect a specific model UID")
    model.add_argument("uid")
    args = parser.parse_args(argv)
    try:
        client = Client(args.timeout)
        result = client.search(args.query, args.limit) if args.command == "search" else client.model(args.uid)
        report = {"schema_version": 1, "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "provider": "Sketchfab Data API v3", "purpose": "metadata and licensing preflight only",
                  "result": result}
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return 0
    except PreflightError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
