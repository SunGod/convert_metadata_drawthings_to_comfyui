# Draw Things → ComfyUI PNG Metadata Converter
**A handy little Python script that will take a directory full of PNGs exported from DrawThings, and convert their metadata so that the same PNGs can also be used in ComfyUI.**

**Version 1.00** · Python 3.9+ · Windows 10/11, macOS · Standard library only · No custom ComfyUI nodes

Convert **existing Draw Things PNGs** into images that can be dragged directly onto a **vanilla ComfyUI** canvas to load a connected, best-effort workflow.

The converter adds ComfyUI `workflow` and `prompt` JSON to the PNG, preserving the image's original Draw Things XMP and compressed pixel data. It does **not** generate new images, install models, or modify Draw Things projects.

> **Release note:** v1.00 freezes the tested **Alpha 13 (A13)** functionality. The release updates version identification and documentation, not workflow behavior.

## Quick start

Download **[`convert_metadata_drawthings_to_comfyui.py`](convert_metadata_drawthings_to_comfyui.py)**. No installation or `pip install` is necessary beyond Python 3.9+.

**Windows 10/11 — Command Prompt or PowerShell**

```cmd
py convert_metadata_drawthings_to_comfyui.py . --dry-run --verbose
py convert_metadata_drawthings_to_comfyui.py . --backup --verify
```

**macOS**

```bash
python3 convert_metadata_drawthings_to_comfyui.py . --dry-run --verbose
python3 convert_metadata_drawthings_to_comfyui.py . --backup --verify
```

`.` means the current directory; you can instead supply a folder path or one `.png` file. Subdirectories are excluded unless you specify `--recursive`.

### Recommended optional features

```cmd
py convert_metadata_drawthings_to_comfyui.py . --ckpt-to-safetensors --short-filenames --backup --verify
```

- **`--ckpt-to-safetensors`** adjusts known Draw Things **model filename references** for the file formats commonly used in ComfyUI. Checkpoints and LoRAs generally map to `.safetensors`; recognized upscaler, restoration, and conditioning-model references use category-specific conventions such as `.pth`. This is one flag for supported categories; it doesn't convert the model files themselves.
- **`--short-filenames`** optionally renames images using their *filesystem creation timestamps*, ignoring Draw Things' long prompt-derived filenames.
- **`--backup --verify`** keeps a copy of the source file and verifies the metadata and image-data invariants.

Use `--dry-run` first if you want to preview the changes.

## Optional short filename format

```text
DrawThings_YYYYMMDD_HHMMSS_ffffff.png
```

Example: `DrawThings_20261016_202203_934009.png`

The date and 24-hour time come **only from filesystem Date Created**, including **six fractional-second digits (microseconds)**. The converter never substitutes Date Modified. It intentionally ignores any leading export index, trailing seed, or fingerprint in the original filename.

If two images share the same creation timestamp to the microsecond, the additional images get `_1`, `_2`, `_3`, and so forth. No existing destination file is overwritten. Filenames already matching this convention remain unchanged on subsequent runs; earlier A11/A12 shortened filenames can be renamed again without rebuilding ComfyUI metadata.

**Note:** File creation timestamps may reflect when Draw Things exported or a tool copied the PNG, not the original generation time. On Linux, the optional shortening operation requires accessible filesystem birth time. On Windows and macOS, the native creation/birth timestamp is used.

## Filename reference normalization

The optional `--ckpt-to-safetensors` flag changes **generated ComfyUI references only**—not the source Draw Things XMP or any actual model files.

| Recorded Draw Things filename | Output reference |
| --- | --- |
| `biglove_photo1_f16.ckpt` (checkpoint) | `biglove_photo1.safetensors` |
| `coolthing_lora_f16.ckpt` (LoRA) | `coolthing.safetensors` |
| `biglove_zt3_f16.ckpt` (refiner) | `biglove_zt3.safetensors` |
| `realesrgan_x2plus_f16.ckpt` (upscaler) | `realesrgan_x2plus.pth` |
| `restoreformer_v1.0_f16.ckpt` (face restoration) | `restoreformer_v1.0.pth` |
| `control_v11p_sd15_canny_f16.ckpt` (ControlNet) | `control_v11p_sd15_canny.pth` |

Known suffix transformations are practical naming conventions, **not guaranteed recovery of the originally downloaded filename**. Some names may require manual correction in ComfyUI. Unrecognized upscaler/restoration references are left unchanged rather than assigned an invented extension. Filenames in informational Notes are normalized when recognized, even when that processing stage is not reconstructed as functional nodes.

## What gets reconstructed?

The output PNG includes a connected workflow with **standard ComfyUI nodes** where the source data makes reconstruction defensible. Depending on the image, this may cover:

- Checkpoint, supported LoRAs, prompts, seed, steps, CFG, supported sampler/scheduler, and generation resolution.
- CLIP skip and tiled VAE decoding where supported.
- **Conditional two-pass Hires Fix** when configuration **and recorded processing events** support it: first-pass sampling → VAE decode → bilinear resize → VAE encode → second sampling → final decode/save.
- Native **Note nodes** in compact left-side columns for recorded settings that cannot be accurately represented as executable processing.

Original LoRA weights are used as recorded. Draw Things LoRA modes (`all`, `base`, `refiner`) describe **generation stages**, not independently adjustable model-versus-CLIP weights. A refiner-only LoRA is not incorrectly added to the base-stage graph.

Untranslated settings remain in the original XMP and, where meaningful, Notes. This includes genuinely different refiners, advanced guidance and model-specific behavior, external upscaling, and face restoration. The approved `normal` scheduler fallback is used when appropriate. **ComfyUI output is a best-effort translation, not a guarantee of identical regenerated pixels.**

For the second Hires Fix pass, the recorded seed is reused because the original second-pass RNG state isn't embedded; `stage2Steps` is **not** assumed to be the Hires Fix steps value.

## Data preservation and safety

- **PNG IDAT data is preserved**: source image data is not decoded and re-encoded.
- Existing Draw Things XMP is **retained**, including the original model filenames and weights.
- The source file is updated **in place**; optional renaming preserves its filesystem identity.
- On Windows, **Date Created, Date Modified, and Date Accessed** are captured, restored, and checked. On macOS, modification/access times are restored and birth time is retained through the same file object.
- `--backup` creates numbered backups such as `.bak1.png` without replacing older backups.
- `--verify` checks preserved image data, original XMP, and generated metadata after writing.
- Symlinks and numbered backup files are skipped during scans; other file types are ignored.

For large or irreplaceable image collections, test on copies first. Use `--backup --verify` for important runs.

## Existing conversions and classification

Running the script again on a PNG with ComfyUI metadata normally skips conversion. To **rebuild its ComfyUI workflow from the preserved Draw Things metadata**, use `--overwrite`:

```cmd
py convert_metadata_drawthings_to_comfyui.py . --overwrite --ckpt-to-safetensors --backup --verify
```

`--short-filenames` can rename an already-converted image **without** `--overwrite` and without rewriting its workflow.

| Classification | Meaning |
| --- | --- |
| **FULL** | Enough metadata for a best-effort functional ComfyUI graph |
| **PARTIAL** | Recognizable Draw Things data exists, but a functional graph cannot be built safely; output has informational Notes only |
| **NONE** | Draw Things metadata absent; leave the PNG unchanged |
| **ERROR** | Invalid PNG, permissions, or other processing failure |

### Command-line options

| Argument | Purpose |
| --- | --- |
| `path` | One `.png` or directory; defaults to `.` |
| `--recursive`, `-r` | Scan subdirectories |
| `--backup` | Keep numbered copies of pre-conversion files |
| `--dry-run` | Preview without modifying PNGs |
| `--overwrite` | Rebuild existing ComfyUI metadata |
| `--ckpt-to-safetensors` | Normalize supported model reference filenames |
| `--short-filenames` | Use Date Created and microseconds for filenames |
| `--verify` | Verify output and preserved content |
| `--report results.csv` | Create per-file CSV status report |
| `--verbose`, `-v` | Show per-file details |
| `--quiet` | Suppress ordinary output |
| `--no-progress` | Suppress progress bar |
| `--version` / `--help` | Show version or help |

`--report` may write a CSV even during `--dry-run`; it does not change image files.

## Supported input and limitations

- **Input:** Draw Things-generated `.png` files with embedded XMP `exif:UserComment` JSON. A1111 metadata and JPG conversion are not supported by this script.
- **Output:** PNGs with vanilla ComfyUI `workflow` and `prompt` metadata.
- **Not included:** Draw Things `.sqlite3` project parsing, generation history recovery, model file conversion, automatic model installation, or reconstruction of unsupported operations.
- **Platform:** Python 3.9+ on Windows 10/11 and macOS; Linux works for ordinary conversion, with filesystem birth-time availability required for `--short-filenames`.

For full details, examples, and operational cautions, read [`convert_metadata_drawthings_to_comfyui_README.txt`](convert_metadata_drawthings_to_comfyui_README.txt).

---

**v1.00** — Stable release of the user-tested A13 converter. The separate **ComfyThings (CT)** project is intended to explore direct Draw Things project-database exports; it is not part of this converter.
