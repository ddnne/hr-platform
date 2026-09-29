"""Copy an explicit source allowlist to an ignored, private build directory.

No provider fetch or deployment. Keep virtualenvs and real inputs outside src,
the directory recursively scanned for Python Worker modules.
"""

import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hr_platform.cli import private_root  # noqa: E402

MODEL_FILES = ("__init__.py", "model.py", "cloud_model.py")


def prepare(destination):
    repo = Path(__file__).resolve().parents[1]
    root = private_root(destination)
    # A build directory is fresh: never mix previously copied private content.
    if root.exists():
        raise ValueError("FRESH_BUILD_DIRECTORY_REQUIRED")
    source = root / "src"
    package = source / "hr_platform"
    package.mkdir(parents=True)
    for name in MODEL_FILES:
        shutil.copyfile(repo / "src/hr_platform" / name, package / name)
    shutil.copyfile(repo / "workers/research/entry.py", source / "entry.py")
    for name in ("pyproject.toml", "pylock.toml"):
        shutil.copyfile(repo / "workers/research" / name, root / name)
    config = {
        "name": "hr-platform-dev-research",
        "main": "src/entry.py",
        "compatibility_date": "2026-09-29",
        "compatibility_flags": ["python_workers"],
        "workers_dev": False,
        "preview_urls": False,
        "triggers": {"crons": []},
    }
    (root / "wrangler.jsonc").write_text(json.dumps(config, indent=2) + "\n")
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Python Workerの明示したソースだけを非公開の新規ディレクトリへ準備"
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(prepare(args.out))
