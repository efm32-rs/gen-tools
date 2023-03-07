[![Scripts Check](https://github.com/efm32-rs/gen-tools/actions/workflows/scripts_check.yml/badge.svg)](https://github.com/efm32-rs/gen-tools/actions/workflows/scripts_check.yml)

## `efm32-rs` tooling

EFM32 Rust project tooling repository that is used for generating PACs for Silabs EFM MCUs

### Prerequisites

- install Python (required version `>=3.8`)
- install [Poetry](https://python-poetry.org/docs/#installation)
- install required Rust packages
    ```bash
    cargo install form svd2rust svdtools
    ```

- install `gen-tools` environment
    ```bash
    poetry install --no-root
    ```

## Known limitations

- **WIP**
