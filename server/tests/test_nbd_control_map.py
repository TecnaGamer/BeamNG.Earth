import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('nbd_control_map', Path(__file__).resolve().parents[2]/'tools/nbd_control_map.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class SourceDisplay(unittest.TestCase):
    def test_source_text_cannot_close_embedded_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/'in.json', Path(tmp)/'map.html'
            source.write_text(json.dumps({'features':[{'geometry':{'coordinates':[4.5,52.]},
                'properties':{'id':'123','goals_raw':['</script><script>alert(1)</script>']}}]}))
            control.build(source, output, '<review>')
            page = output.read_text(encoding='utf-8')
            self.assertNotIn('</script><script>alert', page)
            self.assertIn('&lt;review&gt;', page)
            self.assertIn('\\u003c/script\\u003e', page)
            self.assertNotIn('src="https://', page)

    def test_empty_inspection_layer_still_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/'in.json', Path(tmp)/'map.html'
            source.write_text('{"features":[]}')
            control.build(source, output, 'Empty')
            self.assertIn('const rows=[]', output.read_text())
