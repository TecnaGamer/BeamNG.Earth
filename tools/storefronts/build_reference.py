"""Build the offline visual reference chart using the same sign renderer as the player."""
import base64
import io
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / "server"))

from storeidentity import CHAIN_GROUPS, SOURCES, STYLES
from whole_store import sign_font_scope, sign_image


STYLE_NAMES = {key: style["title"] for key, style in STYLES.items()}


@sign_font_scope()
def build():
    names = sorted({name for _, group in CHAIN_GROUPS for name in group}, key=str.casefold)
    rows = []
    for name in names:
        identity = SOURCES[name.casefold()]
        image = sign_image(identity, 5.25)
        image.thumbnail((315, 60))
        stream = io.BytesIO()
        image.save(stream, format="PNG", optimize=True)
        png = base64.b64encode(stream.getvalue()).decode()
        forms = ", ".join(form.replace("_", " ") for form in identity.variants)
        rows.append(
            f'<tr><th scope="row">{escape(name)}</th><td>{escape(identity.label)}</td>'
            f'<td><img width="210" height="40" alt="" src="data:image/png;base64,{png}"></td>'
            f'<td>{escape(STYLE_NAMES.get(identity.style, identity.style))}<small>{escape(forms)}</small></td></tr>'
        )
    page = HEADER.replace("{{count}}", str(len(names)))
    return page + "\n".join(rows) + "</tbody></table></main></body></html>\n"


HEADER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BeamNG.Earth · Fictional storefront reference</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f4ee;color:#203334;font:16px/1.45 "Segoe UI",sans-serif}
main{max-width:1100px;margin:auto;padding:40px 28px}h1{font-size:clamp(28px,4vw,46px);letter-spacing:-.04em;margin:0}
p{max-width:76ch;margin:8px 0 26px}table{border-collapse:collapse;width:100%;background:#fff}
th,td{padding:9px 14px;border-bottom:1px solid #d6dfd9;text-align:left;vertical-align:middle}
thead th{background:#203c3c;color:white;position:sticky;top:0;font-size:14px}
tbody th{font-weight:600}tbody tr:nth-child(even){background:#f5f8f4}td img{display:block;max-width:100%;height:auto}
small{display:block;color:#48605b;font-size:.83em}
@media(max-width:620px){main{padding:20px 6px}table{font-size:13px}th,td{padding:6px 4px}td img{min-width:86px}}
@media print{@page{margin:12mm}main{max-width:none;padding:0}thead{display:table-header-group}
thead th{position:static}tr{break-inside:avoid}th,td{padding:5px 8px}body{font-size:11pt}*{print-color-adjust:exact}}
</style></head><body><main><h1>Fictional storefront reference</h1>
<p>{{count}} mapped chains, alphabetized by real name. Each sample is drawn by the game’s sign renderer.
The last column shows the building family and its available entrances. CVS and Walgreens share a pharmacy treatment.</p>
<table><thead><tr><th>Map reference</th><th>Fictional name</th><th>In-game sign sample</th>
<th>Building and entrances</th></tr></thead><tbody>
"""


if __name__ == "__main__":
    (ROOT / "catalog/storefront-reference.html").write_text(build(), encoding="utf-8")
