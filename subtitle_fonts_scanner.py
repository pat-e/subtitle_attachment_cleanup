#!/usr/bin/env python3
"""
subtitle_fonts_scanner.py
─────────────────────────
Read-only scanner that inspects a single MKV file and reports:
  • Fonts required by ASS/SSA subtitle tracks
  • Fonts currently embedded as attachments
  • Fonts that are missing (required but not embedded)

Usage:
  python subtitle_fonts_scanner.py input.mkv
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: 'fonttools' is required to accurately read internal font names.")
    print("  Install with:  pip install fonttools")
    print("  (Arch Linux):  sudo pacman -S python-fonttools")
    sys.exit(1)

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_ass_font_names(ass_path: Path) -> set[str]:
    """
    Parse an ASS/SSA file and return a lower-case set of all font family names
    referenced in [V4+ Styles] and via \\fn inline overrides in [Events].
    """
    fonts: set[str] = set()
    try:
        with open(ass_path, "r", encoding="utf-8", errors="ignore") as fh:
            in_styles = False
            in_events = False
            fontname_idx = -1

            for raw in fh:
                line = raw.strip()
                if not line:
                    continue

                if line.startswith("["):
                    in_styles = line == "[V4+ Styles]"
                    in_events = line == "[Events]"
                    continue

                if in_styles:
                    if line.startswith("Format:"):
                        fmt_cols = [c.strip().lower() for c in line[len("Format:"):].split(",")]
                        fontname_idx = fmt_cols.index("fontname") if "fontname" in fmt_cols else -1
                    elif line.startswith("Style:") and fontname_idx != -1:
                        cols = [c.strip() for c in line[len("Style:"):].split(",")]
                        if len(cols) > fontname_idx:
                            fonts.add(cols[fontname_idx])

                if in_events and line.startswith("Dialogue:"):
                    for match in re.findall(r"\\fn([^\\}]+)", line):
                        fonts.add(match.strip())

    except Exception as exc:
        print(yellow(f"  Warning: could not fully parse {ass_path.name}: {exc}"))

    return {f.lower() for f in fonts if f}


def get_internal_font_names(font_path: Path) -> set[str]:
    """
    Extract the internal family / full-name / typographic family strings
    from a TTF, OTF, or TTC file using fontTools.
    Returns a lower-case set.
    """
    names: set[str] = set()
    try:
        font = TTFont(str(font_path), fontNumber=0)
        for record in font["name"].names:
            if record.nameID in (1, 4, 16):
                try:
                    names.add(record.toUnicode().lower())
                except Exception:
                    pass
        font.close()
    except Exception as exc:
        print(yellow(f"  Warning: could not read font metadata for {font_path.name}: {exc}"))
    return names


def safe_stem(name: str) -> str:
    """Strip unsafe characters for use as a temp filename component."""
    return "".join(c for c in name if c.isalnum() or c in " ._-").rstrip()


# ── Core scan logic ───────────────────────────────────────────────────────────

def scan_mkv(mkv_path: Path) -> None:
    print()
    print(f"Scanning: {mkv_path.name}")
    print("─" * 70)

    # ── 1. Read MKV metadata ─────────────────────────────────────────────────
    result = subprocess.run(
        ["mkvmerge", "-J", str(mkv_path)],
        capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        print(f"ERROR: mkvmerge could not read '{mkv_path.name}'.")
        print(f"  {result.stderr.strip()}")
        return

    try:
        mkv_info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: failed to parse mkvmerge JSON output: {exc}")
        return

    tracks      = mkv_info.get("tracks", [])
    attachments = mkv_info.get("attachments", [])

    # ── 2. Find ASS/SSA subtitle tracks ─────────────────────────────────────
    ass_tracks = [
        t for t in tracks
        if t.get("type") == "subtitles"
        and (
            "S_TEXT/ASS" in str(t.get("properties", {}).get("codec_id", ""))
            or "S_TEXT/SSA" in str(t.get("properties", {}).get("codec_id", ""))
            or "SubStationAlpha" in str(t.get("codec", ""))
        )
    ]

    # ── 3. Find font attachments ─────────────────────────────────────────────
    font_mimes = ["font", "truetype", "opentype", "sfnt", "application/x-truetype-font"]
    font_attachments = [
        a for a in attachments
        if any(m in a.get("content_type", "").lower() for m in font_mimes)
    ]

    # ── 4. Report basic counts ───────────────────────────────────────────────
    print(f"  ASS/SSA subtitle tracks : {len(ass_tracks)}")
    print(f"  Font attachments        : {len(font_attachments)}")

    if not ass_tracks:
        print("\n  WARNING: No ASS/SSA subtitle tracks found — no font requirements to check.")
        if not font_attachments:
            print("           No font attachments either. Nothing to report.")
        else:
            print(f"           {len(font_attachments)} font attachment(s) present but no subtitles reference them.")
            _list_embedded_fonts_only(font_attachments)
        return

    # ── 5. Extract ASS tracks to a temp directory ────────────────────────────
    required_fonts: set[str] = set()

    with tempfile.TemporaryDirectory(prefix="fonts_scanner_") as tmp:
        tmp_path = Path(tmp)

        # Extract subtitle tracks
        extract_sub_cmd = ["mkvextract", "tracks", str(mkv_path)]
        ass_temp_files: list[Path] = []
        for t in ass_tracks:
            tid = t["id"]
            out = tmp_path / f"track_{tid}.ass"
            extract_sub_cmd.append(f"{tid}:{out}")
            ass_temp_files.append(out)

        sub_result = subprocess.run(extract_sub_cmd, capture_output=True, text=True)
        if sub_result.returncode != 0:
            print(f"ERROR extracting subtitle tracks: {sub_result.stderr.strip()}")
            return

        # Collect required font names from each ASS file
        print(f"\n  ASS tracks parsed:")
        for ass_file in ass_temp_files:
            if not ass_file.exists():
                continue
            found = get_ass_font_names(ass_file)
            tid_str = ass_file.stem.replace("track_", "")
            track_info = next(
                (t for t in ass_tracks if str(t["id"]) == tid_str), {}
            )
            lang = track_info.get("properties", {}).get("language", "und")
            name = track_info.get("properties", {}).get("track_name", "")
            label = f"Track {tid_str}"
            if name:
                label += f" – {name}"
            label += f" [{lang}]"
            print(f"    {label}: {len(found)} font(s) referenced")
            required_fonts.update(found)

        # ── 6. Extract font attachments ──────────────────────────────────────
        embedded_font_names: dict[str, set[str]] = {}   # filename → internal names (lower)

        if font_attachments:
            extract_att_cmd = ["mkvextract", "attachments", str(mkv_path)]
            for att in font_attachments:
                aid  = att["id"]
                stem = safe_stem(att["file_name"])
                out  = tmp_path / f"att_{aid}_{stem}"
                extract_att_cmd.append(f"{aid}:{out}")
                att["_temp_path"] = out

            att_result = subprocess.run(extract_att_cmd, capture_output=True, text=True)
            if att_result.returncode != 0:
                print(f"ERROR extracting font attachments: {att_result.stderr.strip()}")
            else:
                for att in font_attachments:
                    temp_p: Path = att["_temp_path"]
                    if temp_p.exists():
                        internal = get_internal_font_names(temp_p)
                        embedded_font_names[att["file_name"]] = internal

        # ── 7. Match required → embedded ─────────────────────────────────────
        #
        # A font is "covered" if any embedded font file reports an internal name
        # that matches a required name (case-insensitive), OR if the attachment
        # filename stem matches the required name.
        #
        covered_required: set[str] = set()
        attachment_match: dict[str, list[str]] = {}  # file_name → matched font names

        for filename, internal_names in embedded_font_names.items():
            stem_lower = Path(filename).stem.lower()
            matched = set()
            for req in required_fonts:
                if req in internal_names or req == stem_lower:
                    matched.add(req)
            if matched:
                attachment_match[filename] = sorted(matched)
                covered_required.update(matched)

        missing_fonts  = required_fonts - covered_required
        extra_embedded = set(embedded_font_names.keys()) - set(attachment_match.keys())

        # ── 8. Print report ──────────────────────────────────────────────────
        _print_report(required_fonts, embedded_font_names, attachment_match,
                      missing_fonts, extra_embedded)


def _list_embedded_fonts_only(font_attachments: list) -> None:
    """Called when there are no ASS tracks — just list what's embedded."""
    print(f"\n  Embedded font attachments:")
    for att in font_attachments:
        print(f"    • {att['file_name']}  ({att.get('content_type', '?')})")


def _print_report(
    required: set[str],
    embedded: dict[str, set[str]],
    matched: dict[str, list[str]],
    missing: set[str],
    extra: set[str],
) -> None:
    sep = "─" * 70

    # ── Needed fonts ─────────────────────────────────────────────────────────
    print(f"\n  FONTS NEEDED BY SUBTITLES  ({len(required)} total)")
    print(f"  {sep}")
    if required:
        for name in sorted(required):
            status = "[OK]" if name not in missing else "[MISSING]"
            print(f"    {status}  {name}")
    else:
        print(f"    (none)")

    # ── Embedded fonts ────────────────────────────────────────────────────────
    print(f"\n  FONTS EMBEDDED IN MKV  ({len(embedded)} file(s))")
    print(f"  {sep}")
    if embedded:
        for filename, internal_names in sorted(embedded.items()):
            is_used = filename in matched
            tag  = "[USED]  " if is_used else "[EXTRA] "
            hits = matched.get(filename, [])
            line = f"    {tag}{filename}"
            if hits:
                line += f"  →  covers: {', '.join(hits)}"
            print(line)
            # Show internal name(s) for transparency
            for iname in sorted(internal_names)[:6]:
                print(f"          internal name: {iname}")
            if len(internal_names) > 6:
                print(f"          … and {len(internal_names) - 6} more")
    else:
        print(f"    (none)")

    # ── Missing fonts ─────────────────────────────────────────────────────────
    print(f"\n  MISSING FONTS  ({len(missing)} font(s) not embedded)")
    print(f"  {sep}")
    if missing:
        for name in sorted(missing):
            print(f"    ✘  {name}")
    else:
        print(f"    ✓ All required fonts are present — nothing missing!")

    # ── Extra (unused) embedded fonts ─────────────────────────────────────────
    if extra:
        print(f"\n  EXTRA / UNUSED EMBEDDINGS  ({len(extra)} file(s) not needed by any subtitle)")
        print(f"  {sep}")
        for filename in sorted(extra):
            print(f"    ⚠  {filename}")

    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print()
        print("subtitle_fonts_scanner.py")
        print("─" * 40)
        print("  Scans an MKV file and reports which fonts are needed by")
        print("  ASS/SSA subtitles, which are already embedded, and which")
        print("  are missing.")
        print()
        print("Usage:")
        print("  python subtitle_fonts_scanner.py <input.mkv>")
        print()
        print("Example:")
        print("  python subtitle_fonts_scanner.py \"My Show S01E01.mkv\"")
        print()
        sys.exit(1)

    mkv_path = Path(sys.argv[1])

    if not mkv_path.exists():
        print(f"\nError: File not found: {mkv_path}")
        sys.exit(1)

    if mkv_path.suffix.lower() != ".mkv":
        print(f"\nWarning: '{mkv_path.name}' does not have an .mkv extension.")
        print("  This script is designed for MKV files. Proceeding anyway…")

    scan_mkv(mkv_path)


if __name__ == "__main__":
    main()
