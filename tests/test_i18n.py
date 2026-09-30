import json
import unittest
from importlib.resources import files
from string import Formatter
from unittest.mock import patch

from borasuki.i18n import load_catalog, resolve_references, translate


class LocaleTests(unittest.TestCase):
    def test_raw_catalogs_have_matching_keys_and_placeholders(self):
        def unique_keys(pairs):
            self.assertEqual(len(pairs), len(dict(pairs)), 'Duplicate locale key')
            return dict(pairs)
        catalogs = [json.loads(files("borasuki").joinpath("locales", f"{language}.json")
                               .read_text(encoding="utf-8"), object_pairs_hook=unique_keys) for language in ("en", "tr")]
        english, turkish = catalogs
        self.assertEqual(english.keys(), turkish.keys())
        english, turkish = map(resolve_references, catalogs)
        for key in english:
            with self.subTest(key=key):
                self.assertTrue(english[key].strip())
                self.assertTrue(turkish[key].strip())
                fields = lambda text: {field for _, field, _, _ in Formatter().parse(text) if field}
                self.assertEqual(fields(english[key]), fields(turkish[key]))

    def test_context_menu_uses_requested_language(self):
        self.assertEqual(translate(load_catalog("tr"), "menu.upscale"), "Borasuki ile büyüt")
        self.assertEqual(translate(load_catalog("en"), "menu.upscale"), "Upscale with Borasuki")

    def test_completion_message_formats_unicode_filename(self):
        self.assertEqual(translate(load_catalog("tr"), "notification.completed.body",
                                   filename="Bölüm {01}.mkv"), "Bölüm {01}.mkv hazır.")

    def test_unsupported_locale_does_not_access_arbitrary_path(self):
        with self.assertLogs("borasuki.i18n", level="WARNING"):
            self.assertEqual(load_catalog("../../private"), load_catalog("en"))

    def test_missing_translation_falls_back_to_english(self):
        with patch("borasuki.i18n._read_locale", side_effect=[{"a": "English", "b": "Fallback"}, {"a": "Türkçe"}]), self.assertLogs("borasuki.i18n", level="WARNING"):
            self.assertEqual(load_catalog("tr"), {"a": "Türkçe", "b": "Fallback"})

    def test_missing_key_is_visible(self):
        with self.assertRaises(KeyError):
            translate(load_catalog("tr"), "missing.key")

    def test_missing_placeholder_is_visible(self):
        with self.assertRaises(KeyError):
            translate(load_catalog("en"), "notification.completed.body")

    def test_catalogs_are_independent(self):
        changed = load_catalog("tr")
        changed["app.name"] = "changed"
        self.assertEqual(load_catalog("tr")["app.name"], "Borasuki")

    def test_shared_names_resolve_without_touching_runtime_values(self):
        catalog = resolve_references({'nav.add':'Video Ekle', 'back':'{@nav.add}: {name}', 'alias':'{@back}'})
        self.assertEqual(catalog['alias'], 'Video Ekle: {name}')
        self.assertEqual(translate(catalog, 'alias', name='{@nav.add}.mp4'), 'Video Ekle: {@nav.add}.mp4')
        for language in ('tr', 'en'):
            catalog = load_catalog(language)
            self.assertIn(catalog['nav.add'], catalog['ui.back_options'])
            self.assertIn(catalog['ui.generate_preview'], catalog['preview.empty'])

    def test_broken_or_circular_references_fail(self):
        with self.assertRaises(KeyError):
            resolve_references({'label':'{@missing}'})
        with self.assertRaises(ValueError):
            resolve_references({'a':'{@b}', 'b':'{@a}'})

    def test_turkish_ui_does_not_mix_english_actions_and_screen_names(self):
        catalog = load_catalog('tr')
        forbidden = r'\b(?:Queue|History|Settings|Preview|Add Job|Color|Contrast|Brightness|Saturation|Resume|Retry|Paused|Completed|Cancelled|Failed|Edit|Custom|Denoise|Upscale)\b'
        for key, value in catalog.items():
            with self.subTest(key=key):
                self.assertNotRegex(value, forbidden)


if __name__ == "__main__":
    unittest.main()
