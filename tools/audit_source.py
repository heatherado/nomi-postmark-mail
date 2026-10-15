"""Audit the Git index without printing matching secret material.

Heuristic safeguard only. Also inspect the staged diff before publishing.
"""
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES={'settings.json','profiles.json','mail-state.json','event-state.json','app.lock','desktop.ini','thumbs.db'}
FORBIDDEN_PARTS={'residents','removed-residents','shortcut backups','backups','__pycache__','.venv'}
PATTERNS={
    'personal-windows-path':re.compile(r'[A-Za-z]:[\\/]Users[\\/][^\s\x22\x27<>]+',re.I),
    'github-token':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'openai-like-key':re.compile(r'sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}'),
    'private-key-material':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'inline-auth-value':re.compile(r'[\x22\x27]Authorization[\x22\x27]\s*:\s*[\x22\x27](?:Bearer\s+)?[A-Za-z0-9_-]{24,}[\x22\x27]'),
}


def main():
    result=subprocess.run(['git','ls-files','--cached','-z'],cwd=ROOT,check=True,stdout=subprocess.PIPE)
    names=[name for name in result.stdout.decode('utf-8').split('\0') if name]
    if not names:
        print('No indexed source files. Stage only the reviewed source first.')
        return 1
    failures=[]
    for name in names:
        pure=PurePosixPath(name)
        lower=[p.lower() for p in pure.parts]
        if pure.is_absolute() or '..' in pure.parts:
            failures.append((name,'unsafe path'))
            continue
        if pure.name.lower() in FORBIDDEN_NAMES or any(p in FORBIDDEN_PARTS or p.startswith('lamplit-ui-') for p in lower) or pure.suffix.lower() in {'.lnk','.log','.bak','.tmp','.zip','.key','.pem','.pfx','.p12','.pyc'} or pure.name.lower().endswith('-state.json') or pure.name.lower().startswith('.env'):
            failures.append((name,'private/runtime artifact'))
            continue
        # Read the staged blob, not potentially different working-tree content.
        data=subprocess.run(['git','show',':'+name],cwd=ROOT,check=True,stdout=subprocess.PIPE).stdout
        if pure.suffix.lower() in {'.png','.ico'}:
            continue
        try:
            text=data.decode('utf-8')
        except UnicodeDecodeError:
            failures.append((name,'unexpected binary file'))
            continue
        for rule,pattern in PATTERNS.items():
            if pattern.search(text):
                failures.append((name,rule))
    for name,rule in failures:
        print(f'BLOCKED: {name} [{rule}]')
    if failures:
        print('Audit failed. Matching values were not printed.')
        return 1
    print(f'PASS: {len(names)} indexed source/test/docs/artwork files checked. No flagged private artifacts or secret patterns. Manual review is still required.')
    return 0


if __name__=='__main__':
    sys.exit(main())
