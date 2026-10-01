"""Check renderer imports in an isolated external Python process."""

import importlib
import sys
from pathlib import Path


def main(app_folder):
    root = Path(app_folder).resolve() / '_internal'
    if not (root / 'borasuki/__init__.py').is_file():
        raise RuntimeError('External renderer package is missing.')
    sys.path.insert(0, str(root))
    for name in ('storage', 'profile', 'source', 'enhance'):
        importlib.import_module(f'borasuki.{name}')
    for name, module in tuple(sys.modules.items()):
        if name == 'borasuki' or name.startswith('borasuki.'):
            if not Path(module.__file__).resolve().is_relative_to(root):
                raise RuntimeError(f'{name} was imported from outside the bundle.')
    if not (root / 'borasuki/notify.ps1').is_file():
        raise RuntimeError('Notification helper is missing.')
    print('External renderer imports passed (no source checkout).')


if __name__ == '__main__':
    main(sys.argv[1])
