"""Every direct HTTPS request of the server verifies certificates the way the map downloads do (netfetch's Windows
native verification, truststore): a player's report upload failed on Windows with "certificate verify failed:
certificate has expired" (2026-11-14) while the map downloads, which use it, worked; OpenSSL's own chain building
picked an outdated certificate from that machine's store."""
import ast
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)


class TlsContextTest(unittest.TestCase):
    def test_every_urlopen_passes_a_context(self):
        """urllib.request.urlopen in the server's modules always gets context= (ioprofiler's wrapper passes **kwargs
        through and its callers give the context)."""
        bad = []
        for name in sorted(os.listdir(SERVER)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(SERVER, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "urlopen":
                    target = ast.unparse(node.func.value)
                    if target not in ("urllib.request", "request"):
                        continue
                    kws = {k.arg for k in node.keywords}
                    if "context" not in kws and None not in kws:          # (None: a **kwargs pass-through)
                        bad.append(f"{name}:{node.lineno}")
        self.assertEqual(bad, [], "urlopen without the Windows-native TLS context (netfetch.windows_ssl_context)")

    def test_report_post_uses_the_windows_context(self):
        import reportsend
        sentinel = object()
        seen = {}

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b"{}"

        def fake(req, timeout=None, context=None):
            seen["context"] = context
            return Resp()
        with mock.patch.object(reportsend.netfetch, "windows_ssl_context", return_value=sentinel), \
                mock.patch.object(reportsend.urllib.request, "urlopen", fake):
            status, _ = reportsend.post("https://example.invalid/report", b"{}")
        self.assertEqual(status, 200)
        self.assertIs(seen["context"], sentinel)


if __name__ == "__main__":
    unittest.main()
