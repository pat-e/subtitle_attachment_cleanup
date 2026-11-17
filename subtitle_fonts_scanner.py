#!/usr/bin/env python3
"""Report the fonts one MKV's subtitles need and the fonts it embeds.

Matching follows subtitle_fonts_cleaner.py: every face in a .ttf, .otf, or
.ttc, variable-font named instances, and names compared without case, spaces,
hyphens, or underscores. This script does not modify the file.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    from fontTools.ttLib import TTCollection, TTFont
except ImportError:
    print("Error: 'fonttools' is required to accurately read internal font names.")
    print("  Install with:  pip install fonttools")
    print("  (Arch Linux):  sudo pacman -S python-fonttools")
    sys.exit(1)


_NAME_IDS = (1, 2, 4, 6, 16, 17)
_FONT_MIME_TOKENS = ("font", "truetype", "opentype", "sfnt")
_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc", ".tte", ".dfont", ".woff", ".woff2"}
_FN_TAG = re.compile(r"\\fn([^\\}]+)")
_STYLE_SECTIONS = {"[v4+ styles]", "[v4 styles]"}
_DISPLAY_NAME_IDS = (1, 4, 16)
_TAG_WIDTH = len("[DUPLICATE]")


def normalize_font_key(name: str) -> str:
    """Compare font names ignoring case, spaces, hyphens, and underscores.

    A leading @ is the ASS marker for vertical text and is not part of the
    family name. Only one leading marker is removed.
    """
    text = name.strip().replace("\ufeff", "").replace("\x00", "")
    if text.startswith("@") or text.startswith("\uff20"):
        text = text[1:].strip()
    return "".join(
        ch for ch in text.casefold() if not ch.isspace() and ch not in "-_"
    )


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="ignore")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="ignore")
    return raw.decode("utf-8", errors="ignore")


def read_ass_font_names(ass_path: Path) -> set[str]:
    """Fonts requested by Style lines and inline \\fn tags, in original spelling.

    Names are taken from every [V4 Styles] / [V4+ Styles] line and from every
    \\fn override in [Events]. The caller strips a leading @ when matching.
    """
    fonts: set[str] = set()
    in_styles = False
    in_events = False
    fontname_idx = -1

    for raw_line in read_text(ass_path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            header = line.lower()
            in_styles = header in _STYLE_SECTIONS
            in_events = header == "[events]"
            fontname_idx = -1
            continue

        if in_styles:
            lowered = line.lower()
            if lowered.startswith("format:"):
                columns = [part.strip().lower() for part in line.split(":", 1)[1].split(",")]
                fontname_idx = columns.index("fontname") if "fontname" in columns else -1
            elif lowered.startswith("style:") and fontname_idx != -1:
                columns = [part.strip() for part in line.split(":", 1)[1].split(",")]
                if len(columns) > fontname_idx and columns[fontname_idx]:
                    fonts.add(columns[fontname_idx])
        elif in_events:
            for match in _FN_TAG.findall(line):
                name = match.strip()
                if name:
                    fonts.add(name)
    return fonts


def _clean_name(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.replace("\x00", "").replace("\ufeff", "").strip()
    if not cleaned or len(cleaned) > 512:
        return None
    if "\n" in cleaned or "\r" in cleaned:
        return None
    return cleaned


def _name_strings(font, name_id: int) -> list[str]:
    if not isinstance(name_id, int) or name_id <= 0 or name_id == 0xFFFF:
        return []
    if "name" not in font:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for record in font["name"].names:
        if record.nameID != name_id:
            continue
        try:
            text = _clean_name(record.toUnicode())
        except Exception:
            continue
        if text and text not in seen:
            seen.add(text)
            found.append(text)
    return found


def _combine(names: set[str], families: list[str], subfamilies: list[str]) -> None:
    for family in families:
        for subfamily in subfamilies:
            if family.casefold() == subfamily.casefold():
                continue
            names.add(f"{family} {subfamily}")


def face_match_names(font) -> set[str]:
    """Names one face can be requested by.

    That is the family, typographic family, subfamily, full name, and
    PostScript name in every language, plus family + subfamily and each
    variable-font named instance (on its own and after its family).
    """
    by_id = {name_id: _name_strings(font, name_id) for name_id in _NAME_IDS}
    names: set[str] = set()
    for values in by_id.values():
        names.update(values)

    legacy_family = by_id[1]
    legacy_sub = by_id[2]
    typo_family = by_id[16]
    typo_sub = by_id[17]
    _combine(names, legacy_family, legacy_sub)
    _combine(names, typo_family, typo_sub)
    _combine(names, legacy_family, typo_sub)
    _combine(names, typo_family, legacy_sub)

    families: list[str] = []
    for family in legacy_family + typo_family:
        if family not in families:
            families.append(family)

    if "fvar" in font:
        for instance in font["fvar"].instances:
            styles = _name_strings(font, instance.subfamilyNameID)
            for style in styles:
                names.add(style)
                for family in families:
                    names.add(f"{family} {style}")
            ps_id = getattr(instance, "postscriptNameID", 0xFFFF)
            for ps_name in _name_strings(font, ps_id):
                names.add(ps_name)
    return {name for name in names if name.strip()}


def _remember(names: list[str], seen: set[str], text: str) -> None:
    if text not in seen:
        seen.add(text)
        names.append(text)


def face_display_names(font) -> list[str]:
    """Family, full, and typographic names, plus variable-font instance styles."""
    found: list[str] = []
    seen: set[str] = set()
    for name_id in _DISPLAY_NAME_IDS:
        for text in _name_strings(font, name_id):
            _remember(found, seen, text)
    if "fvar" in font:
        for instance in font["fvar"].instances:
            for style in _name_strings(font, instance.subfamilyNameID):
                _remember(found, seen, style)
    return found


def _open_faces(path: Path):
    """Return (closable, faces) for a single font or a collection.

    .ttc/.otc files start with 'ttcf' and hold more than one face. Reading
    only face 0 drops later faces that the subtitles actually use.
    """
    with path.open("rb") as handle:
        header = handle.read(4)
    if header == b"ttcf":
        collection = TTCollection(str(path), lazy=True)
        return collection, list(collection.fonts)
    font = TTFont(str(path), lazy=True)
    return font, [font]


def short_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    message = text[0] if text else exc.__class__.__name__
    if len(message) > 200:
        return message[:197] + "..."
    return message


def collect_font_details(path: Path) -> tuple[set[str], list[str], str | None]:
    """Read every face. Raise if the file yields no names at all."""
    opener, faces = _open_faces(path)
    try:
        if not faces:
            raise RuntimeError("no faces in font file")
        names: set[str] = set()
        display: list[str] = []
        seen_display: set[str] = set()
        failures: list[str] = []
        for index, face in enumerate(faces):
            try:
                names.update(face_match_names(face))
                for text in face_display_names(face):
                    _remember(display, seen_display, text)
            except Exception as exc:
                failures.append(f"face {index}: {short_error(exc)}")
        if not names:
            detail = "; ".join(failures) if failures else "no readable font names"
            raise RuntimeError(detail)
        partial = "; ".join(failures) if failures else None
        return names, display, partial
    finally:
        close = getattr(opener, "close", None)
        if close is not None:
            close()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_ass_track(track: dict) -> bool:
    if track.get("type") != "subtitles":
        return False
    props = track.get("properties") or {}
    blob = f"{props.get('codec_id') or ''} {track.get('codec') or ''}"
    return "S_TEXT/ASS" in blob or "S_TEXT/SSA" in blob or "SubStationAlpha" in blob


def is_font_attachment(attachment: dict) -> bool:
    mime = str(attachment.get("content_type") or "").lower()
    if any(token in mime for token in _FONT_MIME_TOKENS):
        return True
    suffix = Path(str(attachment.get("file_name") or "")).suffix.lower()
    return suffix in _FONT_EXTENSIONS


def safe_filename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " .-_" else "_" for ch in name).strip()
    return cleaned or "attachment"


def _unique_requested(requested: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in requested:
        text = raw.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    ordered.sort(key=lambda item: (normalize_font_key(item), item.casefold(), item))
    return ordered


def _match_labels(keys: set[str], requested_by_key: dict[str, list[str]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for key in sorted(keys & requested_by_key.keys()):
        for label in requested_by_key[key]:
            if label not in seen:
                seen.add(label)
                labels.append(label)
    return labels


def _display_font_name(name: str) -> str:
    """Drop one leading ASS vertical-text marker for display."""
    text = name.strip()
    if text.startswith("@") or text.startswith("\uff20"):
        return text[1:].strip()
    return text


def _cover_text(labels: list[str]) -> str:
    shown: list[str] = []
    seen: set[str] = set()
    for label in labels:
        display = _display_font_name(label)
        if display and display not in seen:
            seen.add(display)
            shown.append(display)
    return ", ".join(shown)


def label_is_covered(label: str, covered: set[str]) -> bool:
    key = normalize_font_key(label)
    return bool(key) and key in covered


@dataclass
class ScannedFont:
    file_name: str
    order: int
    names: set[str] = field(default_factory=set)
    display_names: list[str] = field(default_factory=list)
    content_hash: str | None = None
    problem: str | None = None
    partial_error: str | None = None
    tag: str = "EXTRA"
    covers: list[str] = field(default_factory=list)
    note: str = ""


def read_embedded_font(path: Path, file_name: str, order: int) -> ScannedFont:
    """Hash the attachment and read every face. A read failure is not 'unused'."""
    info = ScannedFont(file_name=file_name, order=order)
    try:
        info.content_hash = file_sha256(path)
    except OSError as exc:
        info.problem = short_error(exc)
        return info
    try:
        names, display, partial = collect_font_details(path)
    except Exception as exc:
        info.problem = short_error(exc)
        return info
    info.names = names
    info.display_names = display
    info.partial_error = partial
    return info


def _font_keys(font: ScannedFont) -> set[str]:
    keys: set[str] = set()
    candidates = set(font.names)
    # The file name is only a hint after the font itself was readable.
    if not font.problem:
        stem = Path(font.file_name).stem
        if stem:
            candidates.add(stem)
    for name in candidates:
        key = normalize_font_key(name)
        if key:
            keys.add(key)
    return keys


def classify_fonts(
    fonts: list[ScannedFont],
    requested: list[str],
) -> tuple[list[str], list[str], set[str]]:
    """Mark each attachment the way the cleaner would keep or remove it.

    USED and KEPT are kept. EXTRA and DUPLICATE are removed. An unreadable
    font is KEPT and does not satisfy a request. Returns warnings, the
    requested labels in report order, and the normalized keys that matched.
    """
    unique_requested = _unique_requested(requested)
    requested_by_key: dict[str, list[str]] = {}
    for label in unique_requested:
        key = normalize_font_key(label)
        if not key:
            continue
        requested_by_key.setdefault(key, [])
        if label not in requested_by_key[key]:
            requested_by_key[key].append(label)

    font_keys = {font.order: _font_keys(font) for font in fonts}
    warnings: list[str] = []
    for font in fonts:
        labels = _match_labels(font_keys[font.order], requested_by_key)
        if font.problem:
            font.tag = "KEPT"
            font.note = f"WARNING: could not read font ({font.problem}); keeping it"
        elif labels:
            font.tag = "USED"
            font.covers = labels
            if font.partial_error:
                warnings.append(
                    f'"{font.file_name}" matched a subtitle font, but face '
                    f"reading was incomplete ({font.partial_error})"
                )
        elif font.partial_error:
            font.tag = "KEPT"
            font.note = (
                "WARNING: could not read every face "
                f"({font.partial_error}); keeping it"
            )
        else:
            font.tag = "EXTRA"
            font.covers = []
            font.note = ""

    by_filename: dict[str, list[ScannedFont]] = {}
    for font in fonts:
        by_filename.setdefault(font.file_name.casefold(), []).append(font)
    for group in by_filename.values():
        if len(group) < 2:
            continue
        hashes = {font.content_hash for font in group if font.content_hash}
        if len(hashes) > 1:
            quoted = " and ".join(f'"{font.file_name}"' for font in group)
            warnings.append(
                f"{quoted} differ only by case but their contents differ; "
                "not treating them as duplicates"
            )

    by_hash: dict[str, list[ScannedFont]] = {}
    for font in fonts:
        if font.tag in {"USED", "KEPT"} and font.content_hash:
            by_hash.setdefault(font.content_hash, []).append(font)
    for group in by_hash.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda item: item.order)
        primary = group[0]
        for duplicate in group[1:]:
            duplicate.tag = "DUPLICATE"
            duplicate.covers = []
            duplicate.note = f'duplicate of "{primary.file_name}" (identical content)'

    covered: set[str] = set()
    for font in fonts:
        covered.update(font_keys[font.order] & requested_by_key.keys())
    return warnings, unique_requested, covered


def run_tool(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def extract_attachment(mkv_path: Path, attachment_id: int, dest: Path) -> None:
    result = run_tool(
        ["mkvextract", "attachments", str(mkv_path), f"{attachment_id}:{dest}"]
    )
    if result.returncode > 1 or not dest.is_file():
        detail = (result.stderr or "mkvextract failed").strip().splitlines()
        message = detail[-1] if detail else "mkvextract failed"
        raise RuntimeError(message)


def _track_label(track: dict) -> str:
    props = track.get("properties") or {}
    label = f"Track {track.get('id')}"
    name = props.get("track_name") or ""
    if name:
        label += f" – {name}"
    label += f" [{props.get('language') or 'und'}]"
    return label


def scan_mkv(mkv_path: Path) -> None:
    print()
    print(f"Scanning: {mkv_path.name}")
    print("─" * 70)

    try:
        result = run_tool(["mkvmerge", "-J", str(mkv_path)])
    except FileNotFoundError:
        print("ERROR: mkvmerge was not found.")
        return
    if result.returncode > 1 or not result.stdout.strip():
        detail = (result.stderr or result.stdout or "mkvmerge -J failed").strip()
        print(f"ERROR: mkvmerge could not read '{mkv_path.name}'.")
        print(f"  {detail.splitlines()[-1] if detail else 'mkvmerge -J failed'}")
        return

    try:
        mkv_info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: failed to parse mkvmerge JSON output: {exc}")
        return

    tracks = mkv_info.get("tracks") or []
    attachments = mkv_info.get("attachments") or []
    ass_tracks = [track for track in tracks if is_ass_track(track)]
    font_attachments = [item for item in attachments if is_font_attachment(item)]

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

    with tempfile.TemporaryDirectory(prefix="fonts_scanner_") as tmp:
        tmp_path = Path(tmp)
        extract_cmd = ["mkvextract", "tracks", str(mkv_path)]
        ass_paths: list[Path] = []
        for track in ass_tracks:
            dest = tmp_path / f"track_{track['id']}.ass"
            extract_cmd.append(f"{track['id']}:{dest}")
            ass_paths.append(dest)
        try:
            sub_result = run_tool(extract_cmd)
        except FileNotFoundError:
            print("ERROR: mkvextract was not found.")
            return
        if sub_result.returncode > 1 or any(not path.is_file() for path in ass_paths):
            detail = (sub_result.stderr or "mkvextract failed").strip().splitlines()
            message = detail[-1] if detail else "could not extract subtitles"
            print(f"ERROR extracting subtitle tracks: {message}")
            print("  Subtitles could not be read, so attachments were not classified.")
            return

        requested: list[str] = []
        print("\n  ASS tracks parsed:")
        try:
            for ass_path, track in zip(ass_paths, ass_tracks):
                found = read_ass_font_names(ass_path)
                print(f"    {_track_label(track)}: {len(found)} font(s) referenced")
                requested.extend(found)
        except OSError as exc:
            print(f"ERROR reading a subtitle track: {short_error(exc)}")
            print("  Subtitles could not be read, so attachments were not classified.")
            return

        fonts: list[ScannedFont] = []
        for order, attachment in enumerate(font_attachments):
            file_name = str(attachment.get("file_name") or f"attachment-{attachment['id']}")
            dest = tmp_path / f"att_{attachment['id']}_{safe_filename(file_name)}"
            try:
                extract_attachment(mkv_path, int(attachment["id"]), dest)
            except (RuntimeError, FileNotFoundError) as exc:
                fonts.append(
                    ScannedFont(
                        file_name=file_name,
                        order=order,
                        problem=short_error(exc),
                    )
                )
                continue
            fonts.append(read_embedded_font(dest, file_name, order))

        warnings, unique_requested, covered = classify_fonts(fonts, requested)
        _print_report(fonts, unique_requested, covered, warnings)


def _list_embedded_fonts_only(font_attachments: list) -> None:
    """Called when there are no ASS tracks — just list what's embedded."""
    print("\n  Embedded font attachments:")
    for att in font_attachments:
        print(f"    • {att['file_name']}  ({att.get('content_type', '?')})")


def _print_report(
    fonts: list[ScannedFont],
    required: list[str],
    covered: set[str],
    warnings: list[str],
) -> None:
    sep = "─" * 70
    missing = [name for name in required if not label_is_covered(name, covered)]
    extra = [font for font in fonts if font.tag == "EXTRA"]
    keep_count = sum(1 for font in fonts if font.tag in {"USED", "KEPT"})
    remove_count = sum(1 for font in fonts if font.tag in {"EXTRA", "DUPLICATE"})

    print(f"\n  FONTS NEEDED BY SUBTITLES  ({len(required)} total)")
    print(f"  {sep}")
    if required:
        for name in required:
            status = "[OK]" if label_is_covered(name, covered) else "[MISSING]"
            print(f"    {status}  {name}")
    else:
        print("    (none)")

    print(f"\n  FONTS EMBEDDED IN MKV  ({len(fonts)} file(s))")
    print(f"  {sep}")
    if fonts:
        for font in fonts:
            tag = f"[{font.tag}]"
            line = f"    {tag:<{_TAG_WIDTH}}  {font.file_name}"
            covers = _cover_text(font.covers)
            if covers:
                line += f"  →  covers: {covers}"
            print(line)
            if font.note:
                print(f"               {font.note}")
            if font.problem:
                continue
            visible = font.display_names or sorted(font.names)
            visible = sorted(visible, key=str.casefold)
            for iname in visible[:6]:
                print(f"               internal name: {iname}")
            if len(visible) > 6:
                print(f"               … and {len(visible) - 6} more")
    else:
        print("    (none)")

    print(f"\n  MISSING FONTS  ({len(missing)} font(s) not embedded)")
    print(f"  {sep}")
    if missing:
        for name in missing:
            print(f"    ✘  {name}")
    else:
        print("    ✓ All required fonts are present — nothing missing!")

    if extra:
        print(f"\n  EXTRA / UNUSED EMBEDDINGS  ({len(extra)} file(s) not needed by any subtitle)")
        print(f"  {sep}")
        for font in sorted(extra, key=lambda item: item.file_name.casefold()):
            print(f"    ⚠  {font.file_name}")

    print(f"\n  Fonts: keep {keep_count}, remove {remove_count}")
    if warnings:
        print(f"\n  WARNINGS  ({len(warnings)})")
        print(f"  {sep}")
        for warning in warnings:
            print(f"    {warning}")
    print()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except (OSError, ValueError):
            pass
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
        print('  python subtitle_fonts_scanner.py "My Show S01E01.mkv"')
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
