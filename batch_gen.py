# -*- coding: utf-8 -*-
import pathlib
import subprocess
from collections import namedtuple

RsMcuContext = namedtuple("RsMcuContext", ["path", "repo", "version"])

PROJECTS = (
    "efm32g-pacs",
    "efm32gg-pacs",
    "efm32hg-pacs",
    "efm32jg-pacs",
    "efm32lg-pacs",
    "efm32pg-pacs",
    "efm32tg-pacs",
    "efm32wg-pacs",
    "efm32zg-pacs",
)
PROJECTS_CTX = (
    RsMcuContext(
        path=pathlib.Path(f"../{p}").resolve(),
        repo=f"https://github.com/efm32-rs/{p}",
        version="0.1.0",
    )
    for p in PROJECTS
)

if __name__ == "__main__":
    for p in PROJECTS_CTX:
        print(p)
        subprocess.run(
            [
                "poetry",
                "run",
                "python",
                "tools.py",
                "--svd-dir",
                str(p.path.joinpath("svd")),
                "--version",
                p.version,
                "--repo",
                p.repo,
                "pacs-generate",
            ],
            check=True,
        )
