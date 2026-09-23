"""Resolve an approved main-history revision and require its successful Tests run."""
import json
import os
import re
import subprocess


def resolve_revision(requested):
    if requested != 'main' and not re.fullmatch(r'[0-9a-f]{40}', requested):
        raise ValueError('Choose main or a full 40-character commit SHA')
    ref='origin/main' if requested=='main' else requested
    sha=subprocess.check_output(['git','rev-parse','--verify',ref+'^{commit}'],text=True).strip()
    subprocess.run(['git','merge-base','--is-ancestor',sha,'origin/main'],check=True)
    repo=os.environ['GITHUB_REPOSITORY']
    result=json.loads(subprocess.check_output(['gh','api',f'repos/{repo}/actions/workflows/tests.yml/runs?head_sha={sha}&event=push&status=success&per_page=10'],text=True))
    if not any(r['head_sha']==sha and r['conclusion']=='success' and r['head_branch']=='main' for r in result['workflow_runs']):
        raise ValueError('This main revision has no successful Tests push run; wait for CI or choose a verified earlier revision')
    return sha


if __name__=='__main__':
    if os.environ['GITHUB_REF']!='refs/heads/main':
        raise ValueError('Publishing may only be dispatched from main')
    sha=resolve_revision(os.environ.get('REQUESTED_REVISION','main'))
    with open(os.environ['GITHUB_OUTPUT'],'a') as output:output.write('sha='+sha+'\n')
    print('Verified main-history revision',sha)
