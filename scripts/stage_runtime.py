"""Stage only the independent offline runtimes used by this release."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mariadb", type=Path, default=os.environ.get("PILOT_MARIADB_ROOT"))
    parser.add_argument("--browsers", type=Path, default=os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
    args = parser.parse_args()
    resources = ROOT / "packaging" / "resources"
    maria = args.mariadb
    if not maria or not (maria / "bin" / "mariadbd.exe").is_file():
        raise SystemExit("Provide --mariadb with the verified Windows x64 MariaDB distribution.")
    browsers = args.browsers
    if not browsers or not browsers.is_dir() or not list(browsers.glob("chromium*")):
        raise SystemExit("Provide --browsers with the tested Playwright browser cache.")
    target = resources / "runtime" / "mariadb"
    shutil.copytree(maria, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.pdb", "*.lib", "*.log", "mysql-test", "sql-bench"))
    shutil.copytree(browsers, resources / "browsers", dirs_exist_ok=True, ignore=shutil.ignore_patterns(".links", "*.log"))
    report = {"format": "pilot-runtime/2", "runtime": {"mariadb": "runtime/mariadb", "browsers": "browsers"}}
    resources.mkdir(parents=True, exist_ok=True)
    (resources / "runtime-manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
