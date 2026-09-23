"""Publication boundary checks: ignored data must never enter the public artifact."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('public_site', Path(__file__).parents[1]/'site/build.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


def repository(tmp_path):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    (tmp_path/'guide.md').write_text('# Public guide\n')
    (tmp_path/'private').mkdir()
    (tmp_path/'private/secret.md').write_text('never publish this')
    subprocess.run(['git','add','guide.md'],cwd=tmp_path,check=True)
    return tmp_path


@pytest.mark.parametrize('source',['private/secret.md','../elsewhere.md','/etc/passwd','untracked.md'])
def test_nonpublic_sources_refused(tmp_path,source):
    root=repository(tmp_path)
    with pytest.raises(ValueError):site.safe_source(root,source,site.tracked_files(root))


def test_tracked_symlink_and_parent_symlink_refused(tmp_path):
    root=repository(tmp_path)
    (root/'linked.md').symlink_to(root/'private/secret.md')
    subprocess.run(['git','add','linked.md'],cwd=root,check=True)
    with pytest.raises(ValueError,match='Symlink'):site.safe_source(root,'linked.md',site.tracked_files(root))


def test_only_allowlisted_bytes_staged(tmp_path):
    root=repository(tmp_path)
    output=root/'out';output.mkdir()
    manifest={'pages':[{'source':'guide.md','target':'index.md'}],'assets':[]}
    evidence=site.stage_sources(root,output,manifest,'a'*40)
    assert [p.name for p in output.iterdir()]==['index.md']
    assert evidence[0]['source']=='guide.md'
    assert 'never publish' not in (output/'index.md').read_text()


def test_duplicate_and_escaping_targets_refused(tmp_path):
    root=repository(tmp_path);output=root/'out';output.mkdir()
    with pytest.raises(ValueError,match='Duplicate'):
        site.stage_sources(root,output,{'pages':[{'source':'guide.md','target':'index.md'}]*2,'assets':[]},'a'*40)
    with pytest.raises(ValueError,match='Unsafe'):
        site.safe_target('../outside.md')


def test_private_and_unlisted_links_fail_closed(tmp_path):
    root=repository(tmp_path)
    for url in ('private/secret.md','/etc/passwd','unlisted.md'):
        with pytest.raises(ValueError):
            site.rewrite_link(url,'guide.md','index.md',{'guide.md':'index.md'},root,site.tracked_files(root),'a'*40)


def test_code_links_are_revision_bound_and_guides_remain_local(tmp_path):
    root=repository(tmp_path);(root/'examples').mkdir();(root/'examples/demo.py').write_text('print("synthetic")')
    subprocess.run(['git','add','examples'],cwd=root,check=True)
    tracked=site.tracked_files(root)
    assert site.rewrite_link('examples/demo.py','guide.md','index.md',{},root,tracked,'a'*40).endswith('/'+'a'*40+'/examples/demo.py')
    assert site.rewrite_link('guide.md#cue','guide.md','index.md',{'guide.md':'index.md'},root,tracked,'a'*40)=='index.md#cue'


def test_output_links_and_fragments_checked(tmp_path):
    (tmp_path/'index.html').write_text('<a href="guide/#cue">Guide</a>')
    (tmp_path/'guide').mkdir();(tmp_path/'guide/index.html').write_text('<h1 id="cue">Cue</h1>')
    assert site.check_site(tmp_path)==2
    (tmp_path/'guide/index.html').write_text('<h1>Missing cue</h1>')
    with pytest.raises(ValueError,match='fragment'):site.check_site(tmp_path)


def test_public_allowlist_has_no_private_or_media_inputs():
    root=Path(__file__).parents[1]
    manifest=json.loads((root/'site/public-docs.json').read_text())
    for row in manifest['pages']+manifest['assets']:
        assert not row['source'].startswith(('private/','.impeccable/'))
        assert Path(row['source']).suffix not in {'.wav','.mp3','.flac','.als','.opus'}


def test_deployment_refuses_untrusted_revision_before_running_git(monkeypatch):
    script=Path(__file__).parents[1]/'site/scripts/check_publish.py'
    spec=importlib.util.spec_from_file_location('check_publish',script)
    publish=importlib.util.module_from_spec(spec);spec.loader.exec_module(publish)
    monkeypatch.setattr(publish.subprocess,'check_output',lambda *a,**kw: pytest.fail('invalid input reached git'))
    for value in ('feature/unreviewed','--help','main; arbitrary-command','a'*39,''):
        with pytest.raises(ValueError):publish.resolve_revision(value)


def test_deployment_requires_successful_main_push_run(monkeypatch):
    script=Path(__file__).parents[1]/'site/scripts/check_publish.py'
    spec=importlib.util.spec_from_file_location('check_publish',script)
    publish=importlib.util.module_from_spec(spec);spec.loader.exec_module(publish)
    sha='a'*40
    monkeypatch.setenv('GITHUB_REPOSITORY','owner/repo')
    monkeypatch.setattr(publish.subprocess,'run',lambda *a,**kw:None)
    def response(args,**kwargs):
        return sha if args[0]=='git' else json.dumps({'workflow_runs':[{'head_sha':sha,'conclusion':'success','head_branch':'feature/unreviewed'}]})
    monkeypatch.setattr(publish.subprocess,'check_output',response)
    with pytest.raises(ValueError,match='no successful Tests'):publish.resolve_revision(sha)
