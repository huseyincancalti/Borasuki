"""Keep shared tokens readable and prevent page-specific control styling."""

import re
import unittest

from borasuki.storage import ROOT


def luminance(color):
    value = color.lstrip('#')
    if len(value) == 3:
        value = ''.join(char * 2 for char in value)
    channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))


class DesignTokenTests(unittest.TestCase):
    def test_text_and_control_contrast_in_both_themes(self):
        source = (ROOT / 'frontend/tokens.css').read_text(encoding='utf-8')
        colors = {}
        for selector in (':root', 'body.light'):
            block = re.search(re.escape(selector) + r'\s*\{([^}]+)\}', source)[1]
            colors.update(dict(re.findall(r'--([\w-]+):\s*(#[a-fA-F0-9]{3,6});', block)))
            pairs = [(text, surface, 4.5) for text in ('color-text', 'color-muted', 'color-danger', 'color-warning', 'color-success')
                     for surface in ('surface-app', 'surface-panel', 'surface-field')]
            pairs += [('color-on-primary', 'color-primary', 4.5), ('color-text', 'surface-selected', 4.5),
                      ('color-muted', 'surface-selected', 4.5), ('color-border', 'surface-field', 3),
                      ('color-accent', 'surface-panel', 3)]
            for foreground, background, minimum in pairs:
                light, dark = sorted([luminance(colors[foreground]), luminance(colors[background])], reverse=True)
                with self.subTest(theme=selector, foreground=foreground, background=background):
                    self.assertGreaterEqual((light + 0.05) / (dark + 0.05), minimum)

    def test_shared_styles_use_tokens_for_visual_decisions(self):
        styles = (ROOT / 'frontend/app.css').read_text(encoding='utf-8')
        self.assertNotRegex(styles, r':\s*#[a-fA-F0-9]{3,8}\b')
        self.assertNotRegex(styles, r'(?:padding|margin|gap|border-radius|font-size|line-height)\s*:[^;{}]*\dpx')
        self.assertIn(':focus-visible', styles)
        self.assertIn('prefers-reduced-motion:reduce', styles)
        self.assertIn('forced-colors:active', styles)
        for page in (ROOT / 'frontend').glob('*.html'):
            self.assertNotRegex(page.read_text(encoding='utf-8'), r'\sstyle\s*=')
