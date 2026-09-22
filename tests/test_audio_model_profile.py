"""Pruned traversal equivalence fixtures; no optional runtime/model execution."""
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pocket_music.audio_model_runner import _stdlib_files, file_digest


def old_reference(root):
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if 'site-packages' in relative.parts or '__pycache__' in relative.parts:
            continue
        if path.is_file() and path.suffix in ('.py', '.so', '.dylib'):
            if len(result) >= 10000:
                raise ValueError('Standard library fingerprint exceeds bounds')
            result[str(relative)] = file_digest(path)
    return result


def file(root, name, data=b'synthetic file'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_exact_nested_exclusion_and_sorted_key_equivalence(tmp_path):
    for name in ('z.py', 'a.so', 'nested/b.dylib', 'nested/a.py', 'nested/deep/z.py',
                 '.hidden.py', 'site-packages-extra/included.py', 'pycache.py', 'n/site-packages.py',
                 'site-packages/excluded.py', 'site-packages/nested/other.so',
                 'nested/site-packages/excluded.py', 'nested/__pycache__/excluded.dylib',
                 '__pycache__/unusual.py', 'ignored.pyc', 'ignored.pyo', 'ignored.txt'):
        file(tmp_path, name, name.encode())
    expected = old_reference(tmp_path)
    actual = _stdlib_files(tmp_path)
    assert actual == expected and list(actual) == list(expected)
    assert len(actual) == 9


def test_excluded_directories_are_not_entered(tmp_path):
    file(tmp_path, 'valid/a.py')
    file(tmp_path, 'site-packages/large/deep/unused.py')
    file(tmp_path, 'valid/__pycache__/deep/unused.py')
    visited = []
    original = os.scandir
    def scan(path):
        visited.append(str(path))
        return original(path)
    with patch('pocket_music.audio_model_runner.os.scandir', side_effect=scan):
        actual = _stdlib_files(tmp_path)
    assert list(actual) == ['valid/a.py']
    assert all('site-packages' not in Path(path).parts and '__pycache__' not in Path(path).parts for path in visited)


def test_regular_file_links_directory_links_and_broken_links_equal_old(tmp_path):
    root = tmp_path / 'stdlib'
    root.mkdir()
    target = file(tmp_path, 'outside/target.txt', b'outside alias bytes')
    file(root, 'package/original.py', b'original')
    (root / 'alias.py').symlink_to(target)
    (root / 'directory-alias').symlink_to(root / 'package', target_is_directory=True)
    (root / 'directory-named.py').symlink_to(root / 'package', target_is_directory=True)
    (root / 'broken.py').symlink_to(root / 'missing')
    assert _stdlib_files(root) == old_reference(root)
    assert list(_stdlib_files(root)) == ['alias.py', 'package/original.py']
    assert _stdlib_files(root)['alias.py']['sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_symlink_root_matches_old_relative_keys(tmp_path):
    real = tmp_path / 'real'
    file(real, 'nested/source.py')
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    assert _stdlib_files(alias) == old_reference(alias)
    assert list(_stdlib_files(alias)) == ['nested/source.py']


def test_symlink_to_excluded_tree_not_traversed(tmp_path):
    file(tmp_path, 'site-packages/x.py')
    (tmp_path / 'alias').symlink_to(tmp_path / 'site-packages', target_is_directory=True)
    assert _stdlib_files(tmp_path) == old_reference(tmp_path) == {}


def test_included_file_disappearing_before_hash_refuses(tmp_path):
    target = file(tmp_path, 'source.py')
    def remove_then_hash(path):
        target.unlink()
        return file_digest(path)
    with patch('pocket_music.audio_model_runner.file_digest', side_effect=remove_then_hash), pytest.raises(OSError):
        _stdlib_files(tmp_path)


def test_included_file_changed_while_read_refuses(tmp_path):
    target = file(tmp_path, 'source.py', b'original')
    original_open = Path.open
    class MutatingStream:
        def __enter__(self):
            self.stream = original_open(target, 'rb')
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, amount):
            value = self.stream.read(amount)
            if value:
                with original_open(target, 'wb') as stream:
                    stream.write(b'changed content of different size')
            return value
    with patch.object(Path, 'open', return_value=MutatingStream()), pytest.raises(ValueError, match='changed'):
        file_digest(target)


@pytest.mark.parametrize('count', [10000, 10001])
def test_exact_file_count_bound(count, tmp_path):
    rows = [(str(tmp_path), [], [f'file-{i:05}.py' for i in range(count)])]
    with (patch('pocket_music.audio_model_runner.os.walk', return_value=iter(rows)),
          patch.object(Path, 'is_file', return_value=True),
          patch('pocket_music.audio_model_runner.file_digest', return_value={'sha256': 'a' * 64, 'bytes': 1})):
        if count == 10000:
            assert len(_stdlib_files(tmp_path)) == count
        else:
            with pytest.raises(ValueError, match='bounds'):
                _stdlib_files(tmp_path)


def test_unchanged_per_file_byte_limit(tmp_path):
    target = tmp_path / 'too-large.py'
    with target.open('wb') as stream:
        stream.truncate(2 * 1024**3 + 1)
    with pytest.raises(ValueError, match='bounds'):
        _stdlib_files(tmp_path)
