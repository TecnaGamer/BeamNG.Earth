"""Metadata preflight uses a fixed HTTPS origin and never downloads or approves assets."""
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import sketchfab_assets as assets

UID = "a" * 32
TOKEN = "fixture-token-not-a-secret"


def model(**changes):
    result = {"uid": UID, "name": "Public model", "isDownloadable": True,
              "user": {"username": "creator", "displayName": "Creator"},
              "license": {"label": "CC Attribution", "slug": "by",
                          "url": "https://creativecommons.org/licenses/by/4.0/"},
              "faceCount": 1000, "vertexCount": 800}
    result.update(changes)
    return result


class PreflightTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"SKETCHFAB_API_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        patch = mock.patch.object(assets.http.client, "HTTPSConnection")
        self.connection_type = patch.start()
        self.addCleanup(patch.stop)
        self.connection = self.connection_type.return_value
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.answer(model())

    def answer(self, value, headers=None):
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.response.getheader.side_effect = lambda k, default=None: self.headers.get(k, default)
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.response.read1.side_effect = io.BytesIO(body).read1

    def test_authenticated_model_metadata_without_download(self):
        result = assets.Client().model(UID)
        self.connection_type.assert_called_once_with("api.sketchfab.com", timeout=15)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("GET", "/v3/models/" + UID))
        self.assertEqual(kwargs["headers"]["Authorization"], "Token " + TOKEN)
        self.assertEqual(result["review_status"], "manual-review-required")
        self.assertFalse(result["download_authorized"])
        self.assertFalse(result["redistribution_authorized"])
        self.assertFalse(result["runtime_imported"])
        self.assertEqual(result["face_count"], 1000)
        self.connection.close.assert_called_once()

    def test_search_is_bounded_and_does_not_follow_next_url(self):
        self.answer({"results": [model(), model()], "next": "https://evil.invalid/?token=" + TOKEN})
        result = assets.Client().search("bridge & tower", 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["metadata_source"], "search-result")
        self.assertTrue(result["more_results_available"])
        self.connection.request.assert_called_once()
        path = self.connection.request.call_args.args[1]
        self.assertIn("q=bridge+%26+tower", path)
        self.assertIn("downloadable=true", path)
        self.assertNotIn("evil", json.dumps(result))

    def test_redirect_is_never_followed(self):
        self.response.status = 302
        self.headers["Location"] = "https://evil.invalid/" + TOKEN
        with self.assertRaises(assets.PreflightError) as caught:
            assets.Client().model(UID)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.connection_type.assert_called_once()
        self.connection.request.assert_called_once()
        self.response.read1.assert_not_called()

    def test_errors_do_not_echo_remote_body_or_exception(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                self.response.status = status
                self.answer(("error " + TOKEN).encode())
                with self.assertRaises(assets.PreflightError) as caught:
                    assets.Client().model(UID)
                self.assertNotIn(TOKEN, str(caught.exception))
        self.connection.request.side_effect = OSError("failed https://user:" + TOKEN + "@example.invalid")
        with self.assertRaises(assets.PreflightError) as caught:
            assets.Client().model(UID)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_metadata_echoing_token_is_redacted(self):
        self.answer(model(name="echo " + TOKEN))
        self.assertNotIn(TOKEN, json.dumps(assets.Client().model(UID)))

    def test_long_echoed_token_is_redacted_before_field_truncation(self):
        token = "s" * 512
        self.answer(model(name=token))
        with mock.patch.dict(os.environ, SKETCHFAB_API_TOKEN=token):
            self.assertEqual(assets.Client().model(UID)["name"], "[REDACTED]")

    def test_request_deadline_stops_slow_responses(self):
        with mock.patch.object(assets.time, "monotonic", side_effect=[0, 16]):
            with self.assertRaises(assets.PreflightError):
                assets.Client(timeout=15).model(UID)
        self.response.read1.assert_not_called()

    def test_bad_inputs_make_no_http_requests(self):
        for value in ("../download", "https://evil.invalid", "b" * 31, None, 32):
            with self.subTest(uid=value), self.assertRaises(assets.PreflightError):
                assets.Client().model(value)
        for query in ("", "x" * 121, "a\r\nb"):
            with self.subTest(query=query), self.assertRaises(assets.PreflightError):
                assets.Client().search(query)
        for limit in (0, 25):
            with self.assertRaises(assets.PreflightError):
                assets.Client().search("road", limit)
        self.connection_type.assert_not_called()

    def test_bad_token_and_timeout_are_rejected(self):
        for token in ("", "token\r\ninjected", "space token"):
            with mock.patch.dict(os.environ, SKETCHFAB_API_TOKEN=token), self.assertRaises(assets.PreflightError):
                assets.Client()
        for timeout in (0, 31, float("nan")):
            with self.assertRaises(assets.PreflightError):
                assets.Client(timeout)
        self.connection_type.assert_not_called()

    def test_response_size_is_bounded_with_and_without_length(self):
        for headers in ({"Content-Length": str(assets.MAX_BYTES + 1)}, {}):
            self.answer(b" " * (assets.MAX_BYTES + 1), headers)
            with self.assertRaises(assets.PreflightError):
                assets.Client().model(UID)

    def test_invalid_json_content_type_shape_and_uid_fail_cleanly(self):
        for value, headers in ((b"bad json", {}), ([], {}), (model(uid=None), {}),
                               (model(uid="b" * 32), {}), (model(), {"Content-Type": "text/html"})):
            with self.subTest(value=type(value).__name__):
                self.answer(value, headers)
                with self.assertRaises(assets.PreflightError):
                    assets.Client().model(UID)

    def test_unavailable_and_unknown_licenses_are_blocked(self):
        self.assertEqual(assets.preflight(model(isDownloadable=False))["review_status"], "blocked-not-downloadable")
        for license in (None, "CC BY", {"label": "CC BY", "url": "https://evil.invalid/cc-by"}):
            result = assets.preflight(model(license=license))
            self.assertEqual(result["review_status"], "blocked-license-incomplete")
            self.assertFalse(result["download_authorized"])

    def test_license_links_discard_credentials_queries_and_fragments(self):
        self.assertIsNone(assets.license_url("https://user:pass@creativecommons.org/licenses/by/4.0/"))
        self.assertEqual(assets.license_url("http://creativecommons.org/licenses/by/4.0/?secret=yes#x"),
                         "https://creativecommons.org/licenses/by/4.0/")

    def test_cli_returns_sanitized_json_and_error_exit(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(assets.main(["model", UID]), 0)
        self.assertEqual(json.loads(output.getvalue())["schema_version"], 1)
        self.assertNotIn(TOKEN, output.getvalue())
        with mock.patch.dict(os.environ, SKETCHFAB_API_TOKEN=""), redirect_stderr(errors):
            self.assertEqual(assets.main(["model", UID]), 2)


if __name__ == "__main__":
    unittest.main()
