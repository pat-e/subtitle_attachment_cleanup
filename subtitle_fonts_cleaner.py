#!/usr/bin/env python3
"""Remove unused font attachments from every MKV in the current directory.

The script reads each ASS/SSA subtitle track, works out which fonts it asks
for, and writes a new MKV that keeps only those fonts. Matching covers every
face in a .ttf, .otf, or .ttc, including variable-font named instances.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from fontTools.ttLib import TTCollection, TTFont
except ImportError:  # Help must still work if fontTools is missing.
    TTCollection = None
    TTFont = None


_NAME_IDS = (1, 2, 4, 6, 16, 17)
_FONT_MIME_TOKENS = ("font", "truetype", "opentype", "sfnt")
_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc", ".tte", ".dfont", ".woff", ".woff2"}
_FN_TAG = re.compile(r"\\fn([^\\}]+)")
_STYLE_SECTIONS = {"[v4+ styles]", "[v4 styles]"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="subtitle_fonts_cleaner.py",
        description=(
            "Remove font attachments that no ASS/SSA subtitle uses.\n"
            "\n"
            "Run this in a folder of MKV files. For each file it reads every\n"
            "ASS/SSA track, keeps the attached fonts those tracks request, and\n"
            "drops the rest. Non-font attachments stay. Tracks, default flags,\n"
            "chapters, the title, and track names are left as they are.\n"
            "\n"
            "Originals move into original/ and cleaned files are written to\n"
            "finished/. When a font cannot be identified it is kept."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print keep or remove for each font, list requested fonts that "
            "matched nothing, and do not write or move any files."
        ),
    )
    return parser.parse_args(argv)


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


@dataclass
class FontRead:
    names: set[str]
    partial_error: str | None = None


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


def collect_font_names(path: Path) -> FontRead:
    """Read every face. Raise if the file yields no names at all."""
    if TTFont is None or TTCollection is None:
        raise RuntimeError("fontTools is not installed")
    opener, faces = _open_faces(path)
    try:
        if not faces:
            raise RuntimeError("no faces in font file")
        names: set[str] = set()
        failures: list[str] = []
        for index, face in enumerate(faces):
            try:
                names.update(face_match_names(face))
            except Exception as exc:
                failures.append(f"face {index}: {short_error(exc)}")
        if not names:
            detail = "; ".join(failures) if failures else "no readable font names"
            raise RuntimeError(detail)
        partial = "; ".join(failures) if failures else None
        return FontRead(names=names, partial_error=partial)
    finally:
        close = getattr(opener, "close", None)
        if close is not None:
            close()


def short_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    message = text[0] if text else exc.__class__.__name__
    if len(message) > 200:
        return message[:197] + "..."
    return message


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


def mkv_option_value(value: str) -> str:
    """A value that starts with @ would be read as an mkvmerge options file."""
    if value.startswith("@"):
        return "@" + value
    return value


@dataclass
class FontInfo:
    file_name: str
    attachment_id: int
    order: int
    names: set[str] = field(default_factory=set)
    content_hash: str | None = None
    problem: str | None = None
    partial_error: str | None = None


@dataclass
class FontDecision:
    file_name: str
    attachment_id: int
    order: int
    action: str
    reason: str


@dataclass
class FontPlan:
    decisions: list[FontDecision]
    warnings: list[str]
    unmatched: list[str]

    @property
    def keep_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.action == "keep")

    @property
    def remove_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.action == "remove")


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


def _format_match_reason(labels: list[str]) -> str:
    shown_labels: list[str] = []
    seen: set[str] = set()
    for label in labels:
        display = _display_font_name(label)
        if display and display not in seen:
            seen.add(display)
            shown_labels.append(display)
    shown = ", ".join(f'"{label}"' for label in shown_labels[:4])
    extra = len(shown_labels) - 4
    if extra > 0:
        shown += f" and {extra} more"
    noun = "font" if len(shown_labels) == 1 else "fonts"
    return f"matches requested {noun} {shown}"


def decide_fonts(
    fonts: list[FontInfo],
    requested: list[str],
    keep_unused: bool = False,
    unused_reason: str = "not used by any subtitle",
) -> FontPlan:
    """Choose keep or remove for each font.

    A font is kept when any face matches a requested name, when its file name
    matches, or when its contents could not be read. Byte-identical fonts are
    collapsed to the earliest attachment. Files whose names differ only by
    case but whose bytes differ are both kept and reported.
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

    font_keys: dict[int, set[str]] = {}
    for font in fonts:
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
        font_keys[font.order] = keys

    actions: dict[int, tuple[str, str]] = {}
    warnings: list[str] = []
    for font in fonts:
        labels = _match_labels(font_keys[font.order], requested_by_key)
        # An unreadable file is kept even when its name looks like a match.
        # The bytes stay in the MKV and the warning says why.
        if font.problem:
            actions[font.order] = (
                "keep",
                f"WARNING: could not read font ({font.problem}); keeping it",
            )
        elif labels:
            actions[font.order] = ("keep", _format_match_reason(labels))
            if font.partial_error:
                warnings.append(
                    f'"{font.file_name}" matched a subtitle font, but face '
                    f"reading was incomplete ({font.partial_error})"
                )
        elif font.partial_error:
            actions[font.order] = (
                "keep",
                "WARNING: could not read every face "
                f"({font.partial_error}); keeping it",
            )
        elif keep_unused:
            actions[font.order] = ("keep", unused_reason)
        else:
            actions[font.order] = ("remove", unused_reason)

    by_filename: dict[str, list[FontInfo]] = {}
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

    by_hash: dict[str, list[FontInfo]] = {}
    for font in fonts:
        action, _reason = actions[font.order]
        if action == "keep" and font.content_hash:
            by_hash.setdefault(font.content_hash, []).append(font)
    for group in by_hash.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda font: font.order)
        primary = group[0]
        for duplicate in group[1:]:
            actions[duplicate.order] = (
                "remove",
                f'duplicate of "{primary.file_name}" (identical content)',
            )

    covered: set[str] = set()
    for font in fonts:
        covered.update(font_keys[font.order] & requested_by_key.keys())
    unmatched = [
        label
        for label in unique_requested
        if normalize_font_key(label) and normalize_font_key(label) not in covered
    ]

    decisions = [
        FontDecision(
            file_name=font.file_name,
            attachment_id=font.attachment_id,
            order=font.order,
            action=actions[font.order][0],
            reason=actions[font.order][1],
        )
        for font in fonts
    ]
    return FontPlan(decisions=decisions, warnings=warnings, unmatched=unmatched)


def run_tool(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def load_mkv_info(mkv_path: Path) -> dict:
    result = run_tool(["mkvmerge", "-J", str(mkv_path)])
    if result.returncode > 1 or not result.stdout.strip():
        detail = (result.stderr or result.stdout or "mkvmerge -J failed").strip()
        raise RuntimeError(detail.splitlines()[-1])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse mkvmerge JSON ({exc})") from exc


def container_title(info: dict) -> str:
    props = (info.get("container") or {}).get("properties") or {}
    title = props.get("title")
    return title if isinstance(title, str) else ""


def track_name(track: dict) -> str:
    name = (track.get("properties") or {}).get("track_name")
    return name if isinstance(name, str) else ""


def extract_attachment(mkv_path: Path, attachment_id: int, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = run_tool(
        ["mkvextract", "attachments", str(mkv_path), f"{attachment_id}:{dest}"]
    )
    if result.returncode > 1 or not dest.is_file():
        detail = (result.stderr or "mkvextract failed").strip().splitlines()
        message = detail[-1] if detail else "mkvextract failed"
        raise RuntimeError(message)


def inspect_font(attachment: dict, order: int, dest: Path) -> FontInfo:
    info = FontInfo(
        file_name=str(attachment.get("file_name") or dest.name),
        attachment_id=int(attachment["id"]),
        order=order,
    )
    try:
        info.content_hash = file_sha256(dest)
    except OSError as exc:
        info.problem = short_error(exc)
        return info
    try:
        read = collect_font_names(dest)
    except Exception as exc:
        info.problem = short_error(exc)
        return info
    info.names = read.names
    info.partial_error = read.partial_error
    return info


def print_plan(
    ass_count: int,
    requested: list[str],
    other_count: int,
    plan: FontPlan,
    note: str | None = None,
) -> None:
    print(f"  ASS/SSA tracks: {ass_count}")
    print(f"  Font attachments: {len(plan.decisions)}")
    if other_count:
        print(f"  Preserving {other_count} non-font attachment(s).")
    labels = _unique_requested(requested)
    if labels:
        print(f"  Requested fonts ({len(labels)}): {', '.join(labels)}")
    else:
        print("  Requested fonts: none")
    if note:
        print(f"  WARNING: {note}")
    for decision in plan.decisions:
        label = "KEEP  " if decision.action == "keep" else "REMOVE"
        print(f"  {label} {decision.attachment_id} {decision.file_name} - {decision.reason}")
    for warning in plan.warnings:
        print(f"  WARNING: {warning}")
    if plan.unmatched:
        print("  Requested fonts with no matching attachment:")
        for name in plan.unmatched:
            print(f"    - {name}")
    else:
        print("  Requested fonts with no matching attachment: none")
    print(f"  Fonts: keep {plan.keep_count}, remove {plan.remove_count}")


def remux(
    mkv_path: Path,
    out_path: Path,
    info: dict,
    kept_attachments: list[dict],
) -> None:
    """Rebuild the MKV without the dropped fonts.

    Title and track names are passed through explicitly, including blanks, so
    an empty title is not replaced by the output file name and blank track
    names stay blank. Chapters, defaults, and the other tracks are copied
    with the source file.
    """
    command = [
        "mkvmerge",
        "-o",
        str(out_path),
        "--title",
        mkv_option_value(container_title(info)),
    ]
    for track in info.get("tracks") or []:
        command.extend(
            ["--track-name", f"{track['id']}:{mkv_option_value(track_name(track))}"]
        )
    command.extend(["--no-attachments", str(mkv_path)])
    for attachment in kept_attachments:
        description = attachment.get("description") or ""
        if isinstance(description, str) and description:
            command.extend(
                ["--attachment-description", mkv_option_value(description)]
            )
        file_name = str(attachment.get("file_name") or "attachment")
        command.extend(["--attachment-name", mkv_option_value(file_name)])
        mime = attachment.get("content_type") or ""
        if mime:
            command.extend(["--attachment-mime-type", str(mime)])
        command.extend(["--attach-file", str(attachment["temp_path"])])

    if out_path.exists():
        out_path.unlink()
    result = subprocess.run(command)
    if result.returncode > 1 or not out_path.is_file():
        if out_path.exists():
            out_path.unlink()
        raise RuntimeError(f"mkvmerge failed (exit {result.returncode})")


def process_mkv(
    mkv_path: Path,
    original_dir: Path,
    finished_dir: Path,
    temp_dir: Path,
    dry_run: bool,
    job_index: int,
) -> bool:
    print(f"\nProcessing: {mkv_path.name}")
    work = temp_dir / f"job_{job_index}"
    try:
        work.mkdir(parents=True, exist_ok=True)
        return _process_mkv(mkv_path, original_dir, finished_dir, work, dry_run)
    except Exception as exc:
        print(f"  Error: {short_error(exc)}")
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _process_mkv(
    mkv_path: Path,
    original_dir: Path,
    finished_dir: Path,
    work: Path,
    dry_run: bool,
) -> bool:
    info = load_mkv_info(mkv_path)
    tracks = info.get("tracks") or []
    attachments = info.get("attachments") or []
    ass_tracks = [track for track in tracks if is_ass_track(track)]
    font_attachments = [item for item in attachments if is_font_attachment(item)]
    font_identities = {id(item) for item in font_attachments}
    other_attachments = [item for item in attachments if id(item) not in font_identities]

    subtitle_error = None
    requested: list[str] = []
    if ass_tracks:
        extract_cmd = ["mkvextract", "tracks", str(mkv_path)]
        ass_paths: list[Path] = []
        for track in ass_tracks:
            dest = work / f"track_{track['id']}.ass"
            extract_cmd.append(f"{track['id']}:{dest}")
            ass_paths.append(dest)
        result = run_tool(extract_cmd)
        if result.returncode > 1 or any(not path.is_file() for path in ass_paths):
            detail = (result.stderr or "mkvextract failed").strip().splitlines()
            subtitle_error = detail[-1] if detail else "could not extract subtitles"
        else:
            for ass_path in ass_paths:
                try:
                    requested.extend(sorted(read_ass_font_names(ass_path)))
                except OSError as exc:
                    subtitle_error = short_error(exc)
                    requested = []
                    break

    fonts: list[FontInfo] = []
    for order, attachment in enumerate(font_attachments):
        dest = work / f"att_{attachment['id']}_{safe_filename(str(attachment.get('file_name') or 'font'))}"
        attachment["temp_path"] = dest
        try:
            extract_attachment(mkv_path, int(attachment["id"]), dest)
        except RuntimeError as exc:
            fonts.append(
                FontInfo(
                    file_name=str(attachment.get("file_name") or dest.name),
                    attachment_id=int(attachment["id"]),
                    order=order,
                    problem=short_error(exc),
                )
            )
            continue
        fonts.append(inspect_font(attachment, order, dest))

    for attachment in other_attachments:
        dest = work / f"other_{attachment['id']}_{safe_filename(str(attachment.get('file_name') or 'file'))}"
        attachment["temp_path"] = dest

    keep_unused = subtitle_error is not None
    unused_reason = (
        f"WARNING: subtitles could not be read ({subtitle_error}); keeping it"
        if subtitle_error
        else "not used by any subtitle"
    )
    plan = decide_fonts(
        fonts,
        [] if subtitle_error else requested,
        keep_unused=keep_unused,
        unused_reason=unused_reason,
    )
    note = None
    if subtitle_error:
        note = f"subtitles could not be read ({subtitle_error}); keeping every font"
    elif not ass_tracks and fonts:
        note = "no ASS/SSA subtitle tracks; font attachments are unused"
    print_plan(
        len(ass_tracks),
        [] if subtitle_error else requested,
        len(other_attachments),
        plan,
        note,
    )

    if dry_run:
        print("  Dry run: no files were written or moved.")
        return True

    original_dir.mkdir(exist_ok=True)
    finished_dir.mkdir(exist_ok=True)
    out_path = finished_dir / mkv_path.name
    if plan.remove_count == 0:
        print("  No fonts removed. Copying the file unchanged.")
        shutil.copy2(mkv_path, out_path)
    else:
        print(f"  Remuxing to {out_path.name}")
        kept_ids = {
            decision.attachment_id
            for decision in plan.decisions
            if decision.action == "keep"
        }
        # Non-font attachments are not in the decision list, so they stay.
        # Refuse to rewrite the file if a kept attachment was not extracted:
        # dropping it would remove a font we meant to keep.
        kept_attachments = []
        missing: list[str] = []
        for attachment in attachments:
            if is_font_attachment(attachment) and int(attachment["id"]) not in kept_ids:
                continue
            if "temp_path" not in attachment or not Path(attachment["temp_path"]).is_file():
                dest = work / (
                    f"other_{attachment['id']}_"
                    f"{safe_filename(str(attachment.get('file_name') or 'file'))}"
                )
                attachment["temp_path"] = dest
                try:
                    extract_attachment(mkv_path, int(attachment["id"]), dest)
                except RuntimeError as exc:
                    missing.append(f"{attachment.get('file_name')} ({exc})")
                    continue
            if not Path(attachment["temp_path"]).is_file():
                missing.append(str(attachment.get("file_name") or attachment["id"]))
                continue
            kept_attachments.append(attachment)
        if missing:
            raise RuntimeError(
                "refusing to rewrite; could not extract attachments that must be kept: "
                + "; ".join(missing)
            )
        remux(mkv_path, out_path, info, kept_attachments)
    shutil.move(str(mkv_path), str(original_dir / mkv_path.name))
    print(f"  Moved the original to {original_dir.name}/")
    return True


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except (OSError, ValueError):
            pass
    args = parse_args(argv)
    if TTFont is None or TTCollection is None:
        print("Error: 'fonttools' is required to read font names.")
        print("Install it with: pip install fonttools")
        print("On Arch Linux: sudo pacman -S python-fonttools")
        return 1
    missing = [tool for tool in ("mkvmerge", "mkvextract") if shutil.which(tool) is None]
    if missing:
        print("Error: missing required tool(s): " + ", ".join(missing))
        return 1

    root = Path.cwd()
    mkv_files = sorted(
        path for path in root.iterdir() if path.is_file() and path.suffix.lower() == ".mkv"
    )
    if not mkv_files:
        print("No MKV files found in the current directory.")
        return 0

    original_dir = root / "original"
    finished_dir = root / "finished"
    temp_dir = root / "temp_subs_fonts"
    temp_dir.mkdir(exist_ok=True)
    failed = False
    try:
        for job_index, mkv_path in enumerate(mkv_files):
            if not process_mkv(
                mkv_path, original_dir, finished_dir, temp_dir, args.dry_run, job_index
            ):
                failed = True
    finally:
        # Remove the scratch folder only when it is empty, so a directory the
        # user already used for something else is left alone.
        try:
            temp_dir.rmdir()
        except OSError:
            pass

    if failed:
        print("\nFinished with errors.")
        return 1
    print("\nAll tasks completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
