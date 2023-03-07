# -*- coding: utf-8 -*-
import argparse
import itertools
import logging
import os
import pathlib
import subprocess
from collections import namedtuple

_logger = logging.getLogger(__name__)

RsMcuContext = namedtuple("RsMcuContext", ["path"])

PROJECTS = (
    "efm32g-pacs",
    "efm32gg-pacs",
    "efm32hg-pacs",  # noqa: GRG001
    "efm32jg-pacs",  # noqa: GRG001
    "efm32lg-pacs",  # noqa: GRG001
    "efm32pg-pacs",  # noqa: GRG001
    "efm32tg-pacs",  # noqa: GRG001
    "efm32wg-pacs",  # noqa: GRG001
    "efm32zg-pacs",  # noqa: GRG001
)
PROJECTS_CTX = (
    RsMcuContext(
        path=pathlib.Path(f"../{p}").resolve(),
    )
    for p in PROJECTS
)


def execute_pacs_generator(_args: argparse.Namespace) -> None:
    for p in PROJECTS_CTX:
        subprocess.run(
            [
                "python",
                "tools.py",
                "pacs-gen",
                "--svd-dir",
                str(p.path.joinpath("svd")),
            ],
            check=True,
        )


def execute_publish(args: argparse.Namespace) -> None:
    for p in PROJECTS_CTX:
        cmd = ["python", "tools.py", "publish", "--dir", str(p.path)]

        if args.dry_run:
            cmd.append("--dry-run")

        if args.exclude:
            cmd.extend(itertools.chain(*[["--exclude", e] for e in args.exclude]))

        subprocess.run(
            cmd,
            check=True,
        )


def generate_doc_md_table(args: argparse.Namespace) -> None:
    pacs_dir = pathlib.Path(args.dir).resolve()
    arch = args.arch
    docs_md_header = r"""
| Crate| Docs | crates.io | target |
|------|------|-----------|--------|"""
    out = [docs_md_header]

    for p in pacs_dir.glob("*/*"):
        crate_name = f"{p.stem}-pac"
        docs_rs_link = f"[![docs.rs](https://docs.rs/{crate_name}/badge.svg)](https://docs.rs/{crate_name})"
        crates_io_link = f"[![crates.io](https://img.shields.io/crates/d/{crate_name}.svg)](https://crates.io/crates/{crate_name})"
        out.append(f"|`{crate_name}`|{docs_rs_link}|{crates_io_link}|`{arch}`|")

    _logger.info(os.linesep.join(out))


def tag_release(_args: argparse.Namespace) -> None:
    for p in PROJECTS_CTX:
        cmd = ["python", "tools.py", "tag", "--dir", str(p.path)]
        subprocess.run(
            cmd,
            check=True,
        )


def test_crates(args: argparse.Namespace) -> None:
    for p in PROJECTS_CTX:
        cmd = [
            "python",
            "tools.py",
            "test",
            "--dir",
            str(p.path),
        ]

        if args.exclude:
            cmd.extend(itertools.chain(*[["--exclude", e] for e in args.exclude]))

        subprocess.run(
            cmd,
            check=True,
        )


if __name__ == "__main__":
    logging.basicConfig()
    parser = argparse.ArgumentParser(
        description="Batch generator for EFM32 Rust Crates"
    )

    commands = parser.add_subparsers(dest="command")
    commands.add_parser("pacs")
    commands.add_parser("tag", help="Tag PACs repositories")
    docmd = commands.add_parser("docmd")
    docmd.add_argument("--dir", required=True, help="Directory where PACs can be found")
    docmd.add_argument("--arch", help="PACs architecture")
    test = commands.add_parser("test")
    test.add_argument("--exclude", action="append", help="Exclude crate from testing")
    publish = commands.add_parser("publish")
    publish.add_argument(
        "-n", "--dry-run", action="store_true", help="Dry run publishing enable"
    )
    publish.add_argument(
        "--exclude", action="append", help="Exclude crate from publishing"
    )

    args = parser.parse_args()
    handlers = {
        "pacs": execute_pacs_generator,
        "docmd": generate_doc_md_table,
        "publish": execute_publish,
        "tag": tag_release,
        "test": test_crates,
    }

    if handlers.get(args.command) is not None:
        handlers[args.command](args)
    else:
        parser.print_help()
