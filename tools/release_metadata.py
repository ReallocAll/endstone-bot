"""Resolve immutable release intent; ordinary pushes and PRs only build."""
import json
import os
from pathlib import Path
import re


def resolve(event_name, ref, event, version):
    publish = False
    requested = None
    if event_name == 'push' and ref.startswith('refs/tags/'):
        requested = ref.removeprefix('refs/tags/v')
        publish = True
    elif event_name == 'push' and ref == 'refs/heads/main':
        subject = event.get('head_commit', {}).get('message', '').split('\n', 1)[0]
        if subject.startswith('chore: release v'):
            match = re.fullmatch(r'chore: release v(\d+\.\d+\.\d+)', subject)
            if not match:
                raise ValueError('Release subject must be chore: release vX.Y.Z')
            requested = match[1]
            publish = True
    elif event_name == 'workflow_dispatch':
        inputs = event.get('inputs', {})
        requested = inputs.get('version', '')
        publish = str(inputs.get('dry_run', 'true')).lower() == 'false'
        if ref != 'refs/heads/main':
            raise ValueError('Manual releases and previews must use main')
    if requested is not None:
        if not re.fullmatch(r'\d+\.\d+\.\d+', requested) or requested != version:
            raise ValueError('Requested release must match package version X.Y.Z')
    return publish


def main():
    import tomllib
    version = tomllib.loads(Path('pyproject.toml').read_text())['project']['version']
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    publish = resolve(os.environ['GITHUB_EVENT_NAME'], os.environ['GITHUB_REF'], event, version)
    init = Path('endstone_bot/__init__.py').read_text()
    if f'__version__ = "{version}"' not in init:
        raise ValueError('Package runtime version mismatch')
    if publish or os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch':
        if not re.search(r'^## (?:\[' + re.escape(version) + r'\]|' + re.escape(version) + r')(?:\s|$)', Path('CHANGELOG.md').read_text(), re.M):
            raise ValueError('Versioned changelog entry required')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'version={version}\npublish={str(publish).lower()}\n')


if __name__ == '__main__':
    main()
