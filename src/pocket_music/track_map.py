"""Compatibility alias for :mod:`pocket_music.peek`.

Both import paths resolve to the same module, including private helpers and
monkeypatches. New integrations should use the canonical provider name.
"""
import sys
from importlib import import_module

sys.modules[__name__] = import_module(".peek", __package__)
