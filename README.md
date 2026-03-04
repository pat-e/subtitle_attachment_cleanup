# Subtitle Attachment Cleanup

A Python script to automatically clean up unnecessary font attachments from MKV video files. 

When you remove unwanted subtitle streams (like foreign languages) from an MKV using tools like MKVToolNix, the font files attached to those removed subtitles are typically left behind, needlessly inflating the file size. This script reads your MKV files, thoroughly inspects the remaining SSA/ASS subtitle tracks to discover which fonts are actually being used, and builds a new MKV file—leaving behind any orphaned or unused font attachments.

## Features
- Intelligently extracts and parses `.ass` / `.ssa` subtitle tracks from MKV containers.
- Identifies both top-level font declarations (in `[V4+ Styles]`) and inline font overrides (in `[Events]`).
- Uses deep metadata scanning (`fonttools`) to accurately match requested font families against attached font files, even if the attached files have cryptic filenames (e.g., `arialbd.ED3587CD.ttf`).
- Safely preserves all non-font attachments (like cover images).
- Automatically moves original MKVs to an `original/` backup folder and places the cleaned files in a `finished/` folder.

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
Simply place the script inside the directory containing the `.mkv` files you wish to process and run it. You can also place the script in your personal `bin` or `PATH` folder to run it from anywhere.

```bash
python subtitle_fonts_cleaner.py
# If in your PATH, simply execute: subtitle_fonts_cleaner.py
```

### Folder Structure
Upon execution, the script will create three folders in your working directory:
- `temp_subs_fonts/` - A temporary directory used during processing (automatically deleted upon completion).
- `original/` - Your original, unmodified `.mkv` files are safely moved here.
- `finished/` - The new, lean `.mkv` files containing only the active ASS tracks, required font attachments, and original audio/video streams.

## License
MIT License. See the [LICENSE](LICENSE) file for more details.