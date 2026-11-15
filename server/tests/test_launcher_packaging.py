"""Every repo-root module launcher.py imports (directly or through another root module, at the top or inside a function)
is in the source ZIP (tools/package.py FILES). launcher.py imports gameinstall unconditionally: a ZIP without it failed
at once with ModuleNotFoundError (Codex review of 904ac751). The .exe and the AppImage (tools/build_app.py) are
PyInstaller builds of launcher.py from the repo root: its analysis follows the same imports, so they need no list."""
import ast
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def package():
    spec = importlib.util.spec_from_file_location("bne_package", os.path.join(ROOT, "tools", "package.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def imported_names(path):
    """(module, from-name) pairs of every import in the file, wherever it is."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, None) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out += [(node.module, a.name) for a in node.names]
    return out


def root_closure(start="launcher.py"):
    """The repo files launcher.py needs: root modules (followed further) and tools/ modules."""
    need, todo = set(), [start]
    while todo:
        rel = todo.pop()
        if rel in need:
            continue
        need.add(rel)
        if not rel.endswith(".py") or "/" in rel:
            continue
        for mod, name in imported_names(os.path.join(ROOT, rel)):
            top = mod.split(".")[0]
            if top == "tools" and name and os.path.isfile(os.path.join(ROOT, "tools", name + ".py")):
                need.add(f"tools/{name}.py")
            elif os.path.isfile(os.path.join(ROOT, top + ".py")):
                todo.append(top + ".py")
    return need


class SourceZipTest(unittest.TestCase):
    def test_every_root_module_of_the_launcher_is_packed(self):
        pkg = package()
        packed = set(pkg.FILES) | set(pkg.TOOLS)
        need = root_closure()
        self.assertIn("gameinstall.py", need)
        self.assertEqual(sorted(need - packed), [])


if __name__ == "__main__":
    unittest.main()
