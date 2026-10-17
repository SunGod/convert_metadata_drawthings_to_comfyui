DRAW THINGS -> COMFYUI PNG METADATA CONVERTER
Version 1.00
============================================================
Python 3.9+  |  Windows 10/11 and macOS  |  Standard library only

PURPOSE
-------
This standalone script adds native ComfyUI "workflow" (canvas) and "prompt"
(API graph) metadata to existing PNG files containing Draw Things generation
metadata. Open the converted PNG directly in vanilla ComfyUI by dragging it
onto the canvas to inspect the reconstructed workflow.

The original Draw Things XMP metadata and existing PNG image data are retained.
The script does not generate images, convert or install model files, require
ComfyUI extensions, or read Draw Things project databases (.sqlite3).

Version 1.00 freezes the behavior tested as Alpha 13 (A13). No conversion or
layout changes were made for this release; only version identification and
release documentation were updated.

REQUIREMENTS
------------
* Python 3.9 or newer; no pip packages are required.
* PNGs with recognizable Draw Things metadata, typically in XMP's
  exif:UserComment JSON, stored in PNG iTXt XML:com.adobe.xmp.
* Write permission for the original PNGs and their directory.
* Enough free disk space for temporary staging, plus backups if requested.
* Windows 10/11 or macOS are intended platforms. Linux is supported, but
  --short-filenames requires filesystem birth time to be available.

GETTING STARTED
---------------
Save convert_metadata_drawthings_to_comfyui.py. Open a terminal in the folder
containing the images (or pass a separate file/directory path).

Windows 10/11 (Command Prompt or PowerShell):

  py convert_metadata_drawthings_to_comfyui.py . --dry-run --verbose
  py convert_metadata_drawthings_to_comfyui.py . --backup --verify

macOS:

  python3 convert_metadata_drawthings_to_comfyui.py . --dry-run --verbose
  python3 convert_metadata_drawthings_to_comfyui.py . --backup --verify

The default target is the current directory (.). A directory scan is
non-recursive unless --recursive is specified. Only .png files are scanned;
numbered backup PNGs are excluded. Unrelated files such as JPG, MP4 and PY
are ignored. Individual PNG files can be passed as the path.

RECOMMENDED COMMAND FOR YOUR COLLECTION
---------------------------------------
The following enables BOTH optional model-reference normalization and optional
filename shortening. Start with the preview; then run on original files with
backups and verification.

  py convert_metadata_drawthings_to_comfyui.py . --ckpt-to-safetensors --short-filenames --dry-run --verbose

  py convert_metadata_drawthings_to_comfyui.py . --ckpt-to-safetensors --short-filenames --backup --verify

For macOS, replace "py" with "python3".

ALREADY-CONVERTED IMAGES
------------------------
A PNG already containing ComfyUI workflow/prompt metadata is skipped by
default. Use --overwrite to regenerate its ComfyUI metadata from the
preserved original Draw Things XMP:

  py convert_metadata_drawthings_to_comfyui.py . --overwrite --ckpt-to-safetensors --backup --verify

IMPORTANT: --ckpt-to-safetensors has no effect on an already-converted image
unless --overwrite regenerates its workflow. However, --short-filenames can
rename an already-converted image WITHOUT --overwrite, preserving the
existing ComfyUI metadata byte-for-byte.

OPTIONAL SHORT FILENAMES
------------------------
When --short-filenames is enabled, the exact standard is:

  DrawThings_YYYYMMDD_HHMMSS_ffffff.png

Example:

  DrawThings_20261016_202203_934009.png

YYYYMMDD = original filesystem Date Created in local time, no hyphens.
HHMMSS   = original Date Created time, 24-hour format, including seconds.
ffffff  = original Date Created fractional seconds, six digits (microseconds).

Only the file's creation/birth timestamp is used; Date Modified is NEVER a
fallback. Windows uses native Date Created, macOS uses filesystem birth time,
and Linux requires available filesystem birth time. If creation time is not
available, shortening reports an error for that image rather than making up
a date.

The scheme intentionally ignores ALL original filename prefixes, sequence
numbers, generation seeds and fingerprints. Source filenames do not determine
the new names. Creation timestamps can reflect EXPORT/COPY time rather than
the actual generation time; the script does not claim otherwise.

When two images have the same creation timestamp to the microsecond,
subsequent filenames receive _1, _2, _3, etc. The script never overwrites
an existing destination and scans filenames in deterministic sorted order.
Already-shortened names following this scheme, including collision suffixes,
are kept as-is on subsequent runs. Previous A11/A12 shortened filenames can
be updated to this format. No --overwrite is required for rename-only runs.

With both --short-filenames and --backup, the backup uses the NEW short
basename (such as DrawThings_... .bak1.png) to avoid excessive path length.
Backup contents are the ORIGINAL pre-conversion image bytes.

OPTIONAL DRAW THINGS MODEL FILENAME NORMALIZATION
-------------------------------------------------
--ckpt-to-safetensors applies one all-in-one set of practical conventions to
named references in the GENERATED ComfyUI workflow/prompt and informational
Notes. The original Draw Things metadata retains the original filenames.

Examples:

  Checkpoint: biglove_photo1_f16.ckpt       -> biglove_photo1.safetensors
  Refiner:    biglove_zt3_f16.ckpt          -> biglove_zt3.safetensors
  LoRA:       coolthing_lora_f16.ckpt       -> coolthing.safetensors
  Plain ckpt: anothermodel.ckpt            -> anothermodel.safetensors
  Upscaler:   realesrgan_x2plus_f16.ckpt    -> realesrgan_x2plus.pth
  Restoration: restoreformer_v1.0_f16.ckpt -> restoreformer_v1.0.pth
  ControlNet: control_v11p_sd15_canny_f16.ckpt
                                         -> control_v11p_sd15_canny.pth

Category-specific logic uses common naming conventions: checkpoints, LoRAs,
and refiners map to .safetensors; recognized upscalers and face restorers
map to .pth (a recognized remacri_original variant maps to .pt); classic
ControlNet conventions map to .pth and other recognized ControlNet files
may map to .safetensors. Unknown upscaler/restoration references remain as
recorded rather than receiving a guessed extension. Already .safetensors,
.pth and .pt references are not changed. Original filenames that genuinely
end with "_lora" may occasionally need correction in ComfyUI after the
requested LoRA suffix removal.

This option changes only REFERENCES; it does not convert actual model files.
It also doesn't create functional ComfyUI nodes for processing stages that
are otherwise represented by informational Notes. No directories of installed
models are scanned or matched.

METADATA AND WORKFLOW BEHAVIOR
------------------------------
Input: Draw Things JSON in the source PNG XMP (exif:UserComment).
Output: Native ComfyUI 'workflow' JSON + executable 'prompt' JSON in PNG text
chunks, while preserving existing Draw Things XMP.

FULL    Sufficient original metadata for a best-effort connected workflow.
PARTIAL Recognizable Draw Things JSON exists, but reliable functional mapping
        cannot be built. Creates Note-only informational workflow and an empty
        executable prompt. Does not invent missing generation settings.
NONE    No recognizable Draw Things metadata; file remains unchanged.
ERROR   Invalid file, filesystem error, failed validation, etc.

Native vanilla ComfyUI nodes used as applicable:
  CheckpointLoaderSimple, LoraLoader, CLIPSetLastLayer, CLIPTextEncode,
  EmptyLatentImage, KSampler, VAEDecode, VAEDecodeTiled, ImageScale,
  VAEEncode, SaveImage, Note.

The script reconstructs supported generation settings including checkpoint,
positive/negative prompts, seed, steps, CFG, sampler, scheduler, latent
resolution, and applicable LoRAs. Original LoRA strengths are NOT rescaled;
Draw Things LoRA modes all/base/refiner are generation STAGES, not separate
model/CLIP strength fields. Refiner-only LoRAs are not incorrectly attached
to a nonexistent refiner stage. Untranslatable or ambiguous settings remain
available in the unchanged XMP and, where useful, readable canvas Notes.

The script constructs a Hires Fix second pass only when the recorded settings
AND processing profile establish sufficient evidence. That uses the standard
vanilla pipeline: initial sampling -> VAE decode -> bilinear image resize ->
VAE encode -> second sampling with the recorded Hires Fix strength -> final
VAE decode -> Save Image. The second pass reuses the recorded seed; precise
Draw Things RNG and sampler scheduling are not guaranteed to match. The
"stage2Steps" setting is NOT assumed to mean Hires Fix steps.

Other stages/settings (where not reliably representable) are documented, NOT
silently fabricated: genuine refiner scheduling, external upscale/restoration,
unsupported samplers, advanced guidance, specialized architecture parameters,
unknown img2img source images, etc. Unsupported scheduler names may receive
the documented 'normal' fallback. Generated workflows are best-effort and
NOT a promise of pixel-identical re-generation. ComfyUI may show a red model
node if the referenced local model isn't installed.

The canvas layout uses compact Note columns to the left of functional nodes,
with a single blank line between unrelated informational sections. A two-row
layout is used for reconstructed Hires Fix where applicable. Notes are NOT
included as executable nodes in the ComfyUI API 'prompt' graph.

FILE AND TIMESTAMP PRESERVATION
-------------------------------
* Existing PNG chunks, including the original Draw Things XMP, are retained.
  Compressed IDAT image bytes are copied without image re-encoding.
* The original file object/inode is updated in place, rather than replaced
  with a new file; --short-filenames subsequently renames the existing file.
* On Windows, Date Created, Date Modified and Date Accessed are captured,
  restored and checked at native filesystem timestamp precision. On macOS and
  Linux, available access/modified times are restored; birth time is retained
  by preserving the same file object.
* --verify checks the resulting PNG image data, preserved Draw Things XMP,
  and generated ComfyUI metadata. Normal processing also checks timestamps
  and filesystem file identity.
* --backup creates a numbered copy of the original bytes (.bak1.png, etc.).
  Later backups use .bak2.png and so on; old backups are not overwritten.
* Staged data is validated before overwriting source content, with rollback
  attempted if something fails. An unexpected power loss is not guaranteed
  to be recoverable without --backup.
* Symlink files aren't processed. Directory scanning is non-recursive unless
  requested; .bakN.png backups are skipped on subsequent directory scans.

For valuable collections, test on COPIES first and use --backup --verify.

COMMAND-LINE REFERENCE
----------------------

  path                    File or directory, default: current directory (.)
  --recursive, -r         Include subdirectories
  --backup                Keep numbered pre-conversion backups
  --dry-run               Preview processing without modifying PNGs
  --overwrite             Regenerate existing ComfyUI workflow/prompt
  --ckpt-to-safetensors   Normalize model references by category
  --short-filenames       Use creation timestamp with six microsecond digits
  --verify                Verify written PNG and metadata
  --report FILE.csv       Write per-file CSV status/classification/warnings
  --verbose, -v           Print per-file detail and warnings
  --quiet                 Suppress ordinary console output
  --no-progress           Disable progress bar
  --version               Print version
  --help, -h              Show full help

CSV reporting works during --dry-run too (a CSV file may be created even
though no PNG files are modified). Failure counts yield a nonzero process
exit status. --verbose and --quiet cannot be used together.

EXAMPLES
--------

Convert one image (Windows):
  py convert_metadata_drawthings_to_comfyui.py "image.png" --backup --verify

Convert all PNGs in the current directory, including subfolders:
  py convert_metadata_drawthings_to_comfyui.py . --recursive --backup --verify

Convert previously converted PNGs with the optional model-name rules:
  py convert_metadata_drawthings_to_comfyui.py . --overwrite --ckpt-to-safetensors --backup --verify

Rename already-converted PNGs without modifying their ComfyUI metadata:
  py convert_metadata_drawthings_to_comfyui.py . --short-filenames --verify

Preview and get a CSV manifest:
  py convert_metadata_drawthings_to_comfyui.py . --ckpt-to-safetensors --short-filenames --dry-run --report preview.csv --verbose

BOUNDARIES
----------
* This is for existing Draw Things PNGs, not for JPEG images or image
  conversion, not for A1111 metadata, and not for Draw Things SQLite projects.
* Draw Things PNGs have no established embedded generation date across the
  examined samples. Creation-time filename timestamps come from the OS.
* Generation histories missing from an export cannot be safely fabricated;
  this matters for Hires Fix detection.
* Standalone Python script only; no third-party libraries or ComfyUI
  extensions required. Use the output in vanilla ComfyUI.

Version 1.00 is the frozen release of the user-tested A13 behavior.
