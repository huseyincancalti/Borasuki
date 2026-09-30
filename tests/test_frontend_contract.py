"""Static bridge/locale checks; visual acceptance belongs to the user."""

import ast
import json
import re
import unittest
from html.parser import HTMLParser

from borasuki.storage import ROOT


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.ids, self.keys, self.pages, self.literal_text = [], [], [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        for key, values in (("id", self.ids), ("data-i18n", self.keys), ("data-page", self.pages)):
            if key in attributes:
                values.append(attributes[key])

    def handle_data(self, data):
        if data.strip():
            self.literal_text.append(data.strip())


class FrontendContractTests(unittest.TestCase):
    def test_shared_help_and_workstation_structure(self):
        source = (ROOT / 'frontend/index.html').read_text(encoding='utf-8')
        document = Document(source)
        catalog = json.loads((ROOT / 'borasuki/locales/en.json').read_text(encoding='utf-8'))
        for target in re.findall(r'data-help-for="([^"]+)"', source):
            self.assertIn(target, document.ids)
        self.assertNotIn('sidebarNote', source)
        self.assertNotIn('data-i18n="ui.add_hint"', source)
        self.assertIn('class="jobWorkspace"', source)
        self.assertIn('id="sourceVideo"', source)
        self.assertNotIn('id="useSourcePosition"', source)
        self.assertIn('id="sourceRetry"', source)
        self.assertIn('id="batchPanel"', source)
        self.assertNotIn('id="batchFiles"', source)
        self.assertRegex(source, r'id="addMore"[^>]+data-i18n="nav.add"')
        for key in ('ui.help_for', 'ui.source_video', 'ui.browse', 'ui.color_help', 'ui.cancel_dialog'):
            self.assertIn(key, catalog)
        script = (ROOT / 'frontend/platform.js').read_text(encoding='utf-8')
        self.assertIn("panel.popover = 'auto'", script)
        self.assertIn("trigger.setAttribute('popovertarget', panel.id)", script)

    def test_static_selectors_locales_and_api_calls_exist(self):
        document = Document((ROOT / "frontend/index.html").read_text(encoding="utf-8"))
        script = '\n'.join(path.read_text(encoding='utf-8') for path in (ROOT / 'frontend').glob('*.js'))
        self.assertEqual(len(document.ids), len(set(document.ids)))
        for identifier in re.findall(r"(?:\$|\bel)\('([^']+)'\)", script):
            self.assertIn(identifier, document.ids)
        catalog = json.loads((ROOT / "borasuki/locales/en.json").read_text(encoding="utf-8"))
        for key in document.keys + re.findall(r"\bt\('([\w.]+)'(?:,|\))", script):
            self.assertIn(key, catalog)
        api = ast.parse((ROOT / "borasuki/app.py").read_text(encoding="utf-8"))
        methods = {method.name for cls in api.body if isinstance(cls, ast.ClassDef) and cls.name == "API"
                   for method in cls.body if isinstance(method, ast.FunctionDef)}
        for name in re.findall(r"\bcall\('([^']+)'", script):
            self.assertIn(name, methods)
        self.assertNotIn("processing", document.pages)
        for name in document.pages:
            self.assertIn("page-" + name, document.ids)

    def test_markup_prose_comes_from_catalog(self):
        document = Document((ROOT / 'frontend/index.html').read_text(encoding='utf-8'))
        allowed = {'Borasuki', 'Türkçe', 'English', 'MKV', 'MP4', '↑', '2×', '0', '1', '←', '→', '↔', '−', '+', '00:00.000', '100%'}
        self.assertFalse(set(document.literal_text) - allowed, set(document.literal_text) - allowed)


if __name__ == "__main__":
    unittest.main()
