"""Localized launcher text from the catalogs shipped with the mod."""

import json
import locale
import os
from pathlib import Path
import re
import sys

_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
_CATALOGS = _ROOT / "mod/beamng_earth/ui/ui-vue/mods/beamngEarth/locales"
_PLACEHOLDER = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")


def _read(path):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def _tag(value):
    if not isinstance(value, str):
        return ""
    value = value.split(".", 1)[0].split("@", 1)[0].replace("_", "-").lower()
    return value if re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", value) else ""


def _system_locale():
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(name):
            return os.environ[name]
    if os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
            return buffer.value
    return locale.getlocale()[0]


def _parameters(value):
    if not isinstance(value, str) or not value.strip() or set(_PLACEHOLDER.sub("", value)) & set("{}"):
        return None
    return set(_PLACEHOLDER.findall(value))


class Translator:
    def __init__(self, language="en", root=_CATALOGS):
        manifest = _read(root / "catalogs.json")
        tags = {_tag(tag): tag for tag in manifest if _tag(tag)}
        for tag, meta in manifest.items():
            if _tag(tag) and isinstance(meta, dict) and isinstance(meta.get("aliases"), list):
                for alias in meta["aliases"]:
                    if _tag(alias):
                        tags.setdefault(_tag(alias), tag)
        chain = []
        parts = _tag(language).split("-")
        while parts:
            match = tags.get("-".join(parts))
            if match and match not in chain:
                chain.append(match)
            parts.pop()
        if "en" not in chain:
            chain.append("en")
        self.language = chain[0]
        catalogs = [_read(root / (tag + ".json")) for tag in chain]
        self.messages = {}
        for key, text in catalogs[-1].items():
            signature = _parameters(text)
            if not key.startswith("launcher.") or signature is None:
                continue
            self.messages[key] = next((catalog[key] for catalog in catalogs if
                                       _parameters(catalog.get(key)) == signature), text)

    def t(self, key, **params):
        value = self.messages.get(key, "—")

        def substitute(match):
            value = params.get(match[1])
            return str(value) if isinstance(value, (str, int, float)) else "—"

        return _PLACEHOLDER.sub(substitute, value)


_translator = None


def configure(language=None):
    global _translator
    _translator = Translator(language or os.environ.get("BNE_LANGUAGE") or _system_locale())


def t(key, **params):
    global _translator
    if _translator is None:
        _translator = Translator()
    return _translator.t(key, **params)
