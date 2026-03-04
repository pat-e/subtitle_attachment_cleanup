#!/usr/bin/env python3

import os
import re
import sys
import json
import shutil
import subprocess
from pathlib import Path

# Try importing fontTools, which is needed to read internal font names
try:
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: 'fonttools' is required to accurately read internal font names.")
    print("Please install it by running: pip install fonttools (for Windows/Ubuntu)")
    print("Or: sudo pacman -S python-fonttools (for Arch Linux)")
    sys.exit(1)

def get_ass_font_names(ass_path):
    """
    Parse an ASS file and return a set of all font names used.
    It checks the [V4+ Styles] section and \fn overrides in [Events].
    """
    fonts = set()
    try:
        with open(ass_path, 'r', encoding='utf-8', errors='ignore') as f:
            in_styles = False
            in_events = False
            fontname_idx = -1
            
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith('['):
                    in_styles = (line == '[V4+ Styles]')
                    in_events = (line == '[Events]')
                    continue
                    
                if in_styles:
                    if line.startswith('Format:'):
                        format_str = line[len('Format:'):].strip()
                        format_cols = [col.strip().lower() for col in format_str.split(',')]
                        if 'fontname' in format_cols:
                            fontname_idx = format_cols.index('fontname')
                    elif line.startswith('Style:') and fontname_idx != -1:
                        style_str = line[len('Style:'):].strip()
                        style_cols = [col.strip() for col in style_str.split(',')]
                        if len(style_cols) > fontname_idx:
                            fonts.add(style_cols[fontname_idx])
                            
                if in_events:
                    if line.startswith('Dialogue:'):
                        # Find overrides like \fnArial or \fnComic Sans MS
                        matches = re.findall(r'\\fn([^\\}]+)', line)
                        for match in matches:
                            fonts.add(match.strip())
    except Exception as e:
        print(f"Warning: Failed to read {ass_path.name}: {e}")
                        
    return {f.lower() for f in fonts} # case-insensitive set

def get_internal_font_names(font_path):
    """
    Extract internal font names (Family, Full Name, Typographic Family) from a TTF/OTF.
    """
    names = set()
    try:
        # fontNumber=0 works for single fonts and the first font in a collection (.ttc)
        font = TTFont(str(font_path), fontNumber=0) 
        for record in font['name'].names:
            if record.nameID in (1, 4, 16): 
                try:
                    text = record.toUnicode()
                    names.add(text.lower())
                except:
                    pass
        font.close()
    except Exception as e:
        print(f"      Warning: Could not read metadata for {font_path.name}: {e}")
    return names

def safe_filename(name):
    """Make string safe for temporary filename."""
    return "".join(c for c in name if c.isalpha() or c.isdigit() or c in ' .-_').rstrip()

def main():
    root_dir = Path.cwd()
    mkv_files = list(root_dir.glob("*.mkv"))
    if not mkv_files:
        print("No MKV files found in the current directory.")
        return

    original_dir = root_dir / "original"
    finished_dir = root_dir / "finished"
    temp_dir = root_dir / "temp_subs_fonts"

    original_dir.mkdir(exist_ok=True)
    finished_dir.mkdir(exist_ok=True)
    temp_dir.mkdir(exist_ok=True)

    for mkv_path in mkv_files:
        print(f"\nProcessing: {mkv_path.name}")
        
        # 1. Get MKV info via mkvmerge -J
        result = subprocess.run(['mkvmerge', '-J', str(mkv_path)], capture_output=True, text=True, encoding='utf-8')
        if result.returncode != 0:
            print(f"  Error reading {mkv_path.name} with mkvmerge. Skipping.")
            continue
            
        try:
            mkv_info = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"  Error parsing JSON output from mkvmerge for {mkv_path.name}. Skipping.")
            continue

        tracks = mkv_info.get('tracks', [])
        attachments = mkv_info.get('attachments', [])

        # Fix condition: checking codec or codec_id
        ass_tracks = [t for t in tracks if t.get('type') == 'subtitles' and 
                      ('S_TEXT/ASS' in str(t.get('properties', {}).get('codec_id', '')) or 
                       'S_TEXT/SSA' in str(t.get('properties', {}).get('codec_id', '')) or 
                       'SubStationAlpha' in str(t.get('codec', '')))]
        
        # Identify fonts vs other attachments. We look at mime types.
        font_mimes = ['font', 'truetype', 'opentype', 'sfnt', 'application/x-truetype-font']
        font_attachments = [a for a in attachments if any(m in a.get('content_type', '').lower() for m in font_mimes)]
        other_attachments = [a for a in attachments if a not in font_attachments]

        if not ass_tracks and not font_attachments:
            print("  No ASS tracks and no fonts found. Copying without changes.")
            shutil.copy2(str(mkv_path), str(finished_dir / mkv_path.name))
            shutil.move(str(mkv_path), str(original_dir / mkv_path.name))
            continue

        # 2. Extract ASS tracks
        required_fonts = set()
        if ass_tracks:
            print(f"  Extracting {len(ass_tracks)} ASS track(s)...")
            extract_ass_cmd = ['mkvextract', 'tracks', str(mkv_path)]
            ass_temp_files = []
            for t in ass_tracks:
                track_id = t['id']
                out_ass = temp_dir / f"{mkv_path.stem}_track_{track_id}.ass"
                extract_ass_cmd.append(f"{track_id}:{out_ass}")
                ass_temp_files.append(out_ass)
                
            subprocess.run(extract_ass_cmd, check=True)
            
            # Retrieve required names
            for ass_file in ass_temp_files:
                ass_fonts = get_ass_font_names(ass_file)
                print(f"    {ass_file.name} references {len(ass_fonts)} distinct font(s): {list(ass_fonts)[:5]}{'...' if len(ass_fonts) > 5 else ''}")
                required_fonts.update(ass_fonts)
                
            print(f"  Total distinct font name(s) referenced in ASS across MKV: {len(required_fonts)}")
            print(f"  Required fonts list: {list(required_fonts)}")

        # 3. Extract and verify font attachments
        fonts_to_keep = [] 
        if font_attachments:
            print(f"  Extracting {len(font_attachments)} font attachment(s) to verify...")
            extract_att_cmd = ['mkvextract', 'attachments', str(mkv_path)]
            for att in font_attachments:
                att_id = att['id']
                out_font = temp_dir / f"att_{att_id}_{safe_filename(att['file_name'])}"
                extract_att_cmd.append(f"{att_id}:{out_font}")
                att['temp_path'] = out_font 
                
            subprocess.run(extract_att_cmd, check=True)
            
            # Check which fonts match the required list
            # We must be careful because some MKVs have fonts but no ASS referencing them
            for att in font_attachments:
                att_path = att['temp_path']
                if not att_path.exists():
                    continue
                    
                keep_this_font = False
                
                # Check 1: Exact filename match (minus extension)
                filename_no_ext = att_path.stem.lower()
                if filename_no_ext in required_fonts:
                    print(f"    [MATCH] '{att['file_name']}' matched exactly via filename.")
                    keep_this_font = True
                
                # Check 2: Internal TrueType/OpenType name match using fonttools
                if not keep_this_font:
                    internal_names = get_internal_font_names(att_path)
                    intersect = required_fonts.intersection(internal_names)
                    if intersect:
                        print(f"    [MATCH] '{att['file_name']}' matched via internal names: {intersect}")
                        keep_this_font = True
                    else:
                        print(f"    [SKIP]  '{att['file_name']}' did not match any required font. Internal names: {list(internal_names)[:5]}")
                        
                if keep_this_font:
                    fonts_to_keep.append(att)
                    
        print(f"  Keeping {len(fonts_to_keep)} required font attachment(s).")
        
        # 4. Extract other non-font attachments (like cover.jpg) so we don't lose them!
        other_to_keep = []
        if other_attachments:
            print(f"  Extracting {len(other_attachments)} non-font attachment(s) to preserve them...")
            extract_other_cmd = ['mkvextract', 'attachments', str(mkv_path)]
            for att in other_attachments:
                att_id = att['id']
                out_other = temp_dir / f"other_{att_id}_{safe_filename(att['file_name'])}"
                extract_other_cmd.append(f"{att_id}:{out_other}")
                att['temp_path'] = out_other
            
            subprocess.run(extract_other_cmd, check=True)
            other_to_keep.extend(other_attachments)
            
        # 5. Remux using mkvmerge
        out_mkv = finished_dir / mkv_path.name
        remux_cmd = ['mkvmerge', '-o', str(out_mkv), '--no-attachments', str(mkv_path)]
        
        for att in fonts_to_keep + other_to_keep:
            remux_cmd.extend([
                '--attachment-name', att['file_name'],
                '--attachment-mime-type', att['content_type'],
                '--attach-file', str(att['temp_path'])
            ])
            
        print(f"  Remuxing to: {out_mkv.name}")
        subprocess.run(remux_cmd, check=True)
        
        # 6. Move original processed file
        print(f"  Moving original file to 'original' folder...")
        shutil.move(str(mkv_path), str(original_dir / mkv_path.name))
        
        # Cleanup temp for this MKV
        for item in temp_dir.iterdir():
            if item.is_file():
                item.unlink()
                
    # Final cleanup of temp directory
    try:
        temp_dir.rmdir()
    except OSError:
        pass # Not empty, or some other error, keep it for debugging
        
    print("\nAll tasks completed.")

if __name__ == '__main__':
    main()
