"""Prepare a fresh private research build using the existing Worker packaging."""
import json
from pathlib import Path
import shutil

from prepare_python_worker import prepare

RESEARCH_FILES = ('cloud_research.py', 'cloud_research_auto.py', 'research_evaluation.py', 'evaluation.py',
    'research_suite.py', 'research_alternatives.py', 'research_expansions.py',
    'research_joint_kelly.py', 'research_kelly.py', 'research_neutral.py', 'research_portfolio.py',
    'research_portfolio_scenarios.py', 'research_selection.py', 'research_all_markets.py', 'research_ticket_events.py')


def prepare_backtest(destination):
    from hr_platform.common import canonical, sha
    root = prepare(destination, with_storage=True)
    repo = Path(__file__).resolve().parents[1]
    for name in RESEARCH_FILES:
        shutil.copyfile(repo / 'src/hr_platform' / name, root / 'src/hr_platform' / name)
    shutil.copyfile(repo / 'workers/backtest/entry.py', root / 'src/entry.py')
    hashes = {str(p.relative_to(root / 'src')): sha(p.read_bytes()) for p in sorted((root / 'src').rglob('*.py'))}
    config = json.loads((root / 'wrangler.jsonc').read_bytes())
    policy = json.loads((repo / 'configs/cloud-research.json').read_bytes())
    basis = {'sources': hashes,
             'dependencies': {n: sha((root / n).read_bytes()) for n in ('pyproject.toml', 'pylock.toml')},
             'runtime': {k: config[k] for k in ('compatibility_date', 'compatibility_flags', 'python_modules')}}
    config.update(name='hr-platform-dev-backtest', triggers={'crons': []},
        vars={'STORAGE_POLICY_JSON': config['vars']['STORAGE_POLICY_JSON'],
              'RESEARCH_POLICY_JSON': json.dumps(policy), 'ENGINE_ID': sha(canonical(basis)),
              'RESEARCH_ENABLED': 'false', 'AUTO_RESEARCH_ENABLED': 'false'})
    for key in ('services', 'durable_objects', 'migrations'):
        config.pop(key, None)
    (root / 'wrangler.jsonc').write_text(json.dumps(config, indent=2) + '\n')
    (root / 'engine-manifest.json').write_bytes(canonical({'engine_id': config['vars']['ENGINE_ID'], 'basis': basis}))
    return root


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    print(prepare_backtest(args.out))
