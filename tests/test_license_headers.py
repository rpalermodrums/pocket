# SPDX-License-Identifier: AGPL-3.0-only
"""Source files name their license, except files whose exact bytes are recorded identities."""
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCES = ('.py', '.js', '.cjs', '.mjs', '.css', '.html')
# A header here would change a pinned decoder hash, a qualified model-runner profile or a natively
# accepted Max for Live device package. NOTICE lists these files and their license.
PINNED = {'src/pocket_music/audio_note_projection.py', 'src/pocket_music/audio_note_projection_v2.py',
          'src/pocket_music/audio_model_runner.py', 'src/pocket_music/audio_note_runner.py'}
PINNED_DIRECTORY = 'src/pocket_music/devices/'


def source_files():
    for folder in ('src', 'tests', 'site', 'examples'):
        for path in sorted((ROOT / folder).rglob('*')):
            relative = path.relative_to(ROOT).as_posix()
            if path.is_file() and path.suffix in SOURCES and '__pycache__' not in path.parts:
                yield relative, path


def test_source_files_start_with_their_license_identifier():
    missing = []
    for relative, path in source_files():
        if relative in PINNED or relative.startswith(PINNED_DIRECTORY):
            continue
        expected = 'MIT' if relative.startswith('examples/') else 'AGPL-3.0-only'
        if f'SPDX-License-Identifier: {expected}' not in ''.join(path.read_text().splitlines(True)[:2]):
            missing.append(relative)
    assert not missing, f'Add an SPDX-License-Identifier line to: {missing}'


def test_byte_pinned_files_stay_unlabeled():
    pinned = [(relative, path) for relative, path in source_files()
              if relative in PINNED or relative.startswith(PINNED_DIRECTORY)]
    assert {relative for relative, _ in pinned} >= PINNED
    assert any(relative.startswith(PINNED_DIRECTORY) for relative, _ in pinned)
    assert not [relative for relative, path in pinned if 'SPDX-License-Identifier' in path.read_text()]
