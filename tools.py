# -*- coding: utf-8 -*-
"""
`tools.py` util
===============

Helper tool for making PAC specific operation like PACs generate, check resulted crates, etc.

- generate PAC crate(s) (`SVD_DIR` is the directory where MCU's SVD files may be found):

    python tools.py pacs-gen --svd-dir <SVD_DIR>

- check the given crate (`DIR` is a directory where a `pacs/` directory can be found)::

    python tools.py test --dir <DIR>

- tag the given PAC repo with the version specified in the `mcu.toml` (`DIR` is a directory where a `pacs/` directory
  can be found)::

    python tools.py tag --dir <DIR>

- publish the given PAC to `crates.io`::

    python tools.py publish --dir <DIR> [--exclude <CRATE_NAME> ...]
"""

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
    """Get project license

    :return: project license SPDX string
    """
    return LICENSE


def get_project_authors() -> Iterable[str]:
    """Get project authors' name

    :return: project author iterable sequence
    """
    return AUTHORS


def get_mcu_family(mcu_repr: str) -> str:
    """Get MCU family name from a part number/group name

    :param mcu_repr: MCU part number string
    :type mcu_repr: str
    :return: MCU family string
    """
    result = re.match(r"efm32[a-zA-Z]+", mcu_repr)
    assert result is not None

    return result.group()


def get_target_arch(
    mcu_family: str, pac_name: str, mcu_meta_info: Dict[str, Any]
) -> str:
    """Get target architecture for the given MCU and PAC names

    :param mcu_family: MCU family name
    :param pac_name: PAC name
    :param mcu_meta_info: MCU metadata from the `mcu.toml` file
    :return: target architecture string (e.g. `thumbv7m-none-eabi`)
    """
    return mcu_meta_info[mcu_family]["target"]["arch"][pac_name]


def create_no_atomic_platform_config() -> str:
    """Generate a `config.toml` file to allow targets without atomic compare-and-swap (CAS) support
    to be used with `portable-atomic` crate

    :return: TOML string representation
    """
    return toml.dumps(
        dict(build=dict(rustflags=["--cfg=portable_atomic_unsafe_assume_single_core"]))
    )


def generate_pac_crate_toml(
    mcu_family: str,
    supported_mcus: List[str],
    repository: str,
    version: str,
    arch: str,
) -> Dict[str, Any]:
    """Get PAC `Cargo.toml` representation

    :param mcu_family: PAC MCU family name
    :param supported_mcus: a list of supported MCUs
    :param repository: PAC repository URL
    :param version: PAC crate version
    :param arch: PAC architecture
    :return: TOML dictionary that describes PAC `Cargo.toml`
    """

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
            "portable-atomic": {"version": "~1", "default-features": False},
            "critical-section": {"version": "~1", "optional": True},
            "cortex-m-rt": {"version": "~0.7", "optional": True},
        },
        "features": {
            "default": ["rt"],
            "rt": ["cortex-m-rt/device"],
            "critical-section": ["dep:critical-section"],
        },
    }
    cargo_toml_mcu_template["features"].update({m: [] for m in supported_mcus})

    if arch == "thumbv6m-none-eabi":
        cargo_toml_mcu_template["features"]["critical-section"].append(
            "portable-atomic/critical-section"
        )

    return cargo_toml_mcu_template


def generate_repository_readme(svd_descr: SvdMeta) -> str:
    """Generate repository README.md from the template where all available PACs can be found

    :param svd_descr: SVD files metadata
    :return: PAC README.md representation
    """
    readme_template = rf"""# {svd_descr.generic_name.upper()}\
    
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


def generate_lib_rs(pac_family: str, mcu_list: Iterable[str], svd_tool_ver: str) -> str:
    """Generate PAC `lib.rs` content

    :param pac_family: PAC family name
    :param mcu_list: a list of MCU that the PAC supports
    :param svd_tool_ver: `svd2rust` tool version
    :return: `lib.rs` content
    """
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


def generate_build_rs(mcu_list: Iterable[str]) -> str:
    """Generate PAC `build.rs` content

    :param mcu_list: a list of supported MCU names
    :return: `build.rs` content
    """
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


async def generate_pac_crate(
    pacs_dir: Union[str, pathlib.Path],
    svd_descr: SvdMeta,
    pac_family: str,
    patch_file: Optional[Union[str, pathlib.Path]],
) -> None:
    """Generate a PAC

    :param pacs_dir: PAC output directory
    :param svd_descr: SVD files metadata
    :param pac_family: PAC name
    :param patch_file: Patch file for the given crate
    """
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
            # Patch MCU SVD if needed
            if patch_file is not None:
                await _create_patched_svd(patch_file, svd_descr, tmpd)
            # Generate MCU specific module
            pret = await asyncio.create_subprocess_exec(
                *[
                    "svd2rust",
                    "-m",
                    "-g",
                    "--atomics",
                    "-i",
                    f"{svd_descr.path}",
                    "-o",
                    f"{tmpd}",
                ],
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
                *["rustfmt", f"{out_dir.joinpath('mod.rs')}"],
                cwd=out_dir,
            )
            await pret.wait()
            assert pret.returncode == 0


def walk_svd_files(svd_dir: Union[str, pathlib.Path]) -> Iterable[SvdMeta]:
    """Walk through a directory with MCU specific SVD files

    .. note::
        It is expected that SVD files have `.svd` file extension suffix

    :param svd_dir: Directory where SVD files can be found
    :return: SVD file metadata iterable
    """
    for svd_file in svd_dir.iterdir():
        if svd_file.suffix.endswith("svd"):
            yield SvdMeta(
                generic_name=re.sub(r"f\d+.*$", "", svd_file.stem.lower()),
                full_name=svd_file.stem.lower(),
                path=svd_file.resolve(),
            )


async def generate_mcu_family_crates(args: argparse.Namespace) -> Iterable[PacMeta]:
    """Generate MCU family PACs

    This function finds all MCU family specific SVD files in the specified directory where
    `svd/` directory can be found and generate a PAC for MCU group(s). Then it returns
    all generated PACs metadata for further processing if required

    :param args: CLI arguments
    :return: PAC metadata iterable
    """
    svd_dir: Union[str, pathlib.Path] = pathlib.Path(args.svd_dir).resolve()
    pacs_dir = (
        pathlib.Path(args.out_dir).resolve()
        if args.out_dir is not None
        else svd_dir.parent.joinpath("pacs")
    )
    tasks = []
    meta = []

    for p in pathlib.Path(svd_dir).iterdir():
        if p.is_dir():
            pac_family = p.name.lower()
            mcu_list = collections.defaultdict(list)
            mcu_group = set()
            patch_dir = p / "patch"
            if patch_dir.is_dir():
                patches = [
                    patch_file
                    for patch_file in patch_dir.iterdir()
                    if patch_file.suffix == ".yaml"
                ]
            else:
                patches = []

            for svd_file in walk_svd_files(p):
                mcu_group.add(svd_file.generic_name)
                mcu_list[svd_file.generic_name].append(svd_file.full_name)
                group_patch = next(
                    (
                        patch
                        for patch in patches
                        if svd_file.generic_name == patch.stem.lower()
                    ),
                    None,
                )
                tasks.append(
                    asyncio.create_task(
                        generate_pac_crate(pacs_dir, svd_file, pac_family, group_patch)
                    )
                )

            meta.append(
                PacMeta(
                    family=pac_family,
                    supported_mcus=sorted(mcu_group),
                    supported_mcus_full=mcu_list,
                )
            )

    await asyncio.gather(*tasks)

    return meta


def generate_pac_readme(pac_meta: PacMeta, env_meta: EnvMeta) -> str:
    """Generate PAC README.md

    :param pac_meta: PAC metadata
    :param env_meta: environment metadata (`svd2rust` version, repo URL, etc.)
    :return: `README.md` content
    """
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


def _write_crate_lib_rs(
    out_dir: Union[str, pathlib.Path],
    mcu_meta: PacMeta,
    env_meta: EnvMeta,
    mcu_info: Dict[str, Any],
) -> None:
    """Write PAC crate specific files

    The following files are to be written:
    - `Cargo.toml`
    - `lib.rs`
    - `build.rs`
    - `README.md`

    For architectures that do not support atomic compare-and-swap (e.g. `ARM-v6`)
    a `.cargo/config.coml` file is also populated with `portable-atomic` crate
    specific configuration option to allow such operations for a crate

    :param out_dir: PAC output directory
    :param mcu_meta: Parsed PAC metadata
    :param env_meta: Environment metadata (`svd2rust` version, crate version, etc.)
    :param mcu_info: MCU metadata from `mcu.toml` file
    """
    out_dir = pathlib.Path(out_dir).resolve()
    top_mcu_family = get_mcu_family(mcu_meta.family)
    target_arch = get_target_arch(top_mcu_family, mcu_meta.family, mcu_info)

    with out_dir.joinpath("src", "lib.rs").open("w+") as lib_rs:
        lib_rs.write(
            generate_lib_rs(
                mcu_meta.family, mcu_meta.supported_mcus, env_meta.svd2rust_ver
            )
        )
        subprocess.run(["rustfmt", out_dir.joinpath("src", "lib.rs")], check=True)

    with out_dir.joinpath("Cargo.toml").open("w+") as cargo_toml:
        toml.dump(
            generate_pac_crate_toml(
                mcu_meta.family,
                mcu_meta.supported_mcus,
                env_meta.repo,
                env_meta.crate_ver,
                target_arch,
            ),
            cargo_toml,
        )

    with out_dir.joinpath("build.rs").open("w+") as build_rs:
        build_rs.write(generate_build_rs(mcu_meta.supported_mcus))
        subprocess.run(["rustfmt", out_dir.joinpath("build.rs")], check=True)

    with out_dir.joinpath("README.md").open("w+") as readme:
        readme.write(generate_pac_readme(mcu_meta, env_meta))

    if target_arch in ("thumbv6m-none-eabi",):
        config_dir = out_dir.joinpath(".cargo")

        if not config_dir.exists():
            config_dir.mkdir()

        with out_dir.joinpath(".cargo", "config.toml").open("w+") as config:
            config.write(create_no_atomic_platform_config())


def _write_repo_readme(
    out_dir: Union[str, pathlib.Path], mcu_env_meta: Dict[str, Any], pacs: List[str]
) -> None:
    """Write a `README.md` file with PACs description

    :param out_dir: output directory where `README.md` should be written to
    :param mcu_env_meta: MCU metadata from `mcu.toml` file
    :param pacs: a list of generated PACs string name
    """
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


async def _create_patched_svd(
    patch_file: pathlib.Path, svd_descr: SvdMeta, tmp_dir: str
) -> None:
    """Create a patched SVD file with the `svdtools` util

    :param patch_file: SVD patch file path
    :param svd_descr: SVD file meta information
    :param tmp_dir: Temporary directory to copy patched SVD to
    """
    tmp_dir_path = pathlib.Path(tmp_dir)
    patch_file = pathlib.Path(patch_file)
    tmp_patch = tmp_dir_path / patch_file.name
    peripheral_dir = patch_file.parent / "peripheral"
    with patch_file.open() as fp, tmp_patch.open("w") as tp:
        patch_content = fp.read()
        tp.write(
            patch_content.format(svd_path=svd_descr.path, peripheral_dir=peripheral_dir)
        )
    pret = await asyncio.create_subprocess_exec(
        *[
            "svdtools",
            "patch",
            f"{tmp_patch}",
        ],
    )
    await pret.wait()
    assert pret.returncode == 0
    shutil.move(svd_descr.path.with_suffix(".svd.patched"), f"{tmp_dir}")
    svd_patched_name = svd_descr.path.with_suffix(".svd.patched").name
    svd_descr.path = tmp_dir_path / svd_patched_name


async def _publish_crate(
    pac_dir: Union[str, pathlib.Path], mcu_meta_info: Dict[str, Any], is_dry_run: bool
) -> None:
    """Execute `cargo publish` in the given PAC directory

    :param pac_dir: PAC directory
    :param mcu_meta_info: MCU metadata from `mcu.toml` file
    :param is_dry_run: flag that shows whether a crate should be published to the real registry or not
    """
    mcu_family = get_mcu_family(pac_dir.name)
    pac_name = pac_dir.name
    target = get_target_arch(mcu_family, pac_name, mcu_meta_info)
    cmd = ["cargo", "publish", "--no-default-features", "--target", f"{target}"]

    if is_dry_run:
        cmd.append("--dry-run")

    pret = await asyncio.create_subprocess_exec(*["cargo", "clean"], cwd=pac_dir)
    await pret.wait()
    pret = await asyncio.create_subprocess_exec(*cmd, cwd=pac_dir)
    await pret.wait()
    assert pret.returncode == 0
    pret = await asyncio.create_subprocess_exec(*["cargo", "clean"], cwd=pac_dir)
    await pret.wait()
    assert pret.returncode == 0


async def _cargo_check(project_dir: Union[str, pathlib.Path]) -> None:
    """Execute `cargo check` command with the necessary arguments in a PAC directory

    :param project_dir: a PAC directory to run `cargo check` in
    """
    crate_mcu_features = filter(
        lambda x: x.startswith("efm32"),
        toml.load(project_dir.joinpath("Cargo.toml"))["features"],
    )
    mcu_meta_info = toml.load("mcu.toml")
    mcu_family = get_mcu_family(project_dir.name)
    pac_name = project_dir.name
    target_arch = get_target_arch(mcu_family, pac_name, mcu_meta_info)

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
            *["cargo", "clean"],
            cwd=project_dir,
        )
        await pret.wait()
        assert pret.returncode == 0


async def run_pacs_generation(args: argparse.Namespace) -> None:
    """Generate PAC(s) with the `svd2rust` tool

    All metadata related to a MCU family is extracted from the `mcu.toml` file.
    In the case a new MCU family available, make sure to update `mcu.toml` with
    the following info::

        [<mcu-family-name>]
        name = "<mcu-family-string-representation>"
        repository = "<pacs-repository URL>"
        version = "<pacs-version>"

        [<mcu-family-name>.target.arch]
        <mcu-family-name>/<mcu-subgroup-name> = "<target-arch>"

    MCU family name is something that describes a group of chips without digits related to memory
    size, etc. E.g.:

    - `EFM32GG` is the MCU family
    - 3 subgroups available in the family: `EFM32GG`, `EFM32GG11B`, `EFM32GG12B`

    :param args: CLI arguments
    :type args: argparse.Namespace
    """
    proc = subprocess.run(["svd2rust", "--version"], capture_output=True, check=True)
    svd2rust_version = proc.stdout.decode().strip()
    mcu_list_meta = await generate_mcu_family_crates(args)
    _logger.debug(f"svd2rust tool version: {svd2rust_version}")
    svd_dir: Union[str, pathlib.Path] = pathlib.Path(args.svd_dir).resolve()
    pacs_dir = (
        pathlib.Path(args.out_dir).resolve()
        if args.out_dir is not None
        else svd_dir.parent.joinpath("pacs")
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
                f"arch: {get_target_arch(high_level_family, mcu_meta.family, mcu_info)}"
            )
        )
        env_meta = EnvMeta(
            svd2rust_ver=svd2rust_version,
            crate_ver=mcu_info[high_level_family]["version"],
            repo=mcu_info[high_level_family]["repository"],
        )
        _write_crate_lib_rs(
            pacs_dir.joinpath(mcu_meta.family), mcu_meta, env_meta, mcu_info
        )

    repo_root = pacs_dir.parent
    _write_repo_readme(repo_root, mcu_info, generated_pacs)
    _logger.info(f"PACS: {generated_pacs}")


async def run_pacs_test(args: argparse.Namespace) -> None:
    """Run `cargo check` for the available PAC(s)

    This function finds all subdirectories with a `Cargo.toml` file in it
    and execute `cargo check` with MCU specific options to verify whether
    a crate passes that or not

    `--dir` argument should point to PAC(s) root repository/directory

    :param args: CLI arguments
    :type args: argparse.Namespace
    """
    pacs_dir: Union[str, pathlib.Path] = args.dir
    exclude_dirs: Optional[Iterable[Union[str, pathlib.Path]]] = (
        args.exclude if args.exclude is not None else []
    )
    tasks = []

    for p in pathlib.Path(pacs_dir).rglob("Cargo.toml"):
        if not any((ex in str(p.resolve()) for ex in exclude_dirs)):
            full_path = p.resolve().parent
            tasks.append(asyncio.create_task(_cargo_check(full_path)))

    await asyncio.gather(*tasks)


async def run_publish(args: argparse.Namespace) -> None:
    """Run `cargo publish` command for the given crate

    Supported features:

    - `-n`/`--dry-run`: make a dry run publishing call without uploading a crate to the registry
    - `--exclude`: exclude specified crate directory from publishing. This flag can be used multiple times

    .. note::

        Without the dry-run feature enabled, there is a delay between publishing crates
        equal to 5 minutes to avoid `crates.io` limits

    :param args: CLI arguments
    :type args: argparse.Namespace
    """
    pacs_dir = pathlib.Path(args.dir).resolve()
    exclude_dirs: Optional[Iterable[Union[str, pathlib.Path]]] = args.exclude
    is_dry_run = args.dry_run
    mcu_info_meta = toml.load("mcu.toml")

    for p in pacs_dir.rglob("Cargo.toml"):
        if exclude_dirs is None or not any(
            (ex in str(p.resolve()) for ex in exclude_dirs)
        ):
            await _publish_crate(p.parent, mcu_info_meta, is_dry_run)

            if not is_dry_run:
                delay_minutes = 5 * 60
                await asyncio.sleep(delay_minutes)


async def run_tagging(args: argparse.Namespace) -> None:
    """Assign a tag specified in the metadata file (`mcu.toml`) to the repository

    The function checks whether a tag exists and tries to push that tag to the default
    remote repository.

    :param args: CLI arguments
    :type args: argparse.Namespace
    """
    pacs_dir = pathlib.Path(args.dir).resolve()
    mcu_info_meta = toml.load("mcu.toml")
    version = mcu_info_meta[get_mcu_family(pacs_dir.name)]["version"]
    check_tag_not_exists_cmd = ["git", "rev-parse", "-q", "--verify", f"{version}"]
    pret = await asyncio.create_subprocess_exec(*check_tag_not_exists_cmd, cwd=pacs_dir)
    ret = await pret.wait()
    _logger.debug(f"Tag PAC {pacs_dir.name}: {version}")

    if ret != 0:
        tag_cmd = ["git", "tag", f"{version}"]
        pret = await asyncio.create_subprocess_exec(*tag_cmd, cwd=pacs_dir)
        await pret.wait()

    push_cmd = ["git", "push", "--tags"]
    pret = await asyncio.create_subprocess_exec(*push_cmd, cwd=pacs_dir)
    await pret.wait()


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
        "-n", "--dry-run", default=False, action="store_true", help="Dry run publishing"
    )
    publish.add_argument(
        "--dir", required=True, help="Directory to publish crates from"
    )
    publish.add_argument(
        "--exclude", action="append", help="Exclude crate from publishing"
    )
    tagging = pacs_parser.add_parser(
        "tag", help="Tag a PAC crate with the release version"
    )
    tagging.add_argument("--dir", required=True, help="Directory to set a Git tag")

    args = parser.parse_args()
    command_handler = {
        "pacs-gen": run_pacs_generation,
        "test": run_pacs_test,
        "publish": run_publish,
        "tag": run_tagging,
    }

    if command_handler.get(args.command) is not None:
        asyncio.run(command_handler[args.command](args))


if __name__ == "__main__":
    main()
