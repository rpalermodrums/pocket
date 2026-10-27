# SPDX-License-Identifier: AGPL-3.0-only
"""Stage only reviewed public inputs; build and verify a portable Pages artifact."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import posixpath
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
GENERATED_PAGES = {'reference/index.md'}


def tracked_files(root):
    return set(subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')) - {''}


def safe_source(root, relative, tracked):
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] in ('private', '.git', '.impeccable'):
        raise ValueError(f'Unsafe publication source: {relative}')
    if relative not in tracked:
        raise ValueError(f'Publication source is not tracked: {relative}')
    source = root / relative
    if any(parent.is_symlink() for parent in [source, *source.parents] if parent != root.parent):
        raise ValueError(f'Symlink publication source refused: {relative}')
    if not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Publication source escapes checkout or is absent: {relative}')
    return source


def safe_target(relative):
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts or '\\' in relative:
        raise ValueError(f'Unsafe publication target: {relative}')
    return relative


def rewrite_link(url, source, target, mapping, root, tracked, revision):
    if url.startswith(('#', 'https://', 'http://', 'mailto:')):
        return url
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or url.startswith('/'):
        raise ValueError(f'Unsupported public link in {source}: {url}')
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(parsed.path)))
    if resolved in GENERATED_PAGES:
        result = posixpath.relpath(resolved, posixpath.dirname(target) or '.')
        return result + ('#' + parsed.fragment if parsed.fragment else '')
    safe_source(root, resolved, tracked)
    if resolved in mapping:
        result = posixpath.relpath(mapping[resolved], posixpath.dirname(target) or '.')
        return result + ('#' + parsed.fragment if parsed.fragment else '')
    # Only explicitly permitted code/example references may point back to GitHub.
    if not (resolved.startswith(('examples/', 'src/')) or resolved == 'AGENTS.md'):
        raise ValueError(f'Link points to unlisted public content: {source}: {url}')
    return f'https://github.com/rpalermodrums/pocket/blob/{revision}/{resolved}' + ('#' + parsed.fragment if parsed.fragment else '')


def verbatim_page(text, title):
    # Plain-text sources such as LICENSE are shown exactly as written, never parsed as Markdown.
    fence = '`' * max(3, 1 + max(map(len, re.findall('`+', text)), default=0))
    body = text.rstrip('\n')
    return f'# {title}\n\n{fence}text\n{body}\n{fence}\n'


def stage_sources(root, destination, manifest, revision):
    tracked = tracked_files(root)
    rows = manifest['pages'] + manifest['assets']
    mapping = {}
    titles = {}
    targets = set()
    for row in rows:
        source, target = row['source'], safe_target(row['target'])
        if source in mapping or target in targets or target in GENERATED_PAGES:
            raise ValueError('Duplicate publication source or target')
        safe_source(root, source, tracked)
        mapping[source] = target
        titles[source] = row.get('title', PurePosixPath(source).name)
        targets.add(target)
    evidence = []
    for source, target in mapping.items():
        original = safe_source(root, source, tracked).read_bytes()
        data = original
        if source.endswith('.md'):
            content = original.decode()
            content = re.sub(r'(?<!!)(\[[^\]\n]+\]\()([^\s)]+)(\))',
                lambda m: m[1] + rewrite_link(m[2], source, target, mapping, root, tracked, revision) + m[3], content)
            data = content.encode()
        elif target.endswith('.md'):
            data = verbatim_page(original.decode(), titles[source]).encode()
        if re.search(rb'/Users/[^\s"<>]+|/home/(?!runner/)[^\s"<>]+', data):
            raise ValueError(f'Personal filesystem path in public source: {source}')
        output = destination / target
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        evidence.append({'source': source, 'target': target, 'sha256': hashlib.sha256(original).hexdigest()})
    return evidence


def generate_reference(destination, root, revision):
    # Reuse the actual exporter rather than maintaining a second API inventory.
    spec = importlib.util.spec_from_file_location('pocket_contract_export', root/'examples/export_contracts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = asyncio.run(module.build_contracts())
    import pocket_music
    installed = Path(pocket_music.__file__).parent
    for file in (root/'src/pocket_music').glob('*.py'):
        if not (installed/file.name).is_file() or (installed/file.name).read_bytes() != file.read_bytes():
            raise ValueError('Installed Pocket code differs from this checkout; install the matching revision')
    export = destination/'reference'
    export.mkdir()
    for name, value in [('installed-contracts.json', data), ('mcp-tools.json', data['mcp_tools'])]:
        (export/name).write_text(json.dumps(value, indent=2)+'\n')
    names = {r['name'] for r in data['registered_providers']}
    text = ['# Provider reference', '',
        'Every provider Pocket installs, with its Python signature, command-line form and MCP input schema. '
        f'This page is generated from package **{data["package_version"]}** at source revision '
        f'[`{revision[:12]}`](https://github.com/rpalermodrums/pocket/tree/{revision}), so it always matches the code.', '',
        'Only inputs are described: receipts are open JSON dictionaries whose fields depend on the call. '
        'Call `capabilities_list` to see current profiles and prerequisites on your own installation. '
        'The local workspace and practice review pages have their own browser routes; there is no general HTTP API. '
        'New to the vocabulary? Start with [key ideas](../docs/concepts.md).', '',
        '[Download complete contracts](installed-contracts.json) · [Download all MCP tools](mcp-tools.json)', '',
        f'{len(names)} providers, and {len(data["mcp_tools"])} MCP registrations in total, including tool-family and compatibility names.', '',
        '<label for="provider-filter">Find a provider</label><input id="provider-filter" type="search" placeholder="Try context, MIDI, feedback…"><p id="provider-count" role="status"></p>', '']
    from html import escape
    for row in data['registered_providers']:
        text += [f'<details class="provider" id="{row["name"]}"><summary><code>{row["name"]}</code> <span>{escape(row["description"])}</span></summary><div class="provider-body">',
          f'<p><strong>Domain:</strong> {escape(row["domain"])}. <strong>Read only:</strong> {str(row["read_only"]).lower()}.</p>',
          '<p>Python</p><pre><code>'+escape(row['python']['provider']+row['python']['signature'])+'</code></pre>',
          '<p>CLI</p><pre><code>'+escape(row['cli']['command'])+'</code></pre>',
          '<p>MCP input schema</p><pre><code>'+escape(json.dumps(row['mcp']['input_schema'],indent=2))+'</code></pre>',
          '</div></details>', '']
    legacy = sorted(t['name'] for t in data['mcp_tools'] if t['name'] not in names)
    text += ['## Other MCP registrations', '', 'Tool-family names (such as `peek` and `thread`) and compatibility aliases route to the providers above; they are not separate operations. Their exact input schemas are in the complete MCP download.', '', ', '.join('`'+n+'`' for n in legacy), '']
    (export/'index.md').write_text('\n'.join(text))
    return {'package_version':data['package_version'],'providers':len(names),'mcp_tools':len(data['mcp_tools'])}


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.ids=set()
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if 'id' in a:self.ids.add(a['id'])
        for key in ('href','src'):
            if key in a:self.links.append(a[key])


def check_site(site):
    parsed={}
    for file in site.rglob('*.html'):
        parser=PageLinks();parser.feed(file.read_text());parsed[file.resolve()]=parser
    for file,parser in parsed.items():
        for url in parser.links:
            link=urlsplit(url)
            if link.scheme or link.netloc:continue
            if link.path.startswith('/'):
                if not link.path.startswith('/pocket/'):raise ValueError(f'Wrong project subpath: {url}')
                target=site/link.path.removeprefix('/pocket/')
            else:target=file.parent/unquote(link.path) if link.path else file
            if target.is_dir():target=target/'index.html'
            if not target.resolve().is_relative_to(site.resolve()) or not target.exists():
                raise ValueError(f'Broken output link: {file.relative_to(site)}: {url}')
            if link.fragment and target.resolve() in parsed and unquote(link.fragment) not in parsed[target.resolve()].ids:
                raise ValueError(f'Broken fragment: {file.relative_to(site)}: {url}')
    return len(parsed)


def build(output):
    output=output.resolve()
    if output.exists():raise FileExistsError('Choose a new site output directory')
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    dirty=bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True).strip())
    manifest=json.loads((ROOT/'site/public-docs.json').read_text())
    output.mkdir(parents=True)
    stage=output/'staging';stage.mkdir()
    evidence=stage_sources(ROOT,stage,manifest,revision)
    info=generate_reference(stage,ROOT,revision)
    # Theme itself has an explicit tracked-file allowlist too.
    theme=output/'theme';theme.mkdir()
    tracked=tracked_files(ROOT)
    for relative in manifest['theme']:
        source=safe_source(ROOT,relative,tracked)
        shutil.copyfile(source,theme/source.name)
    import yaml
    # "Improve this page" links point at the tracked Markdown each page came from.
    sources={row['target']:row['source'] for row in evidence if row['source'].endswith('.md')}
    config={'site_name':'Pocket','site_description':'Composable tools for music, for people and their AI agents. '
            'Python, command-line and MCP interfaces with explicit inputs and inspectable results.',
        'site_url':'https://rpalermodrums.github.io/pocket/','docs_dir':str(stage),'site_dir':str(output/'pocket'),
        'theme':{'name':None,'custom_dir':str(theme),'static_templates':['404.html']},'plugins':['search'],
        'markdown_extensions':['fenced_code','tables',{'toc':{'permalink':'#','permalink_title':'Link to this section'}},
                               'attr_list','md_in_html'],
        'nav':manifest['nav'],'extra':{'revision':revision,'dirty':dirty,'tagline':'The musical toolkit for agents',
                                       'sources':sources,**info},'strict':True,
        'validation':{'nav':{'omitted_files':'ignore'},'links':{'not_found':'warn','anchors':'warn','unrecognized_links':'warn'}}}
    config_file=output/'mkdocs.yml';config_file.write_text(yaml.safe_dump(config,sort_keys=False))
    subprocess.run([sys.executable,'-m','mkdocs','build','--strict','--config-file',str(config_file)],check=True)
    site=output/'pocket'
    files=[{'path':str(f.relative_to(site)),'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'bytes':f.stat().st_size}
           for f in sorted(site.rglob('*')) if f.is_file()]
    (site/'publication-manifest.json').write_text(json.dumps({'schema':'pocket.public-site/v1','revision':revision,
        **info,'uncommitted_sources':dirty,'source_files':evidence,'files':files},indent=2)+'\n')
    pages=check_site(site)
    print(json.dumps({'site':str(site),'html_pages':pages,**info}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    build(parser.parse_args().output)
