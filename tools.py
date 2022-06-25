# -*- coding: utf-8 -*-
import argparse
import asyncio
import dataclasses
import multiprocessing
import pathlib
import re
import shutil
import tempfile
from typing import Union, Iterable, Dict, Any, Optional

import toml

LICENSE = "BSD-3-Clause"
AUTHORS = ["Vladimir Petrigo <vladimir.petrigo@gmail.com>"]

CRATE_GEN_SEM = asyncio.Semaphore(multiprocessing.cpu_count())
PUBLISH_SEM = asyncio.Semaphore(1)
OUT_DIR_LOCK = asyncio.Lock()


@dataclasses.dataclass
class SvdMeta:
    name: str
    path: pathlib.Path


def mcu_crate_toml_template(
    svd_descr: SvdMeta, repository: str, version: str
) -> Dict[str, Any]:
    cargo_toml_mcu_template = {
        "package": {
            "name": f"{svd_descr.name}-pac",
            "description": f"Peripheral access API for {svd_descr.name.upper()} MCU (generated using svd2rust)",
            "homepage": repository,
            "version": version,
            "authors": AUTHORS,
            "license": LICENSE,
            "keywords": ["no-std", "arm", "cortex-m", "efm32"],
            "categories": ["embedded", "hardware-support", "no-std"],
            "repository": repository,
            "readme": "README.md",
            "edition": "2021",
        },
        "dependencies": {
            "cortex-m": "~0.7",
            "vcell": "~0.1",
            "cortex-m-rt": {"version": "~0.7", "optional": True},
        },
        "features": {"rt": ["cortex-m-rt/device"]},
    }

    return cargo_toml_mcu_template


def pac_readme_template(svd_descr: SvdMeta) -> str:
    readme_template = rf"""# {svd_descr.name.upper()}

A peripheral access crate for the {svd_descr.name.upper()} from Silabs for Rust Embedded projects.

## License

The included SVD files are sourced from https://www.silabs.com/documents/public/cmsis-packs and
are licensed under the Zlib (see [LICENSE-3RD-PARTY](LICENSE-3RD-PARTY-Zlib)).

The remainder of the code is under:

- 3-Clause BSD license ([LICENSE-3BSD](LICENSE-3BSD) or https://opensource.org/licenses/BSD-3-Clause)

### Contribution

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in the
work by you, as defined in the BSD-3-Clause license without any additional terms or conditions.
"""

    return readme_template


async def generate_svd2rust_crate(
    pacs_dir: Union[str, pathlib.Path],
    svd_descr: SvdMeta,
    pac_family: str,
    repository: str,
    version: str,
) -> None:
    print(svd_descr.name, svd_descr.path)

    out_dir = pacs_dir.joinpath(f"{pac_family}", f"{svd_descr.name}")

    async with OUT_DIR_LOCK:
        if out_dir.exists():
            return
        else:
            out_dir.mkdir(parents=True)

    async with CRATE_GEN_SEM:
        with tempfile.TemporaryDirectory() as tmpd:
            pret = await asyncio.create_subprocess_exec(
                *["svd2rust", "-i", f"{svd_descr.path}", "-o", f"{tmpd}"]
            )
            await pret.wait()
            assert pret.returncode == 0
            lib_rs = pathlib.Path(tmpd, "lib.rs")
            pret = await asyncio.create_subprocess_exec(
                *[
                    "form",
                    "-i",
                    f"{lib_rs}",
                    "-o",
                    f"{tmpd}/src",
                ],
            )

            await pret.wait()
            assert pret.returncode == 0
            lib_rs.unlink()

            for element in pathlib.Path(tmpd).iterdir():
                shutil.move(element, out_dir)

            with out_dir.joinpath("Cargo.toml").open("w+") as cargo_toml:
                content = mcu_crate_toml_template(svd_descr, repository, version)
                toml.dump(content, cargo_toml)

            pret = await asyncio.create_subprocess_exec(*["cargo", "fmt"], cwd=out_dir)
            await pret.wait()
            assert pret.returncode == 0
            pret = await asyncio.create_subprocess_exec(
                *["rustfmt", "build.rs"], cwd=out_dir
            )
            await pret.wait()
            assert pret.returncode == 0

            with out_dir.joinpath("README.md").open("w+") as readme:
                readme.write(pac_readme_template(svd_descr))


def walk_svd_files(svd_dir: Union[str, pathlib.Path]) -> Iterable[SvdMeta]:
    for svd_file in svd_dir.iterdir():
        if svd_file.suffix.endswith("svd"):
            yield SvdMeta(
                name=re.sub(r"f\d+.*$", "", svd_file.stem.lower()),
                path=svd_file.resolve(),
            )


async def generate_svd2rust_crates(args: argparse.Namespace) -> None:
    svd_dir: Union[str, pathlib.Path] = pathlib.Path(args.svd_dir).resolve()
    pacs_dir = (
        args.out_dir if args.out_dir is not None else svd_dir.parent.joinpath("pacs")
    )
    version = args.version
    repository = args.repo
    tasks = []

    for p in pathlib.Path(svd_dir).iterdir():
        if p.is_dir():
            pac_family = p.name.lower()

            for svd_file in walk_svd_files(p):
                tasks.append(
                    asyncio.create_task(
                        generate_svd2rust_crate(
                            pacs_dir, svd_file, pac_family, repository, version
                        )
                    )
                )

    await asyncio.gather(*tasks)


async def run_cargo_test(project_dir: Union[str, pathlib.Path]) -> None:
    pret = await asyncio.create_subprocess_exec(*["cargo", "test"], cwd=project_dir)
    await pret.wait()
    assert pret.returncode == 0
    pret = await asyncio.create_subprocess_exec(*["cargo", "clean"], cwd=project_dir)
    await pret.wait()
    assert pret.returncode == 0


async def run_pacs_test(args: argparse.Namespace) -> None:
    pacs_dir: Union[str, pathlib.Path] = args.dir
    exclude_dirs: Optional[Iterable[Union[str, pathlib.Path]]] = args.exclude
    tasks = []

    for p in pathlib.Path(pacs_dir).rglob("Cargo.toml"):
        if not any((ex in str(p.resolve()) for ex in exclude_dirs)):
            full_path = p.resolve().parent
            tasks.append(asyncio.create_task(run_cargo_test(full_path)))

    await asyncio.gather(*tasks)


async def publish_crate(pac_dir: Union[str, pathlib.Path], is_dry_run: bool) -> None:
    cmd = ["cargo", "publish"]

    if is_dry_run:
        cmd.append("--dry-run")

    pret = await asyncio.create_subprocess_exec(*cmd, cwd=pac_dir)
    await pret.wait()
    assert pret.returncode == 0
    pret = await asyncio.create_subprocess_exec(*["cargo", "clean"], cwd=pac_dir)
    await pret.wait()
    assert pret.returncode == 0


async def run_publish(args: argparse.Namespace) -> None:
    pacs_dir = pathlib.Path(args.dir).resolve()
    exclude_dirs: Optional[Iterable[Union[str, pathlib.Path]]] = args.exclude
    dry_run = args.dry_run if args.dry_run is not None else False

    for p in pacs_dir.rglob("Cargo.toml"):
        if not any((ex in str(p.resolve()) for ex in exclude_dirs)):
            await publish_crate(p.parent, dry_run)
            delay_minutes = 10 * 60
            await asyncio.sleep(delay_minutes)


def main() -> None:
    parser = argparse.ArgumentParser(description="EFM32 Helper Tooling")
    pacs_parser = parser.add_subparsers(help="Tool command", dest="command")
    pacs = pacs_parser.add_parser("pacs-generate", help="Run PACs generation")
    pacs.add_argument(
        "--svd-dir", required=True, help="SVD files directory to scan for"
    )
    pacs.add_argument(
        "--out-dir",
        help="Output directory for Rust crates output (by default it is set to the same root svd_dir in)",
    )
    pacs.add_argument("--version", required=True, help="Generated crates version")
    pacs.add_argument("--repo", required=True, help="Repository crates assigned tob")

    test = pacs_parser.add_parser("test", help="Run PACs tests")
    test.add_argument("--dir", required=True, help="A PAC directory to run tests in")
    test.add_argument(
        "--exclude", action="append", help="Exclude directory with a PAC from testing"
    )

    publish = pacs_parser.add_parser("publish", help="Publish group of PACs crates")
    publish.add_argument(
        "-n", "--dry-run", action="store_true", help="Dry run publishing"
    )
    publish.add_argument(
        "--dir", required=True, help="Directory to publish crates from"
    )
    publish.add_argument(
        "--exclude", action="append", help="Exclude crate from publishing"
    )

    args = parser.parse_args()
    command_handler = {
        "pacs-generate": generate_svd2rust_crates,
        "test": run_pacs_test,
        "publish": run_publish,
    }

    if command_handler.get(args.command) is not None:
        asyncio.run(command_handler[args.command](args))


if __name__ == "__main__":
    main()
