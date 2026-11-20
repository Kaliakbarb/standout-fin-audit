#!/usr/bin/env python3
"""Build, verify or safely unpack a pinned offline release using stdlib+Docker."""

import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
from release_support import ReleaseError, build_release, collect_image_metadata, extract_release, verify_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--image-id", required=True)
    build.add_argument("--image-archive", type=Path, required=True)
    build.add_argument("--sample", type=Path, required=True)
    build.add_argument("--context")
    check = commands.add_parser("check")
    check.add_argument("bundle", type=Path)
    extract = commands.add_parser("extract")
    extract.add_argument("archive", type=Path)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--sha256", required=True, help="Trusted SHA-256 received separately from the archive.")
    args = parser.parse_args()
    try:
        if args.command == "build":
            docker = ["docker", "--context", args.context] if args.context else ["docker"]
            metadata = collect_image_metadata(docker, args.image_id)
            result = build_release(args.root, args.output, args.image_archive, args.image_id, metadata, args.sample)
            print(f"Release built: {result['output']}\nArchive SHA-256: {result['archive_sha256']}")
        elif args.command == "check":
            manifest = verify_bundle(args.bundle)
            print(f"Release inventory PASS; image {manifest['runtime']['image_id']}")
        else:
            path = extract_release(args.archive, args.output, args.sha256)
            print(f"Release safely extracted: {path}")
    except (ReleaseError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Release failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
