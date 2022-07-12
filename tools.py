# -*- coding: utf-8 -*-
import argparse
import asyncio
import collections
import dataclasses
import logging
import multiprocessing
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from typing import Union, Iterable, Dict, Any, Optional, List

import toml

LICENSE = "BSD-3-Clause"
AUTHORS = ["Vladimir Petrigo <vladimir.petrigo@gmail.com>"]

CRATE_GEN_SEM = asyncio.Semaphore(multiprocessing.cpu_count())
PUBLISH_SEM = asyncio.Semaphore(1)
OUT_DIR_LOCK = asyncio.Lock()

_logger = logging.getLogger(__name__)


@dataclasses.dataclass
class SvdMeta:
    generic_name: str
    full_name: str
    path: pathlib.Path


@dataclasses.dataclass
class PacMeta:
    family: str
    supported_mcus: List[str]
    supported_mcus_full: Dict[str, List[str]]


@dataclasses.dataclass
class EnvMeta:
    svd2rust_ver: str
    crate_ver: str
    repo: str


def get_project_licence() -> str:
    default_license = "BSD-3-Clause"

    return default_license


def get_project_authors() -> Iterable[str]:
    authors = ["Vladimir Petrigo <vladimir.petrigo@gmail.com>"]

    return authors


def mcu_crate_toml_template(
    svd_descr: SvdMeta, repository: str, version: str
) -> Dict[str, Any]:
    cargo_toml_mcu_template = {
        "package": {
            "name": f"{svd_descr.generic_name}-pac",
            "description": f"Peripheral access API for {svd_descr.generic_name.upper()} MCU (generated using svd2rust)",
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
        "features": {
            "default": ["rt"],
            "rt": ["cortex-m-rt/device"],
        },
    }

    return cargo_toml_mcu_template


def mcu_family_crate_toml_template(
    mcu_family: str,
    supported_mcus: List[str],
    repository: str,
    version: str,
    arch: str,
) -> Dict[str, Any]:
    mcu_features = (
        [supported_mcus[0], supported_mcus[-1]]
        if len(supported_mcus) > 1
        else [supported_mcus[0]]
    )
    cargo_toml_mcu_template = {
        "package": {
            "name": f"{mcu_family}-pac",
            "description": f"Peripheral access API for {mcu_family.upper()} MCU (generated using svd2rust)",
            "homepage": repository,
            "version": version,
            "authors": AUTHORS,
            "license": LICENSE,
            "keywords": ["no-std", "arm", "cortex-m", "efm32"],
            "categories": ["embedded", "hardware-support", "no-std"],
            "repository": repository,
            "readme": "README.md",
            "edition": "2021",
            "metadata": {
                "docs": {
                    "rs": {
                        "features": ["rt", *mcu_features],
                        "default-target": arch,
                        "targets": [],
                    }
                }
            },
        },
        "dependencies": {
            "cortex-m": "~0.7",
            "vcell": "~0.1",
            "cortex-m-rt": {"version": "~0.7", "optional": True},
        },
        "features": {
            "default": ["rt"],
            "rt": ["cortex-m-rt/device"],
        },
    }
    cargo_toml_mcu_template["features"].update({m: [] for m in supported_mcus})

    return cargo_toml_mcu_template


def pac_readme_template(svd_descr: SvdMeta) -> str:
    readme_template = rf"""# {svd_descr.generic_name.upper()}

[![crates.io](https://img.shields.io/crates/v/{svd_descr.generic_name}-pac?label={svd_descr.generic_name})](https://crates.io/crates/{svd_descr.generic_name}-pac)

A peripheral access crate for the {svd_descr.generic_name.upper()} from Silabs for Rust Embedded projects.

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
    out_dir = pacs_dir.joinpath(f"{pac_family}", f"{svd_descr.generic_name}")

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


async def generate_mcu_family_crate(
    pacs_dir: Union[str, pathlib.Path],
    svd_descr: SvdMeta,
    pac_family: str,
) -> None:
    crate_root = pacs_dir.joinpath(f"{pac_family}")
    main_lib = crate_root.joinpath("src")
    out_dir = main_lib.joinpath(f"{svd_descr.generic_name}")

    async with OUT_DIR_LOCK:
        if out_dir.exists():
            return
        else:
            out_dir.mkdir(parents=True)

    async with CRATE_GEN_SEM:
        with tempfile.TemporaryDirectory() as tmpd:
            # Generate MCU specific module
            pret = await asyncio.create_subprocess_exec(
                *[
                    "svd2rust",
                    "-m",
                    "-g",
                    "-i",
                    f"{svd_descr.path}",
                    "-o",
                    f"{tmpd}",
                ]
            )
            await pret.wait()
            assert pret.returncode == 0
            gen_root = pathlib.Path(tmpd)
            mod_rs = gen_root.joinpath("mod.rs")
            pret = await asyncio.create_subprocess_exec(
                *[
                    "form",
                    "-i",
                    f"{mod_rs}",
                    "-o",
                    f"{gen_root.joinpath('src')}",
                ],
            )

            await pret.wait()
            assert pret.returncode == 0
            mod_rs.unlink()
            form_lib_rs = pathlib.Path(f"{tmpd}", "src", "lib.rs")
            form_lib_rs.rename(form_lib_rs.parent.joinpath("mod.rs"))

            if not main_lib.joinpath("generic.rs").exists():
                shutil.move(gen_root.joinpath("generic.rs"), main_lib)

            shutil.move(gen_root.joinpath("device.x"), out_dir)

            for element in pathlib.Path(tmpd, "src").iterdir():
                shutil.move(element, out_dir)

            pret = await asyncio.create_subprocess_exec(
                *["rustfmt", f"{out_dir.joinpath('mod.rs')}"], cwd=out_dir
            )
            await pret.wait()
            assert pret.returncode == 0


def walk_svd_files(svd_dir: Union[str, pathlib.Path]) -> Iterable[SvdMeta]:
    for svd_file in svd_dir.iterdir():
        if svd_file.suffix.endswith("svd"):
            yield SvdMeta(
                generic_name=re.sub(r"f\d+.*$", "", svd_file.stem.lower()),
                full_name=svd_file.stem.lower(),
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


async def generate_mcu_family_crates(args: argparse.Namespace) -> Iterable[PacMeta]:
    svd_dir: Union[str, pathlib.Path] = pathlib.Path(args.svd_dir).resolve()
    pacs_dir = (
        args.out_dir if args.out_dir is not None else svd_dir.parent.joinpath("pacs")
    )
    tasks = []
    meta = []

    for p in pathlib.Path(svd_dir).iterdir():
        if p.is_dir():
            pac_family = p.name.lower()
            mcu_list = collections.defaultdict(lambda: [])
            mcu_group = set()

            for svd_file in walk_svd_files(p):
                mcu_group.add(svd_file.generic_name)
                mcu_list[svd_file.generic_name].append(svd_file.full_name)
                tasks.append(
                    asyncio.create_task(
                        generate_mcu_family_crate(pacs_dir, svd_file, pac_family)
                    )
                )

            meta.append(
                PacMeta(
                    family=pac_family,
                    supported_mcus=list(sorted(mcu_group)),
                    supported_mcus_full=mcu_list,
                )
            )

    await asyncio.gather(*tasks)

    return meta


def crate_lib_rs_template(
    pac_family: str, mcu_list: Iterable[str], svd_tool_ver: str
) -> str:
    top_mcu_family = get_mcu_family(pac_family)
    svd_tool_ver = svd_tool_ver.split()[1]
    lib_rs_template = f"""//! Peripheral access API for {pac_family.upper()} microcontrollers
//! (generated using [svd2rust](https://github.com/rust-embedded/svd2rust)
//! {svd_tool_ver})
//!
//! You can find an overview of the API here:
//! [svd2rust/#peripheral-api](https://docs.rs/svd2rust/{svd_tool_ver}/svd2rust/#peripheral-api)
//!
//! For more details see the README here:
//! [efm32-rs](https://github.com/efm32-rs/{top_mcu_family}-pacs)
//!
//! This crate supports all {pac_family.upper()} devices; for the complete list please see:
//! [{pac_family}](https://github.com/efm32-rs/{top_mcu_family}-pacs/pacs/{pac_family})

#![allow(non_camel_case_types)]
#![allow(non_snake_case)]
#![no_std]

mod generic;
pub use self::generic::*;

"""

    mcu_modules = os.linesep.join(
        (
            f"""#[cfg(feature = "{mcu}")]
pub mod {mcu};
"""
            for mcu in mcu_list
        )
    )

    return f"{lib_rs_template}{mcu_modules}"


def create_build_rs_template(mcu_list: Iterable[str]) -> str:
    build_rs_template_header = """use std::env;
use std::fs;
use std::path::PathBuf;

fn main() {
    if env::var_os("CARGO_FEATURE_RT").is_some() {
        let out = &PathBuf::from(env::var_os("OUT_DIR").unwrap());
        println!("cargo:rustc-link-search={}", out.display());
"""
    build_rs_template_footer = """        } else { panic!(\"No device features selected\"); };

        fs::copy(device_file, out.join("device.x")).unwrap();
        println!("cargo:rerun-if-changed={}", device_file);
    }

    println!("cargo:rerun-if-changed=build.rs");
}

"""
    first_condition_template = """        let device_file = if env::var_os("CARGO_FEATURE_{}").is_some() {{
            \"{}\""""
    next_condition_template = """        }} else if env::var_os("CARGO_FEATURE_{}").is_some() {{
            \"{}\""""

    mcu_features = [
        first_condition_template.format(mcu.upper(), f"src/{mcu}/device.x")
        if i == 0
        else next_condition_template.format(mcu.upper(), f"src/{mcu}/device.x")
        for i, mcu in enumerate(mcu_list)
    ]

    return os.linesep.join(
        (build_rs_template_header, *mcu_features, build_rs_template_footer)
    )


def create_crate_readme(pac_meta: PacMeta, env_meta: EnvMeta) -> str:
    readme_template = f"""# {pac_meta.family.upper()}
    
[![crates.io](https://img.shields.io/crates/v/{pac_meta.family}-pac?label={pac_meta.family})](https://crates.io/crates/{pac_meta.family}-pac)

This crate provides an autogenerated API for access to {pac_meta.family.upper()} peripherals.

## Usage

Each device supported by this crate is behind a feature gate so that you only
compile the device(s) you want. To use, in your Cargo.toml:

```toml
[dependencies.{pac_meta.family}-pac]
version = \"{env_meta.crate_ver}\"
features = [\"{pac_meta.supported_mcus[0]}\"]
```

The `rt` feature is enabled by default and brings in support for `cortex-m-rt`.
To disable, specify `default-features = false` in `Cargo.toml`.

For full details on the autogenerated API, please see `svd2rust` Peripheral API [here].
  
[here]: https://docs.rs/svd2rust/{env_meta.svd2rust_ver.split()[1]}/svd2rust/#peripheral-api

## Supported Devices"""
    supported_devices_table = """| Feature | Devices |
|:-----:|:-------:|    
"""

    for mcu in sorted(pac_meta.supported_mcus_full.keys()):
        supported_devices_table += f"|`{mcu}`|"
        supported_devices_table += ", ".join(
            [full_mcu.upper() for full_mcu in pac_meta.supported_mcus_full[mcu]]
        )
        supported_devices_table += "|\n"

    return os.linesep.join((readme_template, supported_devices_table))


def write_crate_lib_rs(
    out_dir: Union[str, pathlib.Path],
    mcu_meta: PacMeta,
    env_meta: EnvMeta,
    mcu_info: Dict[str, Any],
) -> None:
    out_dir = pathlib.Path(out_dir).resolve()
    top_mcu_family = get_mcu_family(mcu_meta.family)

    with out_dir.joinpath("src", "lib.rs").open("w+") as lib_rs:
        lib_rs.write(
            crate_lib_rs_template(
                mcu_meta.family, mcu_meta.supported_mcus, env_meta.svd2rust_ver
            )
        )
        subprocess.run(["rustfmt", out_dir.joinpath("src", "lib.rs")], check=True)

    with out_dir.joinpath("Cargo.toml").open("w+") as cargo_toml:
        toml.dump(
            mcu_family_crate_toml_template(
                mcu_meta.family,
                mcu_meta.supported_mcus,
                env_meta.repo,
                env_meta.crate_ver,
                mcu_info[top_mcu_family]["target"]["arch"][mcu_meta.family],
            ),
            cargo_toml,
        )

    with out_dir.joinpath("build.rs").open("w+") as build_rs:
        build_rs.write(create_build_rs_template(mcu_meta.supported_mcus))
        subprocess.run(["rustfmt", out_dir.joinpath("build.rs")], check=True)

    with out_dir.joinpath("README.md").open("w+") as readme:
        readme.write(create_crate_readme(mcu_meta, env_meta))


def write_repo_readme(
    out_dir: Union[str, pathlib.Path], mcu_env_meta: Dict[str, Any], pacs: List[str]
) -> None:
    mcu_family = get_mcu_family(pacs[0])
    repo_readme_template = f"""# {mcu_env_meta[mcu_family]['name']} support for Rust

[![PACs](https://github.com/efm32-rs/{mcu_family}-pacs/actions/workflows/pacs.yml/badge.svg)](https://github.com/efm32-rs/{mcu_family}-pacs/actions/workflows/pacs.yml)

This repository contains Peripheral Access Crates (PACs) for Silabs' {mcu_family.upper()} series of Cortex-M microcontrollers.
All these crates are automatically generated using [svd2rust](https://github.com/rust-embedded/svd2rust).

Refer to the [CHANGELOG](CHANGELOG.md) to see what changed in the last releases.

## Crates

Every EFM32G chip has its own PAC, listed below:

| Crate           | Docs                                                                                 | crates.io                                                                                                 | Target               |
|-----------------|--------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------|----------------------|"""
    repo_readme_footer_template = """## Device Reference Manuals from Silabs

**WIP**

## License

The included SVD files are sourced from https://www.silabs.com/documents/public/cmsis-packs and
are licensed under the Zlib (see [LICENSE-3RD-PARTY](LICENSE-3RD-PARTY-Zlib)).

The remainder of the code is under:

- 3-Clause BSD license ([LICENSE-3BSD](LICENSE-3BSD) or https://opensource.org/licenses/BSD-3-Clause)

### Contribution

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in the
work by you, as defined in the BSD-3-Clause license without any additional terms or conditions.
"""

    crates_str = ""
    for mcu_pac in pacs:
        crate_name = f"{mcu_pac}-pac"
        crates_str += (
            f"| `{crate_name}`"
            f"| [![docs.rs](https://docs.rs/{crate_name}/badge.svg)](https://docs.rs/{crate_name})"
            f"| [![crates.io](https://img.shields.io/crates/d/{crate_name})](https://crates.io/crates/{crate_name})"
            f"| `{mcu_env_meta[mcu_family]['target']['arch'][mcu_pac]}` |\n"
        )

    with out_dir.joinpath("README.md").open("w") as repo_readme:
        repo_readme.write(
            "\n".join((repo_readme_template, crates_str, repo_readme_footer_template))
        )


def get_mcu_family(mcu_repr: str) -> str:
    result = re.match(r"efm32[a-zA-Z]+", mcu_repr)
    assert result is not None

    return result.group()


async def process_mcu_family_pacs_generation(args: argparse.Namespace) -> None:
    proc = subprocess.run(["svd2rust", "--version"], capture_output=True, check=True)
    svd2rust_version = proc.stdout.decode().strip()
    mcu_list_meta = await generate_mcu_family_crates(args)
    _logger.debug(f"svd2rust tool version: {svd2rust_version}")
    svd_dir: Union[str, pathlib.Path] = pathlib.Path(args.svd_dir).resolve()
    pacs_dir = (
        args.out_dir if args.out_dir is not None else svd_dir.parent.joinpath("pacs")
    )
    mcu_info = toml.load("mcu.toml")
    _logger.debug(f"MCU info: {mcu_info}")
    generated_pacs = []

    for mcu_meta in mcu_list_meta:
        generated_pacs.append(mcu_meta.family)
        high_level_family = get_mcu_family(mcu_meta.family)
        _logger.debug(
            (
                f"Crate [{mcu_meta.family}] - {mcu_info[high_level_family]['name']} "
                f"arch: {mcu_info[high_level_family]['target']['arch'][mcu_meta.family]}"
            )
        )
        env_meta = EnvMeta(
            svd2rust_ver=svd2rust_version,
            crate_ver=mcu_info[high_level_family]["version"],
            repo=mcu_info[high_level_family]["repository"],
        )
        write_crate_lib_rs(
            pacs_dir.joinpath(mcu_meta.family), mcu_meta, env_meta, mcu_info
        )

    repo_root = pacs_dir.parent
    write_repo_readme(repo_root, mcu_info, generated_pacs)
    _logger.info(f"PACS: {generated_pacs}")


async def run_cargo_test(project_dir: Union[str, pathlib.Path]) -> None:
    crate_mcu_features = filter(
        lambda x: x.startswith("efm32"),
        toml.load(project_dir.joinpath("Cargo.toml"))["features"],
    )
    mcu_info_meta = toml.load("mcu.toml")
    target_arch = mcu_info_meta[get_mcu_family(project_dir.name)]["target"]["arch"][
        project_dir.name
    ]

    for feature in crate_mcu_features:
        pret = await asyncio.create_subprocess_exec(
            *[
                "cargo",
                "check",
                "--features",
                f"rt, {feature}",
                "--target",
                target_arch,
            ],
            cwd=project_dir,
        )
        await pret.wait()
        assert pret.returncode == 0
        pret = await asyncio.create_subprocess_exec(
            *["cargo", "clean"], cwd=project_dir
        )
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
    cmd = ["cargo", "publish", "--no-default-features"]

    if is_dry_run:
        cmd.append("--dry-run")

    pret = await asyncio.create_subprocess_exec(*cmd, cwd=pac_dir)
    await pret.wait()
    # assert pret.returncode == 0
    pret = await asyncio.create_subprocess_exec(*["cargo", "clean"], cwd=pac_dir)
    await pret.wait()
    assert pret.returncode == 0


async def run_publish(args: argparse.Namespace) -> None:
    pacs_dir = pathlib.Path(args.dir).resolve()
    exclude_dirs: Optional[Iterable[Union[str, pathlib.Path]]] = args.exclude
    dry_run = args.dry_run if args.dry_run is not None else False

    for p in pacs_dir.rglob("Cargo.toml"):
        if exclude_dirs is None or not any(
            (ex in str(p.resolve()) for ex in exclude_dirs)
        ):
            await publish_crate(p.parent, dry_run)
            delay_minutes = 5 * 60
            await asyncio.sleep(delay_minutes)


def main() -> None:
    logging.basicConfig(level=logging.DEBUG)
    parser = argparse.ArgumentParser(description="EFM32 Helper Tooling")
    pacs_parser = parser.add_subparsers(help="Tool command", dest="command")
    pacs = pacs_parser.add_parser("pacs-gen", help="Run PACs generation")
    pacs.add_argument(
        "--svd-dir", required=True, help="SVD files directory to scan for"
    )
    pacs.add_argument(
        "--out-dir",
        help="Output directory for Rust crates output (by default it is set to the same root svd_dir in)",
    )

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
        "pacs-gen": process_mcu_family_pacs_generation,
        "test": run_pacs_test,
        "publish": run_publish,
    }

    if command_handler.get(args.command) is not None:
        asyncio.run(command_handler[args.command](args))


if __name__ == "__main__":
    main()
