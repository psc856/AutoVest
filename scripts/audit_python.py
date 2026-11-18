"""Audit only this application's declared runtime dependency closure using OSV."""
import argparse
import json
from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path

import requests
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def runtime_dependencies(requirements_path):
    queue = [Requirement(line.strip()) for line in requirements_path.read_text().splitlines() if line.strip() and not line.startswith('#')]
    seen, selected, missing = set(), {}, []
    while queue:
        requirement = queue.pop()
        identity = (canonicalize_name(requirement.name), tuple(sorted(requirement.extras)))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            installed = distribution(requirement.name)
        except PackageNotFoundError:
            missing.append(requirement.name)
            continue
        selected[canonicalize_name(installed.metadata['Name'])] = installed.version
        for raw in installed.requires or []:
            child = Requirement(raw)
            if child.marker is None or any(child.marker.evaluate({'extra': extra}) for extra in ['', *requirement.extras]):
                queue.append(child)
    if missing:
        raise RuntimeError('Declared dependencies are not installed: ' + ', '.join(sorted(set(missing))))
    return sorted(selected.items())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='.test-artifacts/python-audit.json')
    parser.add_argument('--requirements', default='requirements.txt')
    args = parser.parse_args()
    requirements_path = Path(args.requirements).resolve()
    project_root = Path(__file__).resolve().parents[1]
    if requirements_path not in {project_root / 'requirements.txt', project_root / 'backend' / 'requirements.txt'}:
        raise ValueError('Use one of the project runtime requirements files.')
    packages = runtime_dependencies(requirements_path)
    provenance = json.loads((project_root / 'backend/vendor/kite_rest/provenance.json').read_text())
    packages.append((provenance['distribution'], provenance['version']))
    response = requests.post('https://api.osv.dev/v1/querybatch', json={'queries': [
        {'package': {'ecosystem': 'PyPI', 'name': name}, 'version': version} for name, version in packages
    ]}, timeout=30)
    response.raise_for_status()
    results = response.json()['results']
    if len(results) != len(packages):
        raise RuntimeError('Advisory response did not match the submitted package list.')
    findings = []
    for (name, version), result in zip(packages, results):
        ids = [item['id'] for item in result.get('vulns', [])]
        if ids:
            findings.append({'package': name, 'version': version, 'advisories': ids})
    report = {'source': 'https://osv.dev', 'scope': 'Application runtime dependency closure only', 'packages_checked': len(packages), 'findings': findings}
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
