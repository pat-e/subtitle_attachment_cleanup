# Subtitle Attachment Cleanup

A Python script to automatically clean up unnecessary font attachments from MKV video files.

When you remove unwanted subtitle streams (like foreign languages) from an MKV using tools like MKVToolNix, the font files attached to those removed subtitles are typically left behind, needlessly inflating the file size. This script reads your MKV files, inspects the remaining SSA/ASS subtitle tracks to discover which fonts are actually being used, and writes a new MKV without the unused font attachments.

## Features

- Parses `.ass` / `.ssa` tracks for font names on `Style` lines (`[V4 Styles]` and `[V4+ Styles]`) and for inline `\fn` overrides in `[Events]`. A leading `@` marks vertical text and is not part of the font name.
- Reads every face inside `.ttf`, `.otf`, and `.ttc` files with fontTools. The whole file is kept when any face is used.
- Matches family, subfamily, typographic family plus subfamily, full name, PostScript name, and variable-font named instances, using every language stored in the name table. `Bahnschrift Condensed` matches a later face in a collection, and a variable font whose named style is `Condensed`.
- Ignores case, spaces, hyphens, and underscores, so `Bahnschrift Condensed`, `BahnschriftCondensed`, and `Bahnschrift-Condensed` are the same name. Cryptic filenames such as `arialbd.ED3587CD.ttf` are matched from the name table, not from the file name alone.
- Drops a later attachment when its bytes are identical to one already kept (`CENTURY.TTF` and `Century.TTF`). Two files whose names differ only by case but whose contents differ, such as `Inkfree.ttf` and `InkFree.ttf`, are both kept and reported.
- Keeps a font, and prints a warning, when the file cannot be read.
- Leaves non-font attachments in place, such as cover images.
- Leaves tracks, default flags, chapters, the title, and track names unchanged, including an empty title and blank track names.
- Moves the originals to `original/` and writes cleaned files to `finished/`. If no font is removed, the file is copied unchanged.

## Prerequisites

Ensure the following tools and libraries are installed and accessible in your system's PATH:

1. **Python 3.x**
2. **MKVToolNix** (specifically `mkvmerge` and `mkvextract`)
3. **fonttools** (Python library)

Install the required Python dependency:

For Windows or Ubuntu, you can use `pip`:

```bash
pip install fonttools
```

For Arch Linux (which enforces PEP 668), you should use `pacman` to install the system package:

```bash
sudo pacman -S python-fonttools
```

## Usage

Run the script from the directory that contains the `.mkv` files. You can also put the script on your `PATH` and run it from that directory.

```bash
python subtitle_fonts_cleaner.py --help
python subtitle_fonts_cleaner.py --dry-run
python subtitle_fonts_cleaner.py
```

`--help` prints the usage text and exits.

`--dry-run` prints `KEEP` or `REMOVE` for every font attachment, with the reason, and lists every requested font that matched nothing. It does not write or move files, and it does not create `original/` or `finished/`.

This is the main script and the intended workflow for batch cleanup.

### Folder structure

A real run uses these folders in the working directory:

- `temp_subs_fonts/` — scratch space while attachments are inspected. The script removes it at the end of the run when the folder is empty.
- `original/` — the unmodified `.mkv` files, moved here after a successful clean.
- `finished/` — the cleaned `.mkv` files: the original tracks, chapters, and non-font attachments, plus only the fonts the subtitles use.

### Sample dry-run

```text
Processing: Example Episode 01.mkv
  ASS/SSA tracks: 1
  Font attachments: 5
  Requested fonts (2): Bahnschrift Condensed, Century
  KEEP   1 BahnschriftCondensed.ttc - matches requested font "Bahnschrift Condensed"
  KEEP   2 BahnschriftCondensed-VariableCollection.ttc - matches requested font "Bahnschrift Condensed"
  KEEP   3 CENTURY.TTF - matches requested font "Century"
  REMOVE 4 Century.TTF - duplicate of "CENTURY.TTF" (identical content)
  REMOVE 5 Comic.ttf - not used by any subtitle
  Requested fonts with no matching attachment:
    - Not Embedded
  Fonts: keep 3, remove 2
  Dry run: no files were written or moved.
```

## Supplemental script: font scanner (read-only)

This repository also includes `subtitle_fonts_scanner.py`, a companion script for inspection and reporting.

Use the cleaner's `--dry-run` to preview a whole folder. Use the scanner when you want a per-track report for one file. The scanner uses the same font matching as the cleaner: every face, variable-font named instances, and names compared without case, spaces, hyphens, or underscores. It does not modify files and does not create output folders.

### What the scanner reports

- How many ASS/SSA tracks and font attachments the file contains. Non-font attachments, such as cover images, are left out of the report.
- The fonts each track requests, spelled the way the subtitle writes them. A leading `@` stays in this list.
- `[OK]` when a requested font matches an embedded file, and `[MISSING]` when it does not.
- Each readable font file, followed by up to six internal names.
- `[USED]` when a face or the file name matches a request. The covers line drops a leading `@`.
- `[EXTRA]` when no subtitle uses that file. These files are listed again as unused embeddings.
- `[DUPLICATE]` when the bytes match an earlier file that is kept. The cleaner would remove this copy.
- `[KEPT]` when the file could not be read. The cleaner would keep it, and it does not satisfy a request.
- `Fonts: keep N, remove M`, the same counts the cleaner would use.
- A warning when two file names differ only by case but the contents differ, and when a matched file could not be read in full.

### Scanner usage

Run it against a single MKV file:

```bash
python subtitle_fonts_scanner.py "input.mkv"
# If in your PATH, simply execute: subtitle_fonts_scanner.py "input.mkv"
```

### Sample output

```text
Scanning: Example Episode 01.mkv
──────────────────────────────────────────────────────────────────────
  ASS/SSA subtitle tracks : 2
  Font attachments        : 6

  ASS tracks parsed:
    Track 2 – Signs [eng]: 3 font(s) referenced
    Track 3 [ger]: 1 font(s) referenced

  FONTS NEEDED BY SUBTITLES  (4 total)
  ──────────────────────────────────────────────────────────────────────
    [OK]  Bahnschrift Condensed
    [OK]  Century
    [MISSING]  Not Embedded
    [OK]  @Vertical Gothic

  FONTS EMBEDDED IN MKV  (6 file(s))
  ──────────────────────────────────────────────────────────────────────
    [USED]       BahnschriftCondensed.ttc  →  covers: Bahnschrift Condensed
               internal name: Bahnschrift
               internal name: Condensed
    [USED]       CENTURY.TTF  →  covers: Century
               internal name: Century
    [DUPLICATE]  Century.TTF
               duplicate of "CENTURY.TTF" (identical content)
               internal name: Century
    [EXTRA]      Comic.ttf
               internal name: Comic Sans MS
    [USED]       Vertical.otf  →  covers: Vertical Gothic
               internal name: Vertical Gothic
    [KEPT]       broken.ttf
               WARNING: could not read font (Not a TrueType or OpenType font (not enough data)); keeping it

  MISSING FONTS  (1 font(s) not embedded)
  ──────────────────────────────────────────────────────────────────────
    ✘  Not Embedded

  EXTRA / UNUSED EMBEDDINGS  (1 file(s) not needed by any subtitle)
  ──────────────────────────────────────────────────────────────────────
    ⚠  Comic.ttf

  Fonts: keep 4, remove 2
```

### Typical workflow

1. Run `subtitle_fonts_cleaner.py --dry-run` in the episode folder to see which fonts would be kept or removed.
2. Optionally run `subtitle_fonts_scanner.py` on one file for a per-track report.
3. Run `subtitle_fonts_cleaner.py` to process every MKV in the working directory.
4. Optionally run the scanner again on a file in `finished/` to verify the result.

## License

MIT License. See the [LICENSE](LICENSE) file for more details.
