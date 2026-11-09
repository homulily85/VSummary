"""Resolve a stable Docker tag from a GitHub release and the project version."""

import argparse
import re
import tomllib
from pathlib import Path

SEMVER_NUMBER = r"(?:0|[1-9][0-9]*)"
STABLE_VERSION = rf"{SEMVER_NUMBER}\.{SEMVER_NUMBER}\.{SEMVER_NUMBER}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_tag")
    args = parser.parse_args()

    with Path("pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    if version.count(".") == 1:
        version += ".0"
    if not re.fullmatch(STABLE_VERSION, version):
        parser.error(
            "The project version must be a stable MAJOR.MINOR[.PATCH] version."
        )
    if args.release_tag != f"v{version}":
        parser.error(f"Expected release tag v{version} to match the project version.")
    print(version)


if __name__ == "__main__":
    main()
