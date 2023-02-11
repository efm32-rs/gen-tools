## `efm32-rs` tooling

### `tools.py` util

Helper tool for making PAC specific operation like PACs generate, check resulted crates, etc.

- generate PAC crate(s) (`SVD_DIR` is the directory where MCU's SVD files may be found):
    ```bash
    python tools.py pacs-gen --svd-dir <SVD_DIR>
    ```

- check the given crate (`DIR` is a directory where a `pacs/` directory can be found):
    ```bash
    python tools.py test --dir <DIR>
    ```

- tag the given PAC repo with the version specified in the `mcu.toml` (`DIR` is a directory where a `pacs/` directory
  can be found):
    ```bash
    python tools.py tag --dir <DIR>
    ```

- publish the given PAC to `crates.io`:
    ```bash
    python tools.py publish --dir <DIR> [--exclude <CRATE_NAME> ...]
    ```

### `batch_gen.py` util

**WIP**

## Known limitations

- **WIP**
