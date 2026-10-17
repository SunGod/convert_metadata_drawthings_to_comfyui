#!/usr/bin/env python3
"""Draw Things PNG -> vanilla ComfyUI workflow/prompt metadata, version 1.00.

Python 3.9+, standard library only. Preserves existing PNG chunks (including
Draw Things XMP) and IDAT bytes; modifies the existing file inode in place.
On Windows, restores all three native file times: Created, Modified, Accessed.
Run --help for options. This is a best-effort workflow, not a reproduction engine.
"""
from __future__ import annotations

import argparse
import csv
import copy
import ctypes
from datetime import datetime
import hashlib
import math
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import textwrap
import xml.etree.ElementTree as ET
import zlib

VERSION = "1.00"
PNG_SIG = b"\x89PNG\r\n\x1a\n"
TEXT_TYPES = (b"tEXt", b"zTXt", b"iTXt")
XMP_KEY = "XML:com.adobe.xmp"
MAX_TEXT = 32 * 1024 * 1024
BUF = 1024 * 1024
BAK_RE = re.compile(r"\.bak\d+\.png$", re.I)
# Short names encode the six-digit filesystem creation microseconds.
# Optional _1/_2/... suffixes distinguish truly identical creation timestamps.
SHORT_NAME_RE = re.compile(
    r"^DrawThings_\d{8}_\d{6}_\d{6}(?:_[1-9]\d*)?$", re.I)
FILETIME_EPOCH = 116444736000000000  # 100 ns ticks between 1601 and 1970

SAMPLE_MAP = {
    "euler": "euler", "euler a": "euler_ancestral", "euler ancestral": "euler_ancestral",
    "euler a substep": "euler_ancestral", "heun": "heun", "plms": None,
    "lms": "lms", "ddim": "ddim", "tcd": "lcm", "lcm": "lcm",
    "dpm++ 2m": "dpmpp_2m", "dpm++ 2s a": "dpmpp_2s_ancestral",
    "dpm++ sde": "dpmpp_sde", "dpm++ 2m sde": "dpmpp_2m_sde",
    "dpm++ 3m sde": "dpmpp_3m_sde", "unipc": "uni_pc", "uni pc": "uni_pc",
    "dpm2": "dpm_2", "dpm2 a": "dpm_2_ancestral",
}


class ConversionError(Exception):
    pass


def json_text(obj):
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def _text_value(tag, payload):
    """Return keyword, content from un/compressed PNG text chunks."""
    key, sep, tail = payload.partition(b"\0")
    if not sep or not 1 <= len(key) <= 79:
        return None, None
    try:
        keyword = key.decode("latin-1")
        if tag == b"tEXt":
            return keyword, tail.decode("latin-1")
        if tag == b"zTXt":
            if len(tail) < 2 or tail[0] != 0:
                return keyword, None
            return keyword, _bounded_inflate(tail[1:]).decode("latin-1")
        if tag == b"iTXt":
            if len(tail) < 2:
                return keyword, None
            flag, method = tail[:2]
            lang, sep, rest = tail[2:].partition(b"\0")
            if not sep:
                return keyword, None
            translated, sep, value = rest.partition(b"\0")
            if not sep or method != 0 or flag not in (0, 1):
                return keyword, None
            if flag:
                value = _bounded_inflate(value)
            return keyword, value.decode("utf-8")
    except (UnicodeError, zlib.error, ConversionError):
        return keyword, None
    return None, None


def _bounded_inflate(data):
    dec = zlib.decompressobj()
    raw = dec.decompress(data, MAX_TEXT + 1)
    if len(raw) > MAX_TEXT or dec.unconsumed_tail:
        raise ConversionError("compressed metadata exceeds size limit")
    raw += dec.flush(MAX_TEXT + 1 - len(raw))
    if len(raw) > MAX_TEXT or not dec.eof:
        raise ConversionError("invalid or oversized compressed metadata")
    return raw


def scan_png(path):
    """Read PNG structure/check CRC without decoding or recompressing pixels."""
    chunks = []
    texts = {}
    xmp_payloads = []
    idat = hashlib.sha256()
    dimensions = None
    seen_idat = False
    seen_iend = False
    with open(path, "rb") as stream:
        if stream.read(8) != PNG_SIG:
            raise ConversionError("invalid PNG signature")
        while True:
            pos = stream.tell()
            header = stream.read(8)
            if not header:
                break
            if len(header) != 8:
                raise ConversionError("truncated PNG chunk header")
            length, kind = struct.unpack(">I4s", header)
            if not all((65 <= c <= 90 or 97 <= c <= 122) for c in kind):
                raise ConversionError("invalid PNG chunk type")
            crc = zlib.crc32(kind)
            captured = bytearray() if kind in TEXT_TYPES and length <= MAX_TEXT else None
            first = bytearray() if kind == b"IHDR" and length == 13 else None
            remaining = length
            while remaining:
                part = stream.read(min(remaining, BUF))
                if not part:
                    raise ConversionError("truncated PNG chunk data")
                crc = zlib.crc32(part, crc)
                if captured is not None:
                    captured.extend(part)
                if first is not None:
                    first.extend(part)
                if kind == b"IDAT":
                    idat.update(part)
                remaining -= len(part)
            expected = stream.read(4)
            if len(expected) != 4:
                raise ConversionError("truncated PNG CRC")
            if struct.unpack(">I", expected)[0] != crc & 0xffffffff:
                raise ConversionError("PNG CRC mismatch: " + kind.decode("ascii"))
            keyword = None
            if captured is not None:
                keyword, val = _text_value(kind, bytes(captured))
                if keyword is not None and val is not None:
                    texts.setdefault(keyword, []).append(val)
                if keyword == XMP_KEY:
                    xmp_payloads.append(val)
            if kind == b"IHDR":
                if chunks or first is None:
                    raise ConversionError("invalid PNG IHDR")
                w, h = struct.unpack(">II", first[:8])
                if not w or not h:
                    raise ConversionError("invalid image dimensions")
                dimensions = (w, h)
            if kind == b"IDAT":
                seen_idat = True
            chunks.append((pos, 12 + length, kind, keyword))
            if kind == b"IEND":
                if length != 0:
                    raise ConversionError("invalid IEND chunk")
                seen_iend = True
                tail_len = len(stream.read())
                if tail_len:
                    raise ConversionError("unexpected data after PNG IEND")
                break
    if not dimensions or not seen_idat or not seen_iend:
        raise ConversionError("missing PNG IHDR, IDAT or IEND")
    return {"chunks": chunks, "texts": texts, "xmp": xmp_payloads,
            "dims": dimensions, "idat_sha256": idat.hexdigest()}


def drawthings_json(scan):
    """Find original Draw Things JSON in XMP EXIF UserComment."""
    json_objects = []
    for item in scan["xmp"]:
        if not item:
            continue
        try:
            root = ET.fromstring(item)
        except ET.ParseError:
            continue
        for node in root.iter():
            if node.tag == "{http://ns.adobe.com/exif/1.0/}UserComment":
                for candidate in ("".join(node.itertext()).strip(),):
                    try:
                        value = json.loads(candidate)
                        if isinstance(value, dict) and (
                                ("model" in value and "sampler" in value) or
                                ("v2" in value and isinstance(value["v2"], dict))):
                            json_objects.append(value)
                    except (json.JSONDecodeError, ValueError):
                        pass
    if not json_objects:
        raise ConversionError("no Draw Things JSON in XMP exif:UserComment")
    return json_objects[0]


def _as_int(value, name, low, high):
    if isinstance(value, bool):
        raise ConversionError("invalid " + name)
    try:
        num = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ConversionError("missing/invalid " + name)
    if num < low or num > high:
        raise ConversionError("out-of-range " + name)
    return num


def _as_float(value, name, low, high):
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ConversionError("missing/invalid " + name)
    if not low <= num <= high:
        raise ConversionError("out-of-range " + name)
    return num


def sampler_mapping(label, warnings):
    if not isinstance(label, str) or not label.strip():
        raise ConversionError("Draw Things sampler is missing")
    lower = " ".join(label.lower().replace("_", " ").split())
    suffix = None
    for ending, schedule in ((" karras", "karras"), (" exponential", "exponential"),
                             (" trailing", "normal"), (" ays", "normal"),
                             (" substep", "normal")):
        if lower.endswith(ending):
            lower = lower[:-len(ending)].strip()
            suffix = ending.strip()
            scheduler = schedule
            break
    else:
        scheduler = "normal"
    if lower == "euler a" and suffix == "substep":
        lower = "euler a"
    core = SAMPLE_MAP.get(lower, "unsupported")
    if core is None or core == "unsupported":
        raise ConversionError("unsupported Draw Things sampler: " + label)
    if suffix in ("trailing", "ays", "substep"):
        warnings.append("Draw Things '%s' schedule has no exact KSampler counterpart; used 'normal'" % suffix)
    if lower == "tcd":
        warnings.append("TCD is approximated by ComfyUI LCM; sampler algorithms may differ")
    return core, scheduler


class Graph:
    def __init__(self):
        self.nodes, self.links, self.api = [], [], {}

    def add(self, kind, title, pos, inputs, outputs, fields, widgets=None,
            *, frontend_only=False):
        nid = len(self.nodes) + 1
        ins = [{"name": name, "type": typ, "link": None} for name, typ in inputs]
        outs = [{"name": name, "type": typ, "links": None, "slot_index": i}
                for i, (name, typ) in enumerate(outputs)]
        n = {"id": nid, "type": kind, "pos": list(pos),
             "size": [350, 110 + max(0, len(fields) - 1) * 30],
             "flags": {}, "order": nid - 1, "mode": 0,
             "inputs": ins, "outputs": outs,
             "properties": {"Node name for S&R": kind},
             "widgets_values": widgets if widgets is not None else list(fields.values())}
        n["title"] = title
        self.nodes.append(n)
        if not frontend_only:
            self.api[str(nid)] = {"inputs": dict(fields), "class_type": kind, "_meta": {"title": title}}
        return nid

    def note(self, title, body, pos):
        """Add a native canvas-only Note with content-fitted dimensions.

        Note text and its intentional blank section separators are unchanged.
        The dimensions are estimates for ComfyUI's default Note widget, not
        coordinates for the original PNG image or executable KSampler graph.
        """
        node_id = self.add("Note", title, pos, [], [], {}, [body], frontend_only=True)
        self.nodes[node_id - 1]["size"] = list(_note_size(title, body))
        return node_id

    def connect(self, src, source_name, dst, dest_name):
        source = self.nodes[src-1]
        target = self.nodes[dst-1]
        outs = [i for i, o in enumerate(source["outputs"]) if o["name"] == source_name]
        ins = [i for i, o in enumerate(target["inputs"]) if o["name"] == dest_name]
        if len(outs) != 1 or len(ins) != 1:
            raise AssertionError("invalid graph port")
        o, i = outs[0], ins[0]
        typ = source["outputs"][o]["type"]
        if target["inputs"][i]["type"] != typ:
            raise AssertionError("mismatched graph socket type")
        if target["inputs"][i]["link"] is not None:
            raise AssertionError("duplicate graph connection")
        link_id = len(self.links) + 1
        self.links.append([link_id, src, o, dst, i, typ])
        source["outputs"][o]["links"] = (source["outputs"][o]["links"] or []) + [link_id]
        target["inputs"][i]["link"] = link_id
        self.api[str(dst)]["inputs"][dest_name] = [str(src), o]

    def finish(self):
        workflow = {"last_node_id": len(self.nodes), "last_link_id": len(self.links),
                    "nodes": self.nodes, "links": self.links, "groups": [],
                    "config": {}, "extra": {"ds": {"scale": 0.75, "offset": [60, 80]}},
                    "version": 0.4}
        validate_graph(workflow, self.api)
        return workflow, self.api


def validate_graph(w, a):
    ns = {n["id"]: n for n in w["nodes"]}
    if len(ns) != len(w["nodes"]) or len(w["links"]) != w["last_link_id"]:
        raise ConversionError("bad graph IDs")
    links = {ln[0]: ln for ln in w["links"]}
    if len(links) != len(w["links"]):
        raise ConversionError("duplicate graph link IDs")
    if set(a) != {str(i) for i in ns if ns[i]["type"] != "Note"}:
        raise ConversionError("API graph node mismatch")
    for node in ns.values():
        if node["type"] == "Note" and (node["inputs"] or node["outputs"]):
            raise ConversionError("informational Note has executable sockets")
    if not a and (not ns or any(node["type"] != "Note" for node in ns.values())):
        raise ConversionError("informational workflow must contain only Note nodes")
    for link in links.values():
        lid, source, outslot, target, inslot, typ = link
        if source not in ns or target not in ns:
            raise ConversionError("broken node link")
        try:
            src = ns[source]["outputs"][outslot]
            dst = ns[target]["inputs"][inslot]
        except IndexError:
            raise ConversionError("broken node port")
        if src["type"] != typ or dst["type"] != typ or lid not in (src["links"] or []) or dst["link"] != lid:
            raise ConversionError("inconsistent graph link")
        if a[str(target)]["inputs"].get(dst["name"]) != [str(source), outslot]:
            raise ConversionError("API link does not match canvas")



def _render(value):
    """Human-readable, unmodified recorded values (never reinterpret field units)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)



def _profile_events(data):
    """Names of completed processing milestones as recorded in Draw Things XMP.

    Profile entries document observed events, not their exact implementation.
    Unrecognized profile structures are not grounds for assuming a second pass.
    """
    profile = data.get("profile")
    timings = profile.get("timings") if isinstance(profile, dict) else None
    if not isinstance(timings, list):
        return set()
    return {entry.get("name") for entry in timings
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)}


def _hires_fix_plan(data, v2, width, height, main_denoise):
    """Plan a conservative standard-image two-pass graph; return (plan, reason).

    Require both metadata configuration and explicit recorded second-pass
    activity. Never infer a second sampler merely from stored/default settings.
    Does not guess a model architecture or treat stage2Steps as Hires Fix steps.
    """
    # The structured v2 record is authoritative when it supplies this flag;
    # a conflicting legacy field must not silently enable a second pass.
    enabled = (v2["hiresFix"] is True) if isinstance(v2.get("hiresFix"), bool) else (data.get("hires_fix") is True)
    if not enabled:
        return None, "Hires Fix not enabled in structured configuration"
    raw_w = v2.get("hiresFixWidth")
    raw_h = v2.get("hiresFixHeight")
    raw_strength = v2.get("hiresFixStrength", data.get("second_stage_strength"))
    try:
        if isinstance(raw_w, bool) or isinstance(raw_h, bool):
            raise ValueError("boolean resolution")
        first_w = int(raw_w)
        first_h = int(raw_h)
        if not (16 <= first_w < width and 16 <= first_h < height):
            return None, "first-pass dimensions are not smaller than the target dimensions"
        if first_w % 8 or first_h % 8 or width % 8 or height % 8:
            return None, "first-pass or target dimensions are not multiples of eight"
        strength = _as_float(raw_strength, "Hires Fix strength", 0, 1)
        if strength <= 0:
            return None, "Hires Fix strength is zero"
    except (ValueError, TypeError, ConversionError, OverflowError):
        return None, "Hires Fix dimensions or strength are missing/invalid"
    if main_denoise != 1.0:
        return None, "source-image denoising is used; original input is not embedded"
    events = _profile_events(data)
    necessary = {"sampling", "image_decoded", "second_pass_image_encoded", "second_pass_sampling"}
    if not necessary.issubset(events):
        return None, "recorded processing profile does not confirm a second sampling pass"
    # A different selected refiner can affect sampling in ways the basic
    # CheckpointLoaderSimple / KSampler graph cannot accurately represent.
    refiner = v2.get("refinerModel") or data.get("refiner")
    model = data.get("model") or v2.get("model")
    if refiner and refiner != model:
        return None, "a distinct refiner model is recorded; refiner behavior not reconstructed"
    if v2.get("controls"):
        return None, "control inputs are recorded and not available in the PNG"
    return {"first_width": first_w, "first_height": first_h,
            "target_width": width, "target_height": height, "strength": strength}, ""


def _meaningful_untranslated(data, v2, png_dims, *, hires_plan=None, ckpt_to_safetensors=False, changed=None):
    """Describe untranslated *recorded* settings without inferring DT internals.

    The configuration is not a record of what necessarily executed. `profile`
    event names are shown separately as recorded evidence, never as causal proof.
    """
    items = []
    if changed is None:
        changed = []

    def display_name(value, category, label):
        converted = _normalize_named_reference(value, category, ckpt_to_safetensors)
        if converted != value:
            changed.append((label, value, converted))
        return converted

    def add(title, pairs):
        items.append((title, [(key, _render(val)) for key, val in pairs]))

    if (v2.get("hiresFix") or data.get("hires_fix") is True) and hires_plan is None:
        add("Hires Fix configuration (not reconstructed)", [
            ("hiresFix", v2.get("hiresFix", data.get("hires_fix"))),
            ("hiresFixWidth", v2.get("hiresFixWidth")),
            ("hiresFixHeight", v2.get("hiresFixHeight")),
            ("hiresFixStrength", v2.get("hiresFixStrength", data.get("second_stage_strength"))),
        ])
    if hires_plan is not None:
        add("Hires Fix sampling differences (best-effort reconstruction)", [
            ("second-pass random noise", "Draw Things RNG state is not recorded; ComfyUI reuses recorded seed"),
            ("sampling equivalence", "ComfyUI's sampler and schedule may differ from Draw Things"),
            ("decode/encode", "vanilla ComfyUI VAE used; exact Draw Things processing may differ"),
        ])
    refiner = v2.get("refinerModel") or data.get("refiner")
    if refiner:
        add("Refiner configuration (not reconstructed)", [
            ("refinerModel", display_name(refiner, "refiner", "refinerModel")), ("refinerStart", v2.get("refinerStart", data.get("refiner_start"))),
            ("stage2Steps (recorded; purpose not assumed)", v2.get("stage2Steps")),
            ("stage2Guidance (recorded; purpose not assumed)", v2.get("stage2Guidance")),
            ("stage2Shift (recorded; purpose not assumed)", v2.get("stage2Shift")),
        ])
    upscaler = v2.get("upscaler") or data.get("upscaler")
    if upscaler:
        add("Upscaling configuration (not reconstructed)", [
            ("upscaler", display_name(upscaler, "upscaler", "upscaler")),
            ("upscalerScaleFactor (raw recorded value)", v2.get("upscalerScaleFactor")),
            ("stored PNG dimensions", "%d x %d" % png_dims),
        ])
    face = v2.get("faceRestoration") or data.get("face_restoration")
    if face:
        add("Face restoration configuration (not reconstructed)",
            [("faceRestoration", display_name(face, "face", "faceRestoration"))])
    if v2.get("tiledDiffusion"):
        add("Tiled diffusion configuration (not reconstructed)", [
            (key, v2.get(key)) for key in ("tiledDiffusion", "diffusionTileWidth",
                                         "diffusionTileHeight", "diffusionTileOverlap")])
    if v2.get("cfgZeroStar") or v2.get("cfgZeroInitSteps"):
        add("CFG Zero Star (not reconstructed)", [
            ("cfgZeroStar", v2.get("cfgZeroStar")),
            ("cfgZeroInitSteps", v2.get("cfgZeroInitSteps")),
        ])
    if v2.get("zeroNegativePrompt") or data.get("zero_negative_prompt"):
        add("Special negative-prompt conditioning (not reconstructed)", [
            ("zeroNegativePrompt", v2.get("zeroNegativePrompt", data.get("zero_negative_prompt"))),
        ])
    if v2.get("colorCalibration") not in (None, "none", "disabled"):
        add("Color calibration (not reconstructed)", [("colorCalibration", v2["colorCalibration"])])
    shift = data.get("shift", v2.get("shift"))
    if shift not in (None, 0, 0.0) or v2.get("resolutionDependentShift"):
        add("Model shift (no generic KSampler equivalent)", [
            ("shift", shift), ("resolutionDependentShift", v2.get("resolutionDependentShift")),
        ])
    if v2.get("guidanceEmbed") is not None or v2.get("speedUpWithGuidanceEmbed"):
        add("Embedded guidance settings (not reconstructed)", [
            ("guidanceEmbed", v2.get("guidanceEmbed")),
            ("speedUpWithGuidanceEmbed", v2.get("speedUpWithGuidanceEmbed")),
        ])
    if data.get("seed_mode") not in (None, ""):
        add("Draw Things seed mode (RNG may differ in ComfyUI)",
            [("seed_mode", data["seed_mode"])])
    if v2.get("controls"):
        add("Control configuration (not reconstructed)",
            [("controls", _normalized_controls_for_notes(v2["controls"], ckpt_to_safetensors, changed))])
    if v2.get("teaCache"):
        add("TeaCache settings (not reconstructed)", [
            (k, v2.get(k)) for k in ("teaCache", "teaCacheStart", "teaCacheEnd",
                                     "teaCacheThreshold", "teaCacheMaxSkipSteps")])
    if v2.get("clipWeight") not in (None, 1, 1.0):
        add("CLIP weight (not reconstructed)", [("clipWeight", v2["clipWeight"])])
    if v2.get("separateClipL") or v2.get("separateOpenClipG") or v2.get("separateT5"):
        add("Separate text encoders (not reconstructed)", [
            (k, v2.get(k)) for k in ("separateClipL", "separateOpenClipG", "separateT5")])
    if v2.get("batchCount") not in (None, 1):
        add("Batch count (not reconstructed)", [("batchCount", v2["batchCount"])])
    if data.get("strength", v2.get("strength", 1)) not in (None, 1, 1.0):
        add("Input image/latent not available for denoising", [
            ("strength", data.get("strength", v2.get("strength"))),
            ("limitation", "Empty latent is a best-effort graph, not the original input"),
        ])
    profile = data.get("profile")
    if isinstance(profile, dict) and isinstance(profile.get("timings"), list):
        events = [event.get("name") for event in profile["timings"]
                  if isinstance(event, dict) and isinstance(event.get("name"), str)]
        extra = [name for name in events if name in {
            "second_pass_image_encoded", "second_pass_sampling", "second_pass_image_decoded",
            "image_encoded", "image_upscaled", "face_restored"}]
        if extra:
            add("Named events found in recorded processing profile (not replayed)",
                [("profile event", name) for name in extra])
    return items


# The following helpers affect only canvas presentation. They never alter the
# Draw Things metadata, the reconstructed processing graph, or API prompt.
NOTE_LINE_LIMIT = 25  # Keep each note readable even with extensive metadata.
NOTE_LINE_WIDTH = 54  # Wrap for compact columns, without omitting information.
NOTE_MIN_WIDTH = 270
NOTE_MAX_WIDTH = 445
NOTE_BODY_CHAR_WIDTH = 7.0  # Conservative monospace text width in canvas units.
NOTE_TITLE_CHAR_WIDTH = 7.7
NOTE_SIDE_PADDING = 38
NOTE_MIN_HEIGHT = 80
NOTE_HEIGHT_BASE = 44  # Title bar and compact text-widget padding.
NOTE_LINE_HEIGHT = 15  # Estimated ComfyUI default Note textarea line pitch.
NOTE_COLUMNS = 3  # Informational-only PARTIAL workflows.
NOTE_X_SPACING = 40  # Gap between content-fitted columns, not absolute pitch.
NOTE_Y_SPACING = 26
NOTE_LEFT_GAP = 95  # Keep notes comfortably clear of checkpoint inputs.
NOTE_COLUMN_GAP = 38
NOTE_TARGET_COLUMN_HEIGHT = 950  # Balance notes independently of the functional graph height.


def _note_size(title, body):
    """Return (width, height) using the visible lines rather than fixed size.

    Title and body have different font metrics. Clamp to predictable limits,
    while _note_wrap makes the body fit the maximum text line width.
    """
    lines = body.splitlines() or [""]
    widest = max(len(line) for line in lines)
    width = max(NOTE_MIN_WIDTH,
                min(NOTE_MAX_WIDTH, round(max(
                    widest * NOTE_BODY_CHAR_WIDTH,
                    len(title) * NOTE_TITLE_CHAR_WIDTH) + NOTE_SIDE_PADDING)))
    height = max(NOTE_MIN_HEIGHT, NOTE_HEIGHT_BASE + len(lines) * NOTE_LINE_HEIGHT)
    return width, height


def _note_wrap(line):
    """Wrap long *display* lines; all source values stay verbatim in PNG XMP."""
    if len(line) <= NOTE_LINE_WIDTH:
        return [line]
    leading = len(line) - len(line.lstrip(" "))
    prefix = " " * max(leading, 3)
    # A long individual metadata value may contain no word breaks (e.g. JSON).
    return textwrap.wrap(line, width=NOTE_LINE_WIDTH,
                         subsequent_indent=prefix,
                         break_long_words=True, break_on_hyphens=False,
                         replace_whitespace=False, drop_whitespace=False) or [line]


def _note_topic(title):
    """Choose a human-readable area of the canvas without reinterpreting data."""
    if title.startswith(("Hires Fix", "Refiner configuration")):
        return "Hires Fix & Refiner"
    if title.startswith(("Upscaling", "Face restoration")):
        return "Upscaling & Restoration"
    if title.startswith(("Tiled diffusion", "Tiled VAE")):
        return "Tiling & Decoding"
    if title.startswith(("LoRA ", "Refiner-only LoRA", "All-stage LoRA", "Unmapped LoRA")):
        return "LoRA Details"
    if title.startswith("Named events found"):
        return "Processing Profile"
    if title.startswith(("CFG Zero Star", "Special negative-prompt", "Model shift",
                         "Embedded guidance", "Draw Things seed mode")):
        return "Sampling & Guidance"
    return "Other Recorded Settings"


def _notes_text(items):
    """Show recorded fields with just one blank line between sections.

    Node titles and the section headings already make the context clear;
    repeating several disclaimer lines in every Note wastes canvas space.
    Original complete metadata remains in the source XMP regardless.
    """
    lines = []
    for title, pairs in items:
        if lines:
            lines.append("")
        lines.extend(_note_wrap(title))
        for key, value in pairs:
            if value != "None":
                lines.extend(_note_wrap("  %s: %s" % (key, value)))
    return "\n".join(lines)


def _make_note_pages(items):
    """Return labeled Note texts, preserving all informational sections.

    Small workflows get a single Note. Larger ones get topical Notes. Very
    long sections (e.g. a serialized control setting) paginate instead of
    producing invisible text below the bottom of a capped Note widget.
    """
    all_text = _notes_text(items)
    if len(all_text.splitlines()) <= NOTE_LINE_LIMIT:
        return [("Additional Draw Things settings", all_text)]

    topics = {}
    for heading, pairs in items:
        topics.setdefault(_note_topic(heading), []).append((heading, pairs))
    pages = []
    intro = []
    for topic, sections in topics.items():
        # Page sections on whole field boundaries where possible. If a single
        # field is exceptionally long, repeat the heading on continued pages.
        bodies = []
        body = list(intro)
        for heading, pairs in sections:
            fields = []
            for key, value in pairs:
                if value != "None":
                    fields.extend(_note_wrap("  %s: %s" % (key, value)))
            # Never place a section heading as the last line on a page.
            remaining = list(fields)
            continuation = False
            if not remaining:
                remaining = ["  (no recorded values)"]
            while remaining:
                header = heading + (" (continued)" if continuation else "")
                heading_lines = _note_wrap(header)
                available = NOTE_LINE_LIMIT - len(body) - len(heading_lines)
                if available < 2:
                    bodies.append("\n".join(body).rstrip())
                    body = list(intro)
                    available = NOTE_LINE_LIMIT - len(body) - len(heading_lines)
                # Reserve a spacer only when it fits; content never truncates.
                taken = remaining[:available]
                remaining = remaining[len(taken):]
                body.extend(heading_lines)
                body.extend(taken)
                if len(body) < NOTE_LINE_LIMIT:
                    body.append("")
                if remaining:
                    bodies.append("\n".join(body).rstrip())
                    body = list(intro)
                    continuation = True
        if len(body) > len(intro):
            bodies.append("\n".join(body).rstrip())
        for i, note_body in enumerate(bodies, 1):
            name = "Draw Things - " + topic
            if len(bodies) > 1:
                name += " (%d/%d)" % (i, len(bodies))
            pages.append((name, note_body))
    return pages


def _add_note_grid(graph, pages, top, single_x=None):
    """Fit a Note-only PARTIAL workflow to content-based column widths."""
    if not pages:
        return
    columns = 1 if len(pages) == 1 else NOTE_COLUMNS
    column_widths = [max((_note_size(title, body)[0]
                          for j, (title, body) in enumerate(pages) if j % columns == col),
                         default=NOTE_MIN_WIDTH) for col in range(columns)]
    xs = [single_x if single_x is not None else 40]
    for col in range(1, columns):
        xs.append(xs[-1] + column_widths[col - 1] + NOTE_X_SPACING)
    for start in range(0, len(pages), columns):
        row = pages[start:start + columns]
        heights = []
        for col, (title, body) in enumerate(row):
            nid = graph.note(title, body, [xs[col], top])
            heights.append(graph.nodes[nid - 1]["size"][1])
        top += max(heights) + NOTE_Y_SPACING


def _add_notes_left_of_workflow(graph, pages):
    """Height-balance narrow Note columns to the left of the functional graph.

    Earlier versions filled a column until an arbitrary limit was reached,
    potentially leaving just one orphan Note in a second column. The number
    of columns is now chosen from the *total Note text height*, independently
    of the functional canvas height (which changes when Hires Fix is present).
    A contiguous partition minimizes column height disparities without
    dropping, reordering, or rewriting any informational text. Rightmost
    column contains the earliest topics, same reading order as before.
    """
    if not pages:
        return
    functional = [n for n in graph.nodes if n["type"] != "Note"]
    left = min(n["pos"][0] for n in functional)
    top = min(n["pos"][1] for n in functional)
    sizes = [_note_size(title, body) for title, body in pages]
    n = len(pages)
    total_height = sum(h for _, h in sizes) + NOTE_Y_SPACING * (n - 1)
    num_columns = min(n, max(1, math.ceil(total_height / NOTE_TARGET_COLUMN_HEIGHT)))

    # DP balances actual displayed Note heights; no arbitrary overflow rule or
    # large fixed width. Each column consists of consecutive Note sections.
    # Computing optimal splits avoids one almost-empty column when 7 topics
    # are close to the height cutoff. Cost is deviation from equal heights.
    from functools import lru_cache
    prefix = [0]
    for width, height in sizes:
        prefix.append(prefix[-1] + height)

    def column_height(start, end):
        return prefix[end] - prefix[start] + NOTE_Y_SPACING * (end - start - 1)

    target = (sum(height for _, height in sizes) + NOTE_Y_SPACING * (n - num_columns)) / num_columns

    @lru_cache(None)
    def fit(start, left_columns):
        if left_columns == 1:
            return (column_height(start, n) - target) ** 2, (n,)
        best = None
        # Leave at least one Note for each remaining column.
        for end in range(start + 1, n - left_columns + 2):
            tail_cost, tail_boundaries = fit(end, left_columns - 1)
            score = (column_height(start, end) - target) ** 2 + tail_cost
            candidate = score, (end,) + tail_boundaries
            if best is None or candidate < best:
                best = candidate
        return best

    boundaries = fit(0, num_columns)[1]
    right_edge = left - NOTE_LEFT_GAP
    start = 0
    for end in boundaries:
        width = max(w for w, _ in sizes[start:end])
        x = right_edge - width
        y = top
        for (title, body), (_, height) in zip(pages[start:end], sizes[start:end]):
            graph.note(title, body, [x, y])
            y += height + NOTE_Y_SPACING
        right_edge = x - NOTE_COLUMN_GAP
        start = end


def _display_normalized_partial(data, enabled):
    """For PARTIAL Note-only workflows, selectively normalize known file keys.

    This is a deep-copied display view. The parsed original and source PNG XMP
    are never changed, and non-filename configuration values stay untouched.
    """
    if not enabled:
        return data, 0
    shown = copy.deepcopy(data)
    changed = []

    def change(d, key, category):
        if isinstance(d, dict) and isinstance(d.get(key), str):
            before = d[key]
            after = _normalize_named_reference(before, category, True)
            if after != before:
                d[key] = after
                changed.append((key, before, after))

    def loras(d, key):
        if not isinstance(d, dict) or not isinstance(d.get(key), list):
            return
        for entry in d[key]:
            if isinstance(entry, dict):
                change(entry, "file", "lora")
                change(entry, "model", "lora")

    for d in (shown, shown.get("v2", {})):
        change(d, "model", "checkpoint")
        change(d, "refinerModel", "refiner")
        change(d, "refiner", "refiner")
        change(d, "upscaler", "upscaler")
        change(d, "faceRestoration", "face")
        change(d, "face_restoration", "face")
        loras(d, "loras")
        loras(d, "lora")
        if isinstance(d, dict) and "controls" in d:
            d["controls"] = _normalized_controls_for_notes(d["controls"], True, changed)
    return shown, len(changed)


def informational_workflow(data, png_dims, reason, *, ckpt_to_safetensors=False):
    """Canvas-only Notes when Draw Things JSON is recognizable but not executable."""
    g = Graph()
    shown, adjusted = _display_normalized_partial(data, ckpt_to_safetensors)
    raw = json.dumps(shown, ensure_ascii=True, indent=2)
    # Display the *full* recorded JSON, but split it across readable panels.
    wrapped = []
    for line in raw.splitlines():
        wrapped.extend(_note_wrap(line))
    max_body_lines = NOTE_LINE_LIMIT - 4
    segments = [wrapped[i:i + max_body_lines]
                for i in range(0, len(wrapped), max_body_lines)] or [[]]
    panels = []
    for n, segment in enumerate(segments, 1):
        title = "Draw Things PARTIAL (information only)"
        if len(segments) > 1:
            title += " (%d/%d)" % (n, len(segments))
        body = ("No executable workflow was reconstructed.\n"
                "Reason: %s\n" % reason +
                ("Filenames normalized for display; original XMP unchanged.\n\n"
                 if adjusted else "Original Draw Things XMP/JSON unchanged.\n\n") +
                "\n".join(segment))
        panels.append((title, body))
    _add_note_grid(g, panels, 80)
    w, api = g.finish()
    warnings = [reason]
    if adjusted:
        warnings.append("ComfyUI filenames: %d named reference(s) normalized in PARTIAL Notes; original XMP unchanged" % adjusted)
    return w, api, warnings

def _comfy_model_filename(recorded_filename, ckpt_to_safetensors=False, *, is_lora=False):
    """Optionally restore ComfyUI model or LoRA filenames from Draw Things names.

    A Draw Things-imported checkpoint named 'model.safetensors' may appear as
    'model_f16.ckpt', and an imported LoRA named 'name.safetensors' may appear
    as 'name_lora_f16.ckpt'. Remove the appropriate *whole suffix*, based on
    whether the reference is a LoRA or a checkpoint, rather than blindly
    replacing the extension. Plain .ckpt names retain the A7 fallback.
    Match only final filename suffixes, case-insensitively, preserving paths.
    Original Draw Things XMP remains unchanged; categorized Notes may be normalized.
    """
    if ckpt_to_safetensors and isinstance(recorded_filename, str):
        low = recorded_filename.lower()
        if is_lora and low.endswith("_lora_f16.ckpt") and len(recorded_filename) > len("_lora_f16.ckpt"):
            return recorded_filename[:-len("_lora_f16.ckpt")] + ".safetensors"
        if low.endswith("_f16.ckpt") and len(recorded_filename) > len("_f16.ckpt"):
            return recorded_filename[:-len("_f16.ckpt")] + ".safetensors"
        if low.endswith(".ckpt"):
            return recorded_filename[:-len(".ckpt")] + ".safetensors"
    return recorded_filename



def _normalize_named_reference(value, category, enabled):
    """Return a *display/output-only* filename under the single A7+ flag.

    Not a reverse-engineering claim about the original import file. Only
    normalize a file whose type and naming convention are sufficiently known.
    Missing/uncertain formats remain untouched, for manual choice in ComfyUI.
    Existing .safetensors/.pth/.pt/.bin values are never changed.
    """
    if not enabled or not isinstance(value, str):
        return value
    low = value.lower()
    if not low.endswith(".ckpt"):
        return value
    if category in ("checkpoint", "refiner", "lora"):
        return _comfy_model_filename(value, True, is_lora=(category == "lora"))

    # The DT import convention _f16.ckpt is not exclusive to safetensors.
    # Recognized ESRGAN-style upscalers and known face restorers commonly use
    # .pth in ComfyUI. Do not pretend that every custom model was a .pth file.
    if category in ("upscaler", "face"):
        stem = value[:-len("_f16.ckpt")] if low.endswith("_f16.ckpt") else None
        if stem is None or not stem:
            return value
        filename = re.split(r"[/\\]", stem)[-1].lower()
        if category == "upscaler":
            known = ("realesrgan", "real_esrgan", "realesrnet", "esrgan", "remacri", "4x_", "2x_", "ultrasharp", "swinir", "bsrgan")
        else:
            known = ("restoreformer", "codeformer", "gfpgan")
        if filename == "remacri_original":
            # A commonly distributed original is remacri_original.pt.
            return stem + ".pt"
        if filename.startswith(known):
            return stem + ".pth"
        return value

    if category == "control":
        # Classic SD1.x ControlNet v1.1 was traditionally distributed as .pth;
        # most newer user-imported conditioning models use .safetensors. The
        # latter is a best-effort convention, not proof of original format.
        basename = re.split(r"[/\\]", value)[-1].lower()
        is_legacy_pth = (basename.startswith(("control_v11", "control_v10")) or
                         basename.startswith("control_v1_"))
        ext = ".pth" if is_legacy_pth else ".safetensors"
        if low.endswith("_f16.ckpt") and len(value) > len("_f16.ckpt"):
            return value[:-len("_f16.ckpt")] + ext
        return value[:-len(".ckpt")] + ext
    return value


def _normalized_controls_for_notes(value, enabled, changed):
    """Only rewrite explicit controls[].file fields, never input-image files.

    Control entries may have arbitrary nested options, mask filenames or image
    settings; those are not model references and must be retained verbatim.
    """
    if not isinstance(value, list):
        return value
    result = []
    for entry in value:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        out = dict(entry)
        original = entry.get("file")
        if isinstance(original, str):
            converted = _normalize_named_reference(original, "control", enabled)
            if converted != original:
                out["file"] = converted
                changed.append(("controls[].file", original, converted))
        result.append(out)
    return result


def build_workflow(data, png_dims, *, ckpt_to_safetensors=False):
    v2 = data.get("v2") if isinstance(data.get("v2"), dict) else {}
    warnings = []
    model = data.get("model") or v2.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConversionError("Draw Things checkpoint/model filename missing")
    if not any(model.lower().endswith(ext) for ext in (".ckpt", ".safetensors", ".pt", ".pth", ".bin")):
        warnings.append("model name does not have a typical ComfyUI checkpoint extension; name retained verbatim")
    prompts = (str(data.get("c", "")), str(data.get("uc", "")))
    if not data.get("c") and "c" not in data:
        raise ConversionError("Draw Things positive prompt missing")
    size = data.get("size")
    wh = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", str(size or ""))
    width = v2.get("width", int(wh.group(1)) if wh else png_dims[0])
    height = v2.get("height", int(wh.group(2)) if wh else png_dims[1])
    width = _as_int(width, "width", 16, 65536)
    height = _as_int(height, "height", 16, 65536)
    if (width, height) != png_dims:
        warnings.append("generation size %dx%d differs from stored PNG %dx%d" % (width, height, *png_dims))
    seed = _as_int(data.get("seed", v2.get("seed")), "seed", 0, 2**64 - 1)
    steps = _as_int(data.get("steps", v2.get("steps")), "steps", 1, 100000)
    cfg = _as_float(data.get("scale", v2.get("guidanceScale")), "guidance scale", 0, 1000)
    denoise = _as_float(data.get("strength", v2.get("strength", 1.0)), "strength", 0.0, 1.0)
    sampler, scheduler = sampler_mapping(data.get("sampler", ""), warnings)
    batch = _as_int(v2.get("batchSize", 1), "batch size", 1, 4096)
    if v2.get("batchCount", 1) not in (None, 1):
        warnings.append("Draw Things batch count is not reconstructed (batch size is preserved)")
    if denoise < 1:
        warnings.append("strength below 1: source image/latent is not embedded; empty-latent workflow is only an approximation")
    if data.get("shift", v2.get("shift")) not in (None, 0, 0.0):
        warnings.append("model-specific shift is not applied by the generic vanilla ComfyUI graph")
    if v2.get("resolutionDependentShift"):
        warnings.append("resolution-dependent model shift is not reconstructed")
    if v2.get("guidanceEmbed") is not None or v2.get("speedUpWithGuidanceEmbed"):
        warnings.append("model-specific embedded guidance is not mapped; KSampler CFG may differ")
    hires_plan, hires_reason = _hires_fix_plan(data, v2, width, height, denoise)
    if hires_plan:
        warnings.append("Hires Fix: reconstructed two-pass decode/bilinear-scale/encode/sampling sequence (best effort)")
        warnings.append("Hires Fix second sampler reuses recorded seed; original second-pass RNG state unavailable")
    elif v2.get("hiresFix") or data.get("hires_fix") is True:
        warnings.append("Draw Things Hires Fix not reconstructed: " + hires_reason)
    if v2.get("tiledDiffusion"):
        warnings.append("tiled diffusion is not reconstructed; original settings remain in XMP")
    if v2.get("controls"):
        warnings.append("ControlNet/control inputs are not reconstructed; source control images are unavailable")
    if v2.get("separateClipL") or v2.get("separateOpenClipG") or v2.get("separateT5"):
        warnings.append("separate text-encoder settings are not reconstructed")
    if v2.get("numFrames", 1) not in (1, 0, None) and (v2.get("causalInference") or v2.get("id") not in (None, 0)):
        warnings.append("video/multiframe architecture not represented by still-image latent workflow")
    if v2.get("cfgZeroStar") or v2.get("cfgZeroInitSteps"):
        warnings.append("CFG-zero-star settings are not reconstructed")
    if v2.get("teaCache"):
        warnings.append("TeaCache acceleration settings are not reconstructed")
    if v2.get("zeroNegativePrompt"):
        warnings.append("special zero-negative-prompt conditioning is not reconstructed")
    if v2.get("clipWeight") not in (None, 1, 1.0):
        warnings.append("Draw Things CLIP weight is not reconstructed")
    if v2.get("colorCalibration") not in (None, "none", "disabled"):
        warnings.append("Draw Things color calibration is not reconstructed")
    if data.get("seed_mode"):
        warnings.append("Draw Things seed mode may use a different RNG from ComfyUI")

    # Optional filename mapping changes generated ComfyUI metadata only.
    # The original Draw Things XMP is always preserved verbatim.
    mapped_references = 0
    output_model = _comfy_model_filename(model, ckpt_to_safetensors)
    mapped_references += (output_model != model)
    graph = Graph()
    load = graph.add("CheckpointLoaderSimple", "Draw Things Checkpoint", [-920, 140],
                     [], [("MODEL", "MODEL"), ("CLIP", "CLIP"), ("VAE", "VAE")],
                     {"ckpt_name": output_model}, [output_model])
    model_out, clip_out = load, load
    loras = v2.get("loras") if isinstance(v2.get("loras"), list) else data.get("lora", [])
    if not isinstance(loras, list):
        loras = []
    extra_lora_notes = []
    for i, entry in enumerate(loras):
        if not isinstance(entry, dict):
            warnings.append("LoRA entry %d is malformed; retained in XMP, not applied" % (i + 1))
            extra_lora_notes.append(("LoRA %d (malformed)" % (i + 1), entry))
            continue
        name = entry.get("file") or entry.get("model")
        if not isinstance(name, str) or not name:
            warnings.append("LoRA %d has no filename; retained in XMP, not applied" % (i + 1))
            extra_lora_notes.append(("LoRA %d (no filename)" % (i + 1), entry))
            continue
        if "weight" not in entry:
            warnings.append("LoRA %s has no recorded weight; not inventing one" % name)
            extra_lora_notes.append(("LoRA without weight: " + name, entry))
            continue
        try:
            strength = _as_float(entry["weight"], "LoRA weight", -1000, 1000)
        except ConversionError:
            warnings.append("LoRA %s has an invalid recorded weight; omitted from graph" % name)
            extra_lora_notes.append(("LoRA invalid weight: " + name, entry))
            continue
        # Draw Things modes describe *generation stages*, NOT model-vs-CLIP.
        # The converter does not construct a refiner stage; 'refiner' entries must
        # never be connected to the base checkpoint. Weights are passed through.
        mode = entry.get("mode")
        if mode not in ("all", "base", "refiner"):
            warnings.append("LoRA %s has unrecognized/missing stage mode %r; not applied" % (name, mode))
            extra_lora_notes.append(("Unmapped LoRA stage mode: " + name, entry))
            continue
        if mode == "refiner":
            warnings.append("refiner-only LoRA %s retained in XMP; refiner graph not reconstructed" % name)
            extra_lora_notes.append(("Refiner-only LoRA: " + name, entry))
            continue
        output_lora = _comfy_model_filename(name, ckpt_to_safetensors, is_lora=True)
        mapped_references += (output_lora != name)
        fields = {"lora_name": output_lora, "strength_model": strength, "strength_clip": strength}
        node = graph.add("LoraLoader", "Draw Things LoRA %d (%s)" % (i + 1, mode),
                         [0, 0], [("model", "MODEL"), ("clip", "CLIP")],
                         [("MODEL", "MODEL"), ("CLIP", "CLIP")], fields)
        graph.connect(model_out, "MODEL", node, "model")
        graph.connect(clip_out, "CLIP", node, "clip")
        model_out, clip_out = node, node
        if mode == "all" and v2.get("refinerModel"):
            extra_lora_notes.append(("All-stage LoRA; refiner stage not reconstructed: " + name, entry))

    skip = _as_int(v2.get("clipSkip", 1), "CLIP skip", 1, 100)
    if skip > 1:
        node = graph.add("CLIPSetLastLayer", "Draw Things CLIP Skip", [-240, 50],
                         [("clip", "CLIP")], [("CLIP", "CLIP")],
                         {"stop_at_clip_layer": -skip}, [-skip])
        graph.connect(clip_out, "CLIP", node, "clip")
        clip_out = node
        warnings.append("CLIP skip mapping is model-dependent")

    pos_node = graph.add("CLIPTextEncode", "Draw Things Positive", [-120, -270],
                         [("clip", "CLIP")], [("CONDITIONING", "CONDITIONING")],
                         {"text": prompts[0]}, [prompts[0]])
    neg_node = graph.add("CLIPTextEncode", "Draw Things Negative", [-120, -30],
                         [("clip", "CLIP")], [("CONDITIONING", "CONDITIONING")],
                         {"text": prompts[1]}, [prompts[1]])
    graph.connect(clip_out, "CLIP", pos_node, "clip")
    graph.connect(clip_out, "CLIP", neg_node, "clip")
    latent_width = hires_plan["first_width"] if hires_plan else width
    latent_height = hires_plan["first_height"] if hires_plan else height
    latent = graph.add("EmptyLatentImage", "Draw Things Latent", [-160, 250], [],
                       [("LATENT", "LATENT")],
                       {"width": latent_width, "height": latent_height, "batch_size": batch})
    sample_fields = {"seed": seed, "steps": steps, "cfg": cfg,
                     "sampler_name": sampler, "scheduler": scheduler, "denoise": denoise}
    sampler_node = graph.add("KSampler", "Draw Things First Pass" if hires_plan else "Draw Things Sampling", [280, 50],
                             [("model", "MODEL"), ("positive", "CONDITIONING"),
                              ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                             [("LATENT", "LATENT")], sample_fields,
                             [seed, "fixed", steps, cfg, sampler, scheduler, denoise])
    graph.connect(model_out, "MODEL", sampler_node, "model")
    graph.connect(pos_node, "CONDITIONING", sampler_node, "positive")
    graph.connect(neg_node, "CONDITIONING", sampler_node, "negative")
    graph.connect(latent, "LATENT", sampler_node, "latent_image")

    tiled = bool(data.get("tiled_decoding", v2.get("tiledDecoding", False)))
    if tiled:
        tw = _as_int(data.get("decoding_tile_width", v2.get("decodingTileWidth", 512)), "decoding tile width", 64, 4096)
        th = _as_int(data.get("decoding_tile_height", v2.get("decodingTileHeight", tw)), "decoding tile height", 64, 4096)
        overlap = _as_int(data.get("decoding_tile_overlap", v2.get("decodingTileOverlap", 64)), "decoding tile overlap", 0, 4096)
        if tw != th:
            warnings.append("rectangular tiled decoding approximated by square ComfyUI tiles")
        tile_size = max(tw, th)
        if tile_size % 32:
            rounded = max(64, min(4096, round(tile_size / 32) * 32))
            warnings.append("VAE tile size rounded to ComfyUI's 32-pixel step")
            tile_size = rounded
        if overlap % 32:
            overlap = max(0, min(4096, round(overlap / 32) * 32))
            warnings.append("VAE tile overlap rounded to ComfyUI's 32-pixel step")
        decode_kind = "VAEDecodeTiled"
        decode_fields = {"tile_size": tile_size, "overlap": overlap,
                         "temporal_size": 64, "temporal_overlap": 8}
        decode_widgets = [tile_size, overlap, 64, 8]
    else:
        decode_kind = "VAEDecode"
        decode_fields, decode_widgets = {}, []
    if hires_plan:
        # The published ordinary Draw Things pipeline decodes the first result,
        # resizes it with bilinear interpolation, re-encodes it, then samples.
        # Do not substitute a third-party upscaler or invent stage2Steps.
        first_decode = graph.add("VAEDecode", "Draw Things First Pass Decode", [690, 110],
                                 [("samples", "LATENT"), ("vae", "VAE")],
                                 [("IMAGE", "IMAGE")], {}, [])
        graph.connect(sampler_node, "LATENT", first_decode, "samples")
        graph.connect(load, "VAE", first_decode, "vae")
        scale_fields = {"upscale_method": "bilinear", "width": width,
                        "height": height, "crop": "center"}
        scaled = graph.add("ImageScale", "Draw Things Hires Fix Resize", [1050, 110],
                           [("image", "IMAGE")], [("IMAGE", "IMAGE")],
                           scale_fields, ["bilinear", width, height, "center"])
        graph.connect(first_decode, "IMAGE", scaled, "image")
        encoded = graph.add("VAEEncode", "Draw Things Hires Fix Encode", [1440, 110],
                            [("pixels", "IMAGE"), ("vae", "VAE")],
                            [("LATENT", "LATENT")], {}, [])
        graph.connect(scaled, "IMAGE", encoded, "pixels")
        graph.connect(load, "VAE", encoded, "vae")
        second_fields = {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": hires_plan["strength"]}
        second = graph.add("KSampler", "Draw Things Hires Fix Second Pass", [1830, 110],
                           [("model", "MODEL"), ("positive", "CONDITIONING"),
                            ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                           [("LATENT", "LATENT")], second_fields,
                           [seed, "fixed", steps, cfg, sampler, scheduler, hires_plan["strength"]])
        graph.connect(model_out, "MODEL", second, "model")
        graph.connect(pos_node, "CONDITIONING", second, "positive")
        graph.connect(neg_node, "CONDITIONING", second, "negative")
        graph.connect(encoded, "LATENT", second, "latent_image")
        decode = graph.add(decode_kind, "Draw Things Final Decode", [2260, 110],
                           [("samples", "LATENT"), ("vae", "VAE")],
                           [("IMAGE", "IMAGE")], decode_fields, decode_widgets)
        graph.connect(second, "LATENT", decode, "samples")
        graph.connect(load, "VAE", decode, "vae")
    else:
        decode = graph.add(decode_kind, "Draw Things Decode", [690, 110],
                           [("samples", "LATENT"), ("vae", "VAE")],
                           [("IMAGE", "IMAGE")], decode_fields, decode_widgets)
        graph.connect(sampler_node, "LATENT", decode, "samples")
        graph.connect(load, "VAE", decode, "vae")
    save = graph.add("SaveImage", "Save Image", [1030, 110],
                     [("images", "IMAGE")], [], {"filename_prefix": "DrawThings_ComfyUI"})
    graph.connect(decode, "IMAGE", save, "images")

    # Position native ComfyUI nodes in two clean sampling stages when Hires Fix
    # is documented; otherwise keep A5 canvas positions exactly unchanged.
    lora_nodes = [node for node in graph.nodes if node["type"] == "LoraLoader"]
    has_clip_skip = any(node["type"] == "CLIPSetLastLayer" for node in graph.nodes)
    prompts_x = 1080 if lora_nodes or has_clip_skip else 560
    if has_clip_skip:
        prompts_x += 390
    x_by_type = {
        "CheckpointLoaderSimple": (40, 170),
        "CLIPTextEncode": (prompts_x, -60),
        "EmptyLatentImage": (prompts_x, 590),
        "KSampler": (prompts_x + 560, 200),
        "VAEDecode": (prompts_x + 1070, 200),
        "VAEDecodeTiled": (prompts_x + 1070, 200),
        "SaveImage": (prompts_x + 1560, 200),
        "CLIPSetLastLayer": (prompts_x - 450, 150),
    }
    if hires_plan:
        # Keep the initial decoding/resize/encode path on the upper row,
        # with second-pass sampling, final decoding, and saving directly
        # below the main graph rather than far to the lower right.
        x_by_type.update({
            "VAEDecode": (prompts_x + 1025, 200),
            "VAEDecodeTiled": (prompts_x + 1025, 200),
            "ImageScale": (prompts_x + 1475, 200),
            "VAEEncode": (prompts_x + 1930, 200),
            "SaveImage": (prompts_x + 1490, 850),
        })
    prompt_index = 0
    for node in graph.nodes:
        kind = node["type"]
        if kind == "LoraLoader":
            slot = lora_nodes.index(node)
            node["pos"] = [530, 20 + slot * 300]
            node["size"] = [380, 240]
        elif kind in x_by_type:
            x, y = x_by_type[kind]
            if kind == "CLIPTextEncode":
                y += prompt_index * 320
                prompt_index += 1
            if hires_plan:
                if kind == "KSampler" and node["title"].endswith("Second Pass"):
                    x, y = prompts_x + 560, 820
                elif kind in ("VAEDecode", "VAEDecodeTiled") and node["title"].endswith("Final Decode"):
                    x, y = prompts_x + 1035, 850
            node["pos"] = [x, y]
            node["size"] = {
                "CheckpointLoaderSimple": [380, 170],
                "CLIPTextEncode": [435, 270],
                "EmptyLatentImage": [360, 205],
                "KSampler": [375, 355],
                "VAEDecode": [350, 180],
                "VAEDecodeTiled": [360, 225],
                "ImageScale": [355, 220],
                "VAEEncode": [355, 180],
                "SaveImage": [365, 180],
                "CLIPSetLastLayer": [365, 180],
            }[kind]

    note_filename_changes = []
    untranslated = _meaningful_untranslated(
        data, v2, png_dims, hires_plan=hires_plan,
        ckpt_to_safetensors=ckpt_to_safetensors, changed=note_filename_changes)
    for label, original_entry in extra_lora_notes:
        # Even a refiner-only, malformed or otherwise non-executable LoRA may
        # have an identifiable .ckpt filename. Normalize its NOTE display but
        # never apply it to the wrong generation stage or edit the source XMP.
        display_entry = original_entry
        note_label = label
        if ckpt_to_safetensors and isinstance(original_entry, dict):
            display_entry = dict(original_entry)
            for key in ("file", "model"):
                recorded = original_entry.get(key)
                if isinstance(recorded, str):
                    display = _normalize_named_reference(recorded, "lora", True)
                    if display != recorded:
                        note_filename_changes.append(("LoRA " + key, recorded, display))
                        display_entry[key] = display
                        note_label = note_label.replace(recorded, display)
        field_label = "LoRA entry (normalized filenames)" if display_entry is not original_entry else "original LoRA entry"
        untranslated.append((note_label, [(field_label, _render(display_entry))]))
    mapped_references += len(note_filename_changes)
    if tiled and (tw != th or v2.get("decodingTileOverlap") != overlap):
        untranslated.append(("Tiled VAE decoding approximation", [
            ("recorded decodingTileWidth", _render(tw)),
            ("recorded decodingTileHeight", _render(th)),
            ("recorded decodingTileOverlap", _render(data.get("decoding_tile_overlap", v2.get("decodingTileOverlap")))),
            ("ComfyUI square tile size", _render(tile_size)),
            ("ComfyUI overlap", _render(overlap)),
        ]))
    if untranslated:
        pages = _make_note_pages(untranslated)
        _add_notes_left_of_workflow(graph, pages)
        warnings.append("%d untranslated setting group(s) documented in %d ComfyUI Note(s)" %
                        (len(untranslated), len(pages)))
    if mapped_references:
        warnings.append("ComfyUI filenames: %d named .ckpt reference(s) normalized in functional nodes and/or informational Notes; output formats depend on category (.safetensors/.pth); original XMP unchanged" % mapped_references)
    w, a = graph.finish()
    return w, a, list(dict.fromkeys(warnings))


def write_chunk(dest, kind, key, content):
    payload = key.encode("latin-1") + b"\0" + content.encode("utf-8")
    if len(payload) > 2**31:
        raise ConversionError("generated metadata is too large")
    dest.write(struct.pack(">I", len(payload)))
    dest.write(kind)
    dest.write(payload)
    dest.write(struct.pack(">I", zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff))


def _copy_n(src, dst, total):
    while total:
        part = src.read(min(total, BUF))
        if not part:
            raise ConversionError("source PNG changed during conversion")
        dst.write(part)
        total -= len(part)


def build_staged_png(path, destination, scan, workflow, api, overwrite):
    # Insert before IDAT so PIL/ComfyUI finds PNG text metadata during initial open.
    inserted = False
    with open(path, "rb") as source, open(destination, "wb") as staged:
        staged.write(source.read(8))
        for position, full_len, kind, key in scan["chunks"]:
            if kind == b"IDAT" and not inserted:
                write_chunk(staged, b"tEXt", "workflow", json_text(workflow))
                write_chunk(staged, b"tEXt", "prompt", json_text(api))
                inserted = True
            if overwrite and key in ("workflow", "prompt"):
                continue
            source.seek(position)
            _copy_n(source, staged, full_len)
        staged.flush()
        os.fsync(staged.fileno())
    if not inserted:
        raise ConversionError("no PNG IDAT chunk")


# Native Windows FILETIME handling is essential: os.utime alone may not preserve
# all three Explorer dates, especially creation time.
if os.name == "nt":
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create = k32.CreateFileW
    _create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _create.restype = wintypes.HANDLE
    _get = k32.GetFileTime
    _get.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
    _get.restype = wintypes.BOOL
    _set = k32.SetFileTime
    _set.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
    _set.restype = wintypes.BOOL
    _close = k32.CloseHandle
    _close.argtypes = [wintypes.HANDLE]
    _close.restype = wintypes.BOOL

    def _native_times(path, times=None):
        handle = _create(str(path), 0x180 if times is not None else 0x80,
                         7, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed for timestamps", str(path))
        try:
            if times is None:
                buf = [FILETIME() for _ in range(3)]
                if not _get(handle, *(ctypes.byref(x) for x in buf)):
                    raise OSError(ctypes.get_last_error(), "GetFileTime failed", str(path))
                return tuple((x.dwHighDateTime << 32) | x.dwLowDateTime for x in buf)
            buf = [FILETIME(t & 0xffffffff, t >> 32) for t in times]
            if not _set(handle, *(ctypes.byref(x) for x in buf)):
                raise OSError(ctypes.get_last_error(), "SetFileTime failed", str(path))
        finally:
            _close(handle)


def snapshot_times(path):
    if os.name == "nt":
        return _native_times(path)
    s = os.stat(path)
    return s.st_atime_ns, s.st_mtime_ns


def restore_times(path, times):
    if os.name == "nt":
        _native_times(path, times)
    else:
        os.utime(path, ns=times)


def assert_times(path, times):
    got = snapshot_times(path)
    if got != times:
        raise ConversionError("original filesystem timestamps changed: before %r, after %r" % (times, got))


def temp_name(path, purpose):
    fd, temp = tempfile.mkstemp(prefix=".dt-" + purpose + "-",
                                 suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    return Path(temp)


def copy_file(src, dst):
    with open(src, "rb") as sf, open(dst, "wb") as df:
        shutil.copyfileobj(sf, df, BUF)
        df.flush()
        os.fsync(df.fileno())


def safe_unlink(path):
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def identity(path):
    s = path.stat()
    return (s.st_dev, s.st_ino)


def unique_backup(path):
    for i in range(1, 1000000):
        cand = path.with_name(path.stem + ".bak" + str(i) + path.suffix)
        try:
            with cand.open("xb"):
                pass
            return cand
        except FileExistsError:
            continue
    raise ConversionError("too many existing backups")


def _datetime_from_unix_ns(epoch_ns):
    """Convert integer epoch nanoseconds to local time without float rounding."""
    seconds, nanos = divmod(int(epoch_ns), 1_000_000_000)
    try:
        return datetime.fromtimestamp(seconds).replace(microsecond=nanos // 1000)
    except (ValueError, OverflowError, OSError) as exc:
        raise ConversionError("invalid filesystem creation timestamp") from exc


def _linux_birth_ns(path):
    """Read Linux birth time using statx, if available; never substitute mtime."""
    if not sys.platform.startswith("linux"):
        return None
    class StatxTimestamp(ctypes.Structure):
        _fields_ = [("sec", ctypes.c_int64), ("nsec", ctypes.c_uint32),
                    ("reserved", ctypes.c_int32)]
    class Statx(ctypes.Structure):
        _fields_ = [
            ("mask", ctypes.c_uint32), ("blksize", ctypes.c_uint32),
            ("attributes", ctypes.c_uint64),
            ("nlink", ctypes.c_uint32), ("uid", ctypes.c_uint32),
            ("gid", ctypes.c_uint32), ("mode", ctypes.c_uint16),
            ("spare0", ctypes.c_uint16), ("ino", ctypes.c_uint64),
            ("size", ctypes.c_uint64), ("blocks", ctypes.c_uint64),
            ("attributes_mask", ctypes.c_uint64),
            ("atime", StatxTimestamp), ("btime", StatxTimestamp),
            ("ctime", StatxTimestamp), ("mtime", StatxTimestamp),
            ("rdev_major", ctypes.c_uint32), ("rdev_minor", ctypes.c_uint32),
            ("dev_major", ctypes.c_uint32), ("dev_minor", ctypes.c_uint32),
            ("mnt_id", ctypes.c_uint64), ("dio_mem_align", ctypes.c_uint32),
            ("dio_offset_align", ctypes.c_uint32),
            ("spare3", ctypes.c_uint64 * 12),
        ]
    try:
        fn = ctypes.CDLL(None, use_errno=True).statx
    except AttributeError:
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                   ctypes.c_uint, ctypes.POINTER(Statx)]
    fn.restype = ctypes.c_int
    out = Statx()
    # AT_FDCWD=-100, AT_SYMLINK_NOFOLLOW=0x100, STATX_BTIME=0x800.
    raw_path = os.fsencode(path)
    if fn(-100, raw_path, 0x100, 0x800, ctypes.byref(out)) != 0:
        return None
    if not out.mask & 0x800:
        return None
    return out.btime.sec * 1_000_000_000 + out.btime.nsec


def original_creation_datetime(path, saved_times):
    """Return the filesystem creation/birth date with microsecond precision.

    Windows uses the original native FILETIME snapshot (100-ns units).
    macOS uses st_birthtime_ns or st_birthtime from the source file.
    Linux uses statx birth time, if exposed by the filesystem.
    Date Modified and POSIX change time are NEVER used as substitutes.
    """
    if os.name == "nt":
        ticks = saved_times[0] - FILETIME_EPOCH
        if ticks <= 0:
            raise ConversionError("Date Created is unavailable or invalid")
        # FILETIME ticks = 100 nanoseconds, preserving all microsecond digits.
        return _datetime_from_unix_ns(ticks * 100), "creation"
    st = path.stat()
    birth_ns = getattr(st, "st_birthtime_ns", None)
    if birth_ns is None:
        birth = getattr(st, "st_birthtime", None)
        if birth is not None:
            birth_ns = int(round(birth * 1_000_000_000))
    if birth_ns is None:
        birth_ns = _linux_birth_ns(path)
    if birth_ns is None or birth_ns <= 0:
        raise ConversionError("Date Created is unavailable on this filesystem; refusing to use Date Modified")
    return _datetime_from_unix_ns(birth_ns), "creation"


def short_filename_plan(path, saved_times, reserved_targets=None):
    """Plan DrawThings_YYYYMMDD_HHMMSS_ffffff[_N].png with no overwrites.

    The original filename's sequence/seed and image fingerprint are ignored.
    Already-shortened v1.00 filenames are never renumbered on later runs.
    Earlier A11/A12 filename schemes are converted to this scheme.
    """
    if SHORT_NAME_RE.fullmatch(path.stem):
        return path, "already named"
    when, _ = original_creation_datetime(path, saved_times)
    base = "DrawThings_" + when.strftime("%Y%m%d_%H%M%S_%f")
    reserved_targets = reserved_targets if reserved_targets is not None else set()
    for index in range(1_000_000):
        name = base + ("_%d" % index if index else "") + ".png"
        dest = path.with_name(name)
        if str(dest).casefold() in reserved_targets:
            continue
        if dest == path or not dest.exists():
            return dest, "creation"
    raise ConversionError("too many short filename collisions")


def rename_no_replace(source, destination):
    """Preserve identity and never silently overwrite another file.

    Windows os.rename refuses existing destinations. POSIX os.rename replaces
    destinations, so use exclusive hard-link creation followed by unlink.
    """
    if source == destination:
        return
    if os.name == "nt":
        os.rename(source, destination)  # Windows rejects occupied destinations.
    else:
        os.link(source, destination)    # Atomic, exclusive destination creation.
        os.unlink(source)


def existing_graph_classification(scan):
    try:
        for raw in scan["texts"].get("prompt", []):
            return "FULL" if json.loads(raw) else "PARTIAL"
    except (ValueError, TypeError):
        pass
    return "FULL"  # Existing graph is present, precise label not recorded.


def convert(path, options, reserved_targets=None):
    if path.is_symlink():
        return "skip", "symlink skipped", []
    if path.suffix.lower() != ".png":
        return "skip", "not a PNG", []
    before = snapshot_times(path)
    try:
        inode = identity(path)
        scan = scan_png(path)
        has_comfy = any(key in ("workflow", "prompt") for _, _, _, key in scan["chunks"])
        if has_comfy and not options.overwrite and not options.short_filenames:
            restore_times(path, before)
            return "skip", "already has ComfyUI metadata (use --overwrite)", []
        try:
            info = drawthings_json(scan)
        except ConversionError as exc:
            if str(exc).startswith("no Draw Things JSON"):
                restore_times(path, before)
                return "skip", "NONE; no Draw Things generation metadata", []
            raise
        short_target = path
        warnings = []
        if options.short_filenames:
            short_target, time_source = short_filename_plan(path, before, reserved_targets)
        rename_only = bool(has_comfy and not options.overwrite)
        if rename_only and short_target == path:
            restore_times(path, before)
            return "skip", "already converted; short filename unchanged", warnings
        if rename_only:
            classification = existing_graph_classification(scan)
        else:
            try:
                wf, api, graph_warnings = build_workflow(
                    info, scan["dims"], ckpt_to_safetensors=options.ckpt_to_safetensors)
            except ConversionError as exc:
                wf, api, graph_warnings = informational_workflow(
                    info, scan["dims"], str(exc), ckpt_to_safetensors=options.ckpt_to_safetensors)
            warnings.extend(graph_warnings)
            classification = "FULL" if api else "PARTIAL"
        action = "rename only" if rename_only else "convert"
        if options.dry_run:
            restore_times(path, before)
            if options.short_filenames and reserved_targets is not None:
                reserved_targets.add(str(short_target).casefold())
            suffix = "; would rename to " + short_target.name if short_target != path else ""
            return "dry-run", classification + "; would " + action + suffix, warnings
    except Exception:
        # Original access times are retained even on unsupported/invalid input.
        restore_times(path, before)
        raise
    stage = None
    rollback = None
    persistent = None
    wrote = False
    renamed = False
    current = path
    try:
        if not rename_only:
            stage = temp_name(path, "stage")
            build_staged_png(path, stage, scan, wf, api, options.overwrite)
            test = scan_png(stage)
            if test["idat_sha256"] != scan["idat_sha256"] or test["xmp"] != scan["xmp"]:
                raise ConversionError("staged PNG changed image bytes or Draw Things XMP")
            for key in ("workflow", "prompt"):
                if len(test["texts"].get(key, [])) != 1:
                    raise ConversionError("staged PNG missing unique " + key)
            if json.loads(test["texts"]["workflow"][0]) != wf or json.loads(test["texts"]["prompt"][0]) != api:
                raise ConversionError("staged ComfyUI metadata differs from generated graph")
        if options.backup:
            # With shortening, make backups using the SHORT basename. That
            # avoids Windows filename-length errors on long original names.
            persistent = unique_backup(short_target if short_target != path else path)
            copy_file(path, persistent)
            shutil.copystat(path, persistent)
            rollback = persistent
        elif not rename_only:
            rollback = temp_name(path, "rollback")
            copy_file(path, rollback)
        if not rename_only:
            # Retain original inode and native Windows creation timestamp.
            with open(path, "r+b") as output, open(stage, "rb") as source:
                wrote = True
                output.seek(0)
                shutil.copyfileobj(source, output, BUF)
                output.truncate()
                output.flush()
                os.fsync(output.fileno())
            if options.verify:
                after = scan_png(path)
                if (after["idat_sha256"] != scan["idat_sha256"] or
                        after["xmp"] != scan["xmp"] or
                        json.loads(after["texts"]["workflow"][0]) != wf or
                        json.loads(after["texts"]["prompt"][0]) != api):
                    raise ConversionError("on-disk post-write verification failed")
        if short_target != path:
            rename_no_replace(path, short_target)
            current = short_target
            renamed = True
        if options.verify and rename_only:
            after = scan_png(current)
            if (after["idat_sha256"] != scan["idat_sha256"] or
                    after["xmp"] != scan["xmp"] or
                    after["texts"] != scan["texts"]):
                raise ConversionError("rename-only verification failed")
        restore_times(current, before)
        assert_times(current, before)
        if identity(current) != inode:
            raise ConversionError("original file identity changed")
        if options.short_filenames and reserved_targets is not None:
            reserved_targets.add(str(short_target).casefold())
        msg = classification + ("; renamed" if rename_only else "; converted")
        if renamed:
            msg += "; renamed to " + short_target.name
        return "renamed" if rename_only else "converted", msg, warnings
    except Exception as exc:
        recovery = ""
        if renamed:
            try:
                rename_no_replace(current, path)
                current = path
            except OSError as err:
                recovery += "; RENAME ROLLBACK FAILED: " + str(err)
        if wrote and rollback and Path(rollback).exists():
            try:
                with open(current, "r+b") as output, open(rollback, "rb") as source:
                    shutil.copyfileobj(source, output, BUF)
                    output.truncate()
                    output.flush()
                    os.fsync(output.fileno())
            except OSError as err:
                recovery += "; ROLLBACK FAILED: " + str(err)
        try:
            restore_times(current, before)
        except OSError as err:
            recovery += "; TIMESTAMP RESTORE FAILED: " + str(err)
        if persistent is not None and not wrote and not renamed:
            safe_unlink(persistent)
        return "error", str(exc) + recovery, []
    finally:
        safe_unlink(stage)
        if rollback and rollback != persistent:
            safe_unlink(rollback)


def eligible_paths(target, recursive):
    if target.is_symlink():
        raise ConversionError("symlink input is not processed")
    if target.is_file():
        if target.suffix.lower() != ".png":
            raise ConversionError("input file must be .png")
        return [target]
    if not target.is_dir():
        raise ConversionError("path not found: " + str(target))
    iterator = target.rglob("*") if recursive else target.iterdir()
    paths = [p for p in iterator if p.is_file() and not p.is_symlink() and
             p.suffix.lower() == ".png" and not BAK_RE.search(p.name)]
    return sorted(paths, key=lambda p: str(p).lower())


def print_progress(done, total):
    if not total:
        return
    width = 24
    blocks = int(width * done / total)
    bar = "#" * blocks + "-" * (width - blocks)
    print("\r[%s] %d/%d" % (bar, done, total), end="\n" if done == total else "", flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="Draw Things -> ComfyUI PNG metadata converter, version 1.00 (retains XMP, pixels and file dates).")
    p.add_argument("path", nargs="?", default=".", help="PNG file or directory; default: current directory")
    p.add_argument("--recursive", "-r", action="store_true", help="also search subdirectories")
    p.add_argument("--backup", action="store_true", help="keep numbered .bak1.png, .bak2.png copies")
    p.add_argument("--dry-run", action="store_true", help="analyze and report; never modify files")
    p.add_argument("--overwrite", action="store_true", help="replace existing ComfyUI workflow/prompt; leave Draw Things XMP unchanged")
    p.add_argument("--ckpt-to-safetensors", action="store_true",
                   help="Normalize generated ComfyUI references and Notes by category: checkpoints/LoRAs -> .safetensors, recognized upscalers/face restorers -> .pth, conditioning models -> .safetensors/.pth; preserve original XMP")
    p.add_argument("--short-filenames", action="store_true",
                   help="optionally rename PNGs to DrawThings_YYYYMMDD_HHMMSS_ffffff.png using Date Created only; collisions get _1, _2, etc.; existing A11/A12 names are updated without --overwrite")
    p.add_argument("--verify", action="store_true", help="verify written PNG and metadata against originals")
    p.add_argument("--report", metavar="CSV", help="write CSV report with per-file status and warnings")
    p.add_argument("--verbose", "-v", action="store_true", help="list every processed/skipped PNG with warnings")
    p.add_argument("--quiet", action="store_true", help="suppress ordinary console output")
    p.add_argument("--no-progress", action="store_true", help="disable progress bar")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    args = p.parse_args(argv)
    if args.quiet and args.verbose:
        p.error("--quiet and --verbose are mutually exclusive")
    try:
        target = Path(args.path).expanduser().absolute()
        paths = eligible_paths(target, args.recursive)
    except (ConversionError, OSError) as e:
        p.error(str(e))
    rows = []
    counts = {"converted": 0, "renamed": 0, "dry-run": 0, "skip": 0, "error": 0}
    # Reserve planned destinations so --dry-run shows the same automatic
    # collision suffixes as a real batch run, without touching any files.
    reserved_targets = set()
    for i, file in enumerate(paths, 1):
        try:
            status, message, warnings = convert(file, args, reserved_targets)
        except Exception as exc:
            status, message, warnings = "error", str(exc), []
        counts[status] += 1
        classification = "NONE" if message.startswith("NONE;") else (
            "FULL" if message.startswith("FULL;") else (
            "PARTIAL" if message.startswith("PARTIAL;") else ""))
        rows.append((str(file), status, classification, message, " | ".join(warnings)))
        if not args.quiet and (args.verbose or status == "error"):
            print("\n[%s] %s: %s" % (status.upper(), file.name, message))
            if args.verbose:
                for warn in warnings:
                    print("  NOTE: " + warn)
        if not args.quiet and not args.no_progress and not args.verbose:
            print_progress(i, len(paths))
    if args.report:
        try:
            report = Path(args.report).expanduser()
            with report.open("w", encoding="utf-8-sig", newline="") as f:
                out = csv.writer(f)
                out.writerow(["file", "status", "classification", "message", "warnings"])
                out.writerows(rows)
        except OSError as e:
            print("Cannot write CSV report: " + str(e), file=sys.stderr)
            return 2
    if not args.quiet:
        print("Total %d PNGs: %d converted, %d renamed-only, %d dry-run, %d skipped, %d errors" %
              (len(paths), counts["converted"], counts["renamed"], counts["dry-run"], counts["skip"], counts["error"]))
        classified = {"FULL": 0, "PARTIAL": 0, "NONE": 0}
        for _path, _status, label, _message, _warnings in rows:
            if label in classified:
                classified[label] += 1
        print("Metadata: %d FULL, %d PARTIAL, %d NONE" %
              (classified["FULL"], classified["PARTIAL"], classified["NONE"]))
        if not paths:
            print("No eligible PNGs found (non-PNG files and backups are ignored).")
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
