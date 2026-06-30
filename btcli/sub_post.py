"""Output reassembly: write translated subtitle files (ASS or SRT).

Responsibilities:
  - RTL wrapping (RLI + PDI per line)
  - Output path resolution (suffix from target language)
  - ASS output with configurable font style
  - Font embedding (subsetted Arabic font in ASS)
  - SRT output
  - File conflict handling (overwrite or rename)
"""
from __future__ import annotations

import io
from pathlib import Path

import pysubs2

from .config import cfg
from .logger import log

# ── Constants ─────────────────────────────────────────────────────────────────
RLI = "\u2067"  # Right-to-Left Isolate
PDI = "\u2069"  # Pop Directional Isolate

# Available fonts for embedding (name → typical filename patterns)
AVAILABLE_FONTS = [
    "Amiri",
    "IBM Plex Sans Arabic",
    "Noto Sans Arabic",
    "Cairo",
    "Tajawal",
    "Almarai",
]


# ── RTL Wrapping ──────────────────────────────────────────────────────────────

def wrap_rtl(text: str) -> str:
    """Wrap each line with RLI+PDI bidi marks, using \\N as ASS line break."""
    lines = text.split("\n")
    return r"\N".join(RLI + line + PDI for line in lines)


# ── Output Path Resolution ────────────────────────────────────────────────────

def resolve_output_path(base_path, suffix: str = ".ar", force_srt: bool = False) -> Path:
    """Build output path from source path + target suffix.

    Args:
        base_path: original source subtitle file path
        suffix: target language suffix (e.g. ".ar", ".fr")
        force_srt: if True, always output .srt regardless of source format

    Returns:
        Path object for output file
    """
    fpath = Path(str(base_path))
    stem = fpath.stem

    # Strip known source language suffixes from stem
    source_suffixes = [".en", ".eng", ".en.hi", ".english", ".ja", ".jp", ".jpn"]
    for s in source_suffixes:
        if stem.lower().endswith(s):
            stem = stem[:-len(s)]
            break

    # Determine extension
    source_ext = fpath.suffix.lower()
    if force_srt:
        ext = suffix + ".srt"
    elif source_ext in (".ass", ".ssa"):
        ext = suffix + ".ass"
    else:
        ext = suffix + ".srt"

    out_path = fpath.with_name(stem + ext)

    # Handle file conflicts
    if cfg.get("FILE_CONFLICT", "overwrite") == "rename":
        counter = 1
        base_stem = stem
        while out_path.exists():
            out_path = fpath.with_name(f"{base_stem}{suffix}_{counter}" + out_path.suffix)
            counter += 1

    return out_path


# ── ASS Output ────────────────────────────────────────────────────────────────

def _build_ass_style() -> pysubs2.SSAStyle:
    """Build ASS style from config."""
    style = pysubs2.SSAStyle()
    style.fontname = cfg.get("FONT_NAME", "Amiri")
    style.fontsize = cfg.get("FONT_SIZE", 40)
    style.encoding = 1  # Arabic encoding
    style.alignment = cfg.get("FONT_ALIGNMENT", 2)
    style.outline = cfg.get("FONT_OUTLINE", 1)
    style.shadow = cfg.get("FONT_SHADOW", 0)
    style.bold = False
    style.italic = False
    style.marginl = cfg.get("FONT_MARGIN_L", 20)
    style.marginr = cfg.get("FONT_MARGIN_R", 20)
    style.marginv = cfg.get("FONT_MARGIN_V", 30)
    return style


def build_ass_output(blocks: list) -> str:
    """Build ASS file content from translated blocks.

    Args:
        blocks: list of {start, end, text, block_idx}

    Returns:
        ASS file content as string
    """
    subs = pysubs2.SSAFile()
    subs.styles["Default"] = _build_ass_style()

    for block in blocks:
        event = pysubs2.SSAEvent()
        event.start = block["start"]
        event.end = block["end"]
        event.text = block["text"]
        event.style = "Default"
        subs.append(event)

    return subs.to_string("ass")


def embed_font_in_ass(ass_content: str, font_path: str | None = None) -> str:
    """Embed a subsetted font into ASS file content.

    If font_path is provided, reads and embeds that font file.
    Otherwise attempts to find the configured font on the system.

    Args:
        ass_content: existing ASS file content
        font_path: path to .ttf/.otf font file to embed

    Returns:
        ASS content with embedded font section
    """
    if not font_path:
        # Check bundled font in install dir first
        bundled = Path(__file__).resolve().parent.parent / "fonts" / "NotoSansArabic-subset.ttf"
        if bundled.exists():
            font_path = str(bundled)
        else:
            # Try common system font paths
            font_name = cfg.get("FONT_NAME", "Noto Sans Arabic")
            search_paths = [
                Path("/opt/bulk-translate-cli/fonts"),
                Path.home() / ".fonts",
                Path.home() / ".local" / "share" / "fonts",
                Path("/usr/share/fonts"),
                Path("/usr/local/share/fonts"),
            ]
            for search_dir in search_paths:
                if not search_dir.exists():
                    continue
                for fp in search_dir.rglob("*"):
                    if font_name.lower().replace(" ", "") in fp.stem.lower().replace(" ", ""):
                        if fp.suffix.lower() in (".ttf", ".otf"):
                            font_path = str(fp)
                            break
                if font_path:
                    break

    if not font_path or not Path(font_path).exists():
        log.detail("    Warning: Font file not found for embedding, skipping")
        return ass_content

    # Subset the font to Arabic characters only
    try:
        font_data = _subset_font(font_path)
    except Exception as e:
        log.detail(f"    Warning: Font subsetting failed ({e}), embedding full font")
        font_data = Path(font_path).read_bytes()

    # Encode using ASS UUEncode format
    encoded_lines = _ass_uuencode(font_data)

    font_name = Path(font_path).stem
    font_section = (
        "\n[Fonts]\n"
        f"fontname: {font_name}.ttf\n"
        + "\n".join(encoded_lines)
        + "\n"
    )

    # Insert at end of file
    if ass_content.endswith("\n"):
        ass_content += font_section
    else:
        ass_content += "\n" + font_section

    return ass_content


def _ass_uuencode(data: bytes) -> list:
    """Encode binary data using ASS/SSA UUEncode format.

    ASS uses a custom encoding: each group of 3 bytes becomes 4 characters.
    Each byte is split into 6-bit values, then 33 (0x21, '!') is added to each.
    Lines are 80 characters max.
    """
    result = []
    line = ""

    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]

        # Pad to 3 bytes if needed
        if len(chunk) == 1:
            b0, b1, b2 = chunk[0], 0, 0
        elif len(chunk) == 2:
            b0, b1, b2 = chunk[0], chunk[1], 0
        else:
            b0, b1, b2 = chunk[0], chunk[1], chunk[2]

        # Split 3 bytes (24 bits) into 4 x 6-bit values
        c0 = b0 >> 2
        c1 = ((b0 & 0x03) << 4) | (b1 >> 4)
        c2 = ((b1 & 0x0F) << 2) | (b2 >> 6)
        c3 = b2 & 0x3F

        # Add 33 to each and convert to char
        line += chr(c0 + 33) + chr(c1 + 33) + chr(c2 + 33) + chr(c3 + 33)

        if len(line) >= 80:
            result.append(line[:80])
            line = line[80:]

    if line:
        result.append(line)

    return result


def _subset_font(font_path: str) -> bytes:
    """Subset a font to Arabic Unicode range + basic Latin.

    Uses fonttools if available, otherwise returns full font.
    """
    try:
        from fontTools.subset import Subsetter
        from fontTools.ttLib import TTFont

        font = TTFont(font_path)
        subsetter = Subsetter()

        # Arabic Unicode ranges + basic Latin for mixed content
        unicodes = set()
        # Basic Latin (0020-007F)
        unicodes.update(range(0x0020, 0x0080))
        # Arabic (0600-06FF)
        unicodes.update(range(0x0600, 0x0700))
        # Arabic Supplement (0750-077F)
        unicodes.update(range(0x0750, 0x0780))
        # Arabic Extended-A (08A0-08FF)
        unicodes.update(range(0x08A0, 0x0900))
        # Arabic Presentation Forms-A (FB50-FDFF)
        unicodes.update(range(0xFB50, 0xFE00))
        # Arabic Presentation Forms-B (FE70-FEFF)
        unicodes.update(range(0xFE70, 0xFF00))

        subsetter.populate(unicodes=unicodes)
        subsetter.subset(font)

        buf = io.BytesIO()
        font.save(buf)
        return buf.getvalue()

    except ImportError:
        # fonttools not installed, return full font
        return Path(font_path).read_bytes()


# ── SRT Output ────────────────────────────────────────────────────────────────

def build_srt_output(blocks: list) -> str:
    """Build SRT file content from translated blocks.

    Args:
        blocks: list of {start, end, text, block_idx}

    Returns:
        SRT file content as string
    """
    srt_lines = []
    for i, block in enumerate(blocks, start=1):
        s = pysubs2.time.ms_to_str(block["start"], fractions=True).replace(".", ",")
        e = pysubs2.time.ms_to_str(block["end"], fractions=True).replace(".", ",")
        srt_lines.append(f"{i}\n{s} --> {e}\n{block['text']}\n")
    return "\n".join(srt_lines)


# ── Main Reassembly ───────────────────────────────────────────────────────────

def reassemble_files(translated_blob: dict, meta: dict, files: list,
                     suffix: str = ".ar", force_srt: bool = False):
    """Write translated output files.

    Args:
        translated_blob: {tag: translated_text} for all cues
        meta: full meta dict from build_blob
        files: list of source file Paths
        suffix: output filename suffix
        force_srt: if True, always output SRT

    Returns:
        (completed_names, warnings_list)
    """
    # Group cues by file
    file_cues = {i + 1: [] for i in range(len(files))}
    for tag, m in meta.items():
        file_cues[m["file_idx"]].append((tag, m))

    completed, warnings = [], []
    embed_font = cfg.get("EMBED_FONT", False)

    for file_idx, cues in file_cues.items():
        fpath = Path(str(files[file_idx - 1]))
        if not cues:
            log.detail(f"  No cues for {fpath.name} - skipping")
            warnings.append(f"{fpath.name}: no cues found")
            continue

        cues.sort(key=lambda x: x[1]["block_idx"])
        untranslated = []
        blocks = []

        for tag, m in cues:
            translated_text = translated_blob.get(tag)
            pos_tags = m.get("pos_tags", "")

            if translated_text is not None:
                text = pos_tags + wrap_rtl(translated_text) if pos_tags else wrap_rtl(translated_text)
            else:
                text = pos_tags + wrap_rtl(m["text"]) if pos_tags else wrap_rtl(m["text"])
                untranslated.append((tag, m["text"]))

            blocks.append({
                "start": m["start"],
                "end": m["end"],
                "text": text,
                "block_idx": m["block_idx"],
            })

        # Resolve output path
        out_path = resolve_output_path(fpath, suffix=suffix, force_srt=force_srt)
        is_ass = out_path.suffix.lower() == ".ass"

        # Write output
        if is_ass and not force_srt:
            content = build_ass_output(blocks)
            if embed_font:
                content = embed_font_in_ass(content)
            out_path.write_text(content, encoding="utf-8")
        else:
            content = build_srt_output(blocks)
            out_path.write_text(content, encoding="utf-8")

        # Report
        if untranslated:
            log.info(f"  {out_path.name}: {len(blocks) - len(untranslated)}/{len(blocks)} translated, "
                     f"{len(untranslated)} kept as original")
            warnings.append(f"{out_path.name}: {len(untranslated)} lines untranslated")
        else:
            log.info(f"  {out_path.name}: {len(blocks)} cues (fully translated)")

        completed.append(out_path.name)

    return completed, warnings
