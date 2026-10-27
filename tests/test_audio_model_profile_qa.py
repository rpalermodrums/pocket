# SPDX-License-Identifier: AGPL-3.0-only
"""Independent synthetic traversal equivalence; never imports an optional model."""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import pocket_music.audio_model_runner as runner


def configure(tmp_path, monkeypatch):
    stdlib = tmp_path / 'stdlib'; stdlib.mkdir()
    package = tmp_path / 'package.py'; package.write_bytes(b'fixture package')
    executable = tmp_path / 'python'; executable.write_bytes(b'fixture executable')
    def distribution(name):
        return SimpleNamespace(files=[Path('package.py')], requires=[], version='1.1.0' if name=='beat-this' else 'fixture',
                               locate_file=lambda item: package)
    monkeypatch.setattr(runner.metadata, 'distribution', distribution)
    monkeypatch.setattr(runner.sysconfig, 'get_path', lambda name: str(stdlib))
    monkeypatch.setattr(runner.sys, 'executable', str(executable))
    return stdlib


def independent_manifest(stdlib):
    result={}
    for path in sorted(stdlib.rglob('*')):
        relative=path.relative_to(stdlib)
        if 'site-packages' in relative.parts or '__pycache__' in relative.parts:continue
        if path.is_file() and path.suffix in ('.py','.so','.dylib'):
            payload=path.read_bytes();result[str(relative)]={'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)}
    return result


def test_profile_exact_prior_included_manifest_with_symlinks_and_names(tmp_path,monkeypatch):
    stdlib=configure(tmp_path,monkeypatch)
    paths=['z.py','A.py','a/sub.py','a/nested/lib.so','empty.dylib','ignore.pyo','site-packages/omitted.py',
           'pkg/__pycache__/omitted.py','pkg/site-packages/omitted.so','site-packages-like/included.py',
           'pkg/__pycache__extra/included.so','a.py-dir/file.py']
    for i,name in enumerate(paths):
        path=stdlib/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(str(i).encode())
    outside=tmp_path/'outside';outside.mkdir();(outside/'not-traversed.py').write_bytes(b'outside')
    (stdlib/'linked-dir').symlink_to(outside,target_is_directory=True)
    (stdlib/'linked.py').symlink_to(stdlib/'z.py')
    (stdlib/'broken.py').symlink_to(stdlib/'missing-target')
    expected=independent_manifest(stdlib);actual=runner.profile()['stdlib']
    assert actual==expected and list(actual)==list(expected)
    assert 'linked.py' in actual and 'linked-dir/not-traversed.py' not in actual
    assert 'broken.py' not in actual and len(actual)==9


def test_excluded_subtrees_are_not_enumerated(tmp_path,monkeypatch):
    stdlib=configure(tmp_path,monkeypatch)
    for name in ('site-packages','__pycache__'):
        folder=stdlib/name;folder.mkdir();(folder/'would-be.py').write_text('excluded')
    (stdlib/'kept.py').write_text('included')
    real=os.scandir;visited=[]
    def guarded(path):
        assert Path(path).name not in ('site-packages','__pycache__'),'Excluded tree was traversed'
        visited.append(str(path));return real(path)
    monkeypatch.setattr(os,'scandir',guarded)
    result=runner.profile()['stdlib']
    assert list(result)==['kept.py'] and visited


def test_included_file_mutation_during_hash_refuses(tmp_path,monkeypatch):
    path=tmp_path/'included.py';path.write_bytes(b'original')
    real=Path.open
    class Racing:
        def __init__(self,stream):self.stream=stream;self.changed=False
        def __enter__(self):return self
        def __exit__(self,*args):return self.stream.__exit__(*args)
        def fileno(self):return self.stream.fileno()
        def read(self,length=-1):
            data=self.stream.read(length)
            if not self.changed:
                self.changed=True
                with real(path,'wb') as out:out.write(b'replaced')
            return data
    monkeypatch.setattr(Path,'open',lambda self,*a,**kw:Racing(real(self,*a,**kw)) if self==path and a and a[0]=='rb' else real(self,*a,**kw))
    with pytest.raises(ValueError,match='changed'):runner.file_digest(path)


def test_missing_and_oversized_runtime_file_refuse(tmp_path):
    with pytest.raises(OSError):runner.file_digest(tmp_path/'missing.py')
    path=tmp_path/'large.so'
    with path.open('wb') as stream:stream.truncate(2*1024**3+1)
    with pytest.raises(ValueError,match='bounds'):runner.file_digest(path)


def test_changed_runner_digest_invalidates_historical_profile_before_model(monkeypatch):
    import socket
    old={'runner':{'sha256':'a'*64,'bytes':1},'stdlib':{}}
    fresh={'runner':{'sha256':'b'*64,'bytes':1},'stdlib':{}}
    monkeypatch.setattr(runner,'profile',lambda:fresh)
    monkeypatch.setattr(runner,'load_model',lambda *a:pytest.fail('Stale profile reached model'))
    # Restore process-wide network hooks after this synthetic execute call.
    monkeypatch.setattr(socket,'create_connection',socket.create_connection)
    monkeypatch.setattr(socket.socket,'connect',socket.socket.connect)
    monkeypatch.setattr(socket.socket,'connect_ex',socket.socket.connect_ex)
    with pytest.raises(ValueError,match='profile changed'):
        runner.execute({'expected_profile':runner.sha(runner.canonical(old)),'qualification':'none'})


def test_stdlib_count_boundary_does_not_silently_truncate(tmp_path,monkeypatch):
    stdlib=tmp_path/'many';stdlib.mkdir()
    for index in range(10001):(stdlib/f'f{index:05}.py').touch()
    hashed=[]
    def fast(path):
        hashed.append(path)
        return {'sha256':hashlib.sha256(b'').hexdigest(),'bytes':0}
    monkeypatch.setattr(runner,'file_digest',fast)
    with pytest.raises(ValueError,match='bounds'):runner._stdlib_files(stdlib)
    assert len(hashed)<=10000
    (stdlib/'f10000.py').unlink();hashed.clear()
    result=runner._stdlib_files(stdlib)
    assert len(result)==10000 and list(result)==sorted(result)
