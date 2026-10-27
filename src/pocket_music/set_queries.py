# SPDX-License-Identifier: AGPL-3.0-only
"""Compatibility alias for :mod:`pocket_music.thread_queries`.

Both import paths resolve to the same module, including private helpers and
monkeypatches. New integrations should use the canonical provider name.
"""
import sys
from importlib import import_module

sys.modules[__name__] = import_module(".thread_queries", __package__)
