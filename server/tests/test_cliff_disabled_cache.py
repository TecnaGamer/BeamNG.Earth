"""Cliff-off key arguments remain identical to the pinned upstream expressions."""
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import cliffsettings
import farrestore
import revisit


class DisabledKeysTest(unittest.TestCase):
    def test_road_key_arguments_match_upstream_without_placeholders(self):
        root = Path(__file__).resolve().parents[1]
        expected = json.loads((root / 'tests/fixtures/cliff_disabled_cache.json').read_text())['expressions']
        source = ast.parse((root / 'blockworker.py').read_text())
        for settings in (cliffsettings.CliffSettings(), cliffsettings.CliffSettings(False, False, 'high')):
            self.assertEqual(settings.cache_parts, ())
            self.assertEqual(settings.cache_options, {})
            for node in source.body:
                if not isinstance(node, ast.FunctionDef) or node.name not in expected:
                    continue
                call = next(n for n in ast.walk(node) if isinstance(n, ast.Call)
                            and ast.unparse(n.func) == 'revisit.key')
                # Expand the actual disabled configuration's zero-length splat.
                args = []
                for arg in call.args:
                    if isinstance(arg, ast.Starred) and ast.unparse(arg.value) == 'cliff_settings.cache_parts':
                        args.extend(ast.Constant(x) for x in settings.cache_parts)
                    else:
                        args.append(arg)
                got = ast.Call(func=call.func, args=args, keywords=call.keywords)
                self.assertEqual(ast.dump(got), ast.dump(ast.parse(expected[node.name], mode='eval').body))

    def test_finished_far_key_is_byte_identical_to_upstream(self):
        with patch.object(revisit, 'code_key', return_value='same-upstream-generation'):
            for settings in (cliffsettings.CliffSettings(), cliffsettings.CliffSettings(False, False, 'low')):
                with patch.object(cliffsettings, 'SETTINGS', settings):
                    for photo in (None, 'photo-digest'):
                        self.assertEqual(farrestore.key('tile', 256, 32, photo),
                                         revisit.key('farfin', 'tile', 256, 32))
            keys = set()
            for natural, cuts in ((False, False), (False, True), (True, False), (True, True)):
                with patch.object(cliffsettings, 'SETTINGS', cliffsettings.CliffSettings(natural, cuts)):
                    keys.add(farrestore.key('tile', 256, 32, 'photo-digest'))
            self.assertEqual(len(keys), 4)
