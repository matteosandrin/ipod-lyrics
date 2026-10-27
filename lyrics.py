#!/usr/bin/env python3
"""Fetch lyrics from LRCLIB and write them into audio tags after manual approval.

Usage:
  ./lyrics.py                         # whole library (current dir)
  ./lyrics.py FLACs/Justice           # one folder
  ./lyrics.py "FLACs/Justice/Woman/10. Justice - Close Call.flac"
  ./lyrics.py -a                      # auto-write exact matches, ask about the rest
  ./lyrics.py --fix-tags              # repair tags for iPod: ID3v2.3 for MP3, LF lyrics

Requires: mutagen, requests (both already installed).
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import mutagen
import requests
from mutagen.flac import FLAC
from mutagen.id3 import ID3, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://lrclib.net/api"
UA = "lyrics.py/0.1 (personal library tagger)"
AUDIO_EXTS = {".mp3", ".m4a", ".flac"}
PREVIEW_LINES = 12
# Rejected / not-found songs are remembered here so a re-run of the whole
# library does not ask about them again. Use --include-skipped to override.
SKIP_FILE = Path(__file__).with_name(".lyrics-skip.json")

session = requests.Session()
session.headers["User-Agent"] = UA
# lrclib.net sometimes drops a TLS handshake or answers 5xx under load.
# Retry those with backoff (1s, 2s, 4s) instead of failing the song.
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET",), raise_on_status=False,
)))

TTY = sys.stdout.isatty()


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if TTY else str(s)


def bold(s):
    return c("1", s)


def dim(s):
    return c("2", s)


def green(s):
    return c("32", s)


def yellow(s):
    return c("33", s)


def red(s):
    return c("31", s)


# --- tag reading / writing -------------------------------------------------


def first(v):
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else ""
    return str(v) if v is not None else ""


def read_info(path):
    """Return dict with artist, title, album, duration, existing lyrics."""
    f = mutagen.File(path)
    if f is None:
        return None
    info = {"artist": "", "title": "", "album": "", "duration": 0, "lyrics": ""}
    info["duration"] = int(round(f.info.length or 0))
    if isinstance(f, MP3):
        t = f.tags or {}
        info["artist"] = first(t.get("TPE1"))
        info["title"] = first(t.get("TIT2"))
        info["album"] = first(t.get("TALB"))
        uslt = [fr for k, fr in t.items() if k.startswith("USLT")]
        info["lyrics"] = uslt[0].text if uslt else ""
    elif isinstance(f, MP4):
        t = f.tags or {}
        info["artist"] = first(t.get("\xa9ART"))
        info["title"] = first(t.get("\xa9nam"))
        info["album"] = first(t.get("\xa9alb"))
        info["lyrics"] = first(t.get("\xa9lyr"))
    elif isinstance(f, FLAC):
        t = f.tags or {}
        info["artist"] = first(t.get("artist"))
        info["title"] = first(t.get("title"))
        info["album"] = first(t.get("album"))
        info["lyrics"] = first(t.get("lyrics")) or first(t.get("unsyncedlyrics"))
    fill_from_filename(path, info)
    return info


def fill_from_filename(path, info):
    """Fallback for untagged files: 'Artist/Album/01. Artist - Title.ext'."""
    if not info["title"]:
        stem = path.stem
        stem = re.sub(r"^\s*\d+([.\-]\d+)?[.\-\s]+", "", stem)  # "01. ", "1-04 "
        stem = re.sub(r"^.+?\s+-\s+", "", stem)  # "Artist - "
        info["title"] = stem.strip()
    parent = path.parent
    if re.match(r"^(cd|disc)\s*\d+$", parent.name, re.I):
        parent = parent.parent
    if not info["album"]:
        info["album"] = parent.name
    if not info["artist"]:
        info["artist"] = parent.parent.name


def write_lyrics(path, lyrics):
    f = mutagen.File(path)
    if isinstance(f, MP3):
        if f.tags is None:
            f.add_tags()
        f.tags.delall("USLT")
        f.tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        # iPods do not read ID3v2.4; convert frames and save as v2.3.
        f.tags.update_to_v23()
        f.save(v2_version=3)
    elif isinstance(f, MP4):
        f["\xa9lyr"] = [lyrics]
        f.save()
    elif isinstance(f, FLAC):
        f["LYRICS"] = [lyrics]
        f.save()
    else:
        raise RuntimeError(f"Unsupported file type: {path}")


# --- LRCLIB ----------------------------------------------------------------


def clean_title(title):
    """Drop noise that hurts fuzzy search: (feat. X), - Remastered 2009, etc."""
    t = re.sub(r"\s*[\(\[][^)\]]*(feat\.?|ft\.?|remaster|mono|stereo|edit|version|mix)[^)\]]*[\)\]]", "", title, flags=re.I)
    t = re.sub(r"\s+-\s+(remaster|mono|stereo|live|single|album|radio|\d{4}).*$", "", t, flags=re.I)
    return t.strip() or title


def api_get(artist, title, album, duration):
    r = session.get(
        f"{API}/get",
        params={"artist_name": artist, "track_name": title, "album_name": album, "duration": duration},
        timeout=20,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def api_search(artist, title):
    r = session.get(f"{API}/search", params={"track_name": title, "artist_name": artist}, timeout=20)
    r.raise_for_status()
    return r.json()


def has_text(rec):
    return bool(rec and (rec.get("plainLyrics") or rec.get("instrumental")))


def lookup(artist, title, album, duration):
    """Return (record, how) where how describes the match quality."""
    rec = api_get(artist, title, album, duration)
    if has_text(rec):
        return rec, "exact"
    results = api_search(artist, clean_title(title))
    if not results and clean_title(title) != title:
        results = api_search(artist, title)
    results = [r for r in results if has_text(r)]
    if not results:
        return None, "none"
    # Prefer the candidate whose duration is closest to ours.
    results.sort(key=lambda r: abs((r.get("duration") or 0) - duration))
    best = results[0]
    diff = abs((best.get("duration") or 0) - duration)
    return best, ("close" if diff <= 5 else f"fuzzy ({diff:.0f}s duration diff)")


# --- interactive loop ------------------------------------------------------


def load_skips():
    if SKIP_FILE.exists():
        return set(json.loads(SKIP_FILE.read_text()))
    return set()


def save_skips(skips):
    SKIP_FILE.write_text(json.dumps(sorted(skips), indent=1, ensure_ascii=False))


def show_header(path, info, rec, how):
    print()
    print(bold("=" * min(shutil.get_terminal_size().columns, 100)))
    print(bold(f"{info['artist']} - {info['title']}"), dim(f"[{info['album']}] {info['duration']}s"))
    print(dim(str(path)))
    if rec is None:
        print(red("  no lyrics found"))
        return
    color = green if how in ("exact", "close") else yellow
    print(color(f"  match ({how}): {rec['artistName']} - {rec['trackName']} [{rec['albumName']}] {rec['duration']:.0f}s"),
          dim(f"lrclib id {rec['id']}"), dim("synced" if rec.get("syncedLyrics") else "plain only"))
    if rec.get("instrumental"):
        print(yellow("  marked as INSTRUMENTAL (nothing to write)"))


def show_lyrics(text, full):
    lines = text.splitlines()
    shown = lines if full else lines[:PREVIEW_LINES]
    print(dim("  " + "-" * 40))
    for ln in shown:
        print("  " + ln)
    if not full and len(lines) > PREVIEW_LINES:
        print(dim(f"  ... ({len(lines) - PREVIEW_LINES} more lines, 'v' to view all)"))
    print(dim("  " + "-" * 40))


def ask(prompt):
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def process(path, args, skips):
    """Return 'quit' to stop, anything else to continue."""
    info = read_info(path)
    if info is None:
        print(red(f"skip (unreadable): {path}"))
        return
    if info["lyrics"] and not args.force:
        if args.verbose:
            print(dim(f"skip (has lyrics): {path}"))
        return
    key = str(path.resolve())
    if key in skips and not args.include_skipped:
        if args.verbose:
            print(dim(f"skip (previously rejected): {path}"))
        return

    artist, title, album = info["artist"], info["title"], info["album"]
    while True:
        try:
            rec, how = lookup(artist, title, album, info["duration"])
        except requests.RequestException as e:
            print(red(f"  request failed: {e}"))
            rec, how = None, "error"
        show_header(path, info, rec, how)
        if rec is None or rec.get("instrumental"):
            if args.yes or (args.auto_exact and how == "exact"):
                # A network error is not a rejection: leave the song for the next run.
                if how != "error":
                    skips.add(key)
                return
            ans = ask("  [e]dit search / [r]etry / [s]kip / [q]uit > ")
            if ans == "q":
                return "quit"
            if ans == "e":
                artist, title = edit_query(artist, title)
                continue
            if ans == "r":
                continue
            skips.add(key)
            return

        text = rec["plainLyrics"].strip() + "\n"
        auto = (args.yes and how in ("exact", "close")) or (args.auto_exact and how == "exact")
        if auto:
            write_lyrics(path, text)
            print(green("  written (auto)"))
            return
        full = False
        show_lyrics(text, full)
        while True:
            ans = ask("  [y]es write / [n]o / [v]iew all / [e]dit search / [q]uit > ")
            if ans in ("y", "yes"):
                write_lyrics(path, text)
                skips.discard(key)
                print(green("  written"))
                return
            if ans in ("n", "no", "s"):
                skips.add(key)
                return
            if ans == "v":
                show_lyrics(text, True)
                continue
            if ans == "e":
                artist, title = edit_query(artist, title)
                break
            if ans == "q":
                return "quit"


def edit_query(artist, title):
    a = input(f"  artist [{artist}]: ").strip() or artist
    t = input(f"  title  [{title}]: ").strip() or title
    return a, t


def normalize_newlines(text):
    """Rewrite CR / CRLF as LF.

    Music.app writes CR-only line endings when lyrics are pasted into it, and the
    iPod only breaks lines on LF, so the lyrics show as one paragraph."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def fix_id3_line_endings(tags):
    """Normalize newlines in every USLT frame. Return True if any frame changed."""
    changed = False
    for fr in tags.getall("USLT"):
        text = normalize_newlines(fr.text)
        if text != fr.text:
            fr.text = text
            changed = True
    return changed


def fix_mp3(path):
    """Re-save as clean ID3v2.3 (UTF-16/Latin-1 frames, no v2.4-only frames, LF lyrics)."""
    f = MP3(path)
    if f.tags is None:
        return False
    bad = f.tags.version[:2] != (2, 3) or any(getattr(fr, "encoding", 0) > 1 for fr in f.tags.values())
    bad = fix_id3_line_endings(f.tags) or bad
    if not bad:
        return False
    f.tags.update_to_v23()
    f.save(v2_version=3)
    return True


def fix_m4a(path):
    """Normalize newlines in the lyrics atom."""
    f = MP4(path)
    if not f.tags or "\xa9lyr" not in f.tags:
        return False
    old = list(f.tags["\xa9lyr"])
    new = [normalize_newlines(t) for t in old]
    if new == old:
        return False
    f.tags["\xa9lyr"] = new
    f.save()
    return True


def fix_tags(files):
    """Repair tags for the iPod. Only files that need a change are re-saved."""
    fixers = {".mp3": fix_mp3, ".m4a": fix_m4a}
    fixed = 0
    for path in files:
        fix = fixers.get(path.suffix.lower())
        if fix and fix(path):
            fixed += 1
            print(green(f"fixed: {path}"))
    print(dim(f"{fixed} files re-saved"))


def iter_audio(paths):
    for p in paths:
        p = Path(p)
        if p.is_file():
            if p.suffix.lower() in AUDIO_EXTS:
                yield p
        elif p.is_dir():
            yield from sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in AUDIO_EXTS)
        else:
            print(red(f"not found: {p}"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=["."], help="files or folders (default: current dir)")
    ap.add_argument("-f", "--force", action="store_true", help="also process files that already have lyrics")
    ap.add_argument("-y", "--yes", action="store_true", help="no prompts: write exact/close matches, skip the rest")
    ap.add_argument("-a", "--auto-exact", action="store_true", help="write exact matches without asking, prompt for the rest")
    ap.add_argument("--include-skipped", action="store_true", help="ask again about previously rejected songs")
    ap.add_argument("-v", "--verbose", action="store_true", help="print skipped files")
    ap.add_argument("--fix-tags", action="store_true", help="only repair tags for iPod (ID3v2.3 for MP3, LF lyrics for MP3/M4A), no lyrics lookup")
    args = ap.parse_args()

    files = list(iter_audio(args.paths))
    print(dim(f"{len(files)} audio files"))
    if args.fix_tags:
        fix_tags(files)
        return

    skips = load_skips()
    try:
        for path in files:
            if process(path, args, skips) == "quit":
                break
    except KeyboardInterrupt:
        print()
    finally:
        save_skips(skips)


if __name__ == "__main__":
    main()
