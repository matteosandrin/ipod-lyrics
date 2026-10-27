# ipod-lyrics

Fetch lyrics from [LRCLIB](https://lrclib.net) and write them into the tags of
MP3, M4A and FLAC files, so they show up on an iPod. Each match is shown in the
terminal and you approve it before anything is written.

The script also repairs tags that iPods cannot read: MP3 files are re-saved as
ID3v2.3, and CR line endings in lyrics are rewritten as LF.

## Requirements

- Python 3.9 or newer
- `mutagen` and `requests`, declared in `pyproject.toml`

Install the dependencies and run the script directly:

```bash
pip install mutagen requests
```

Or install the project, which also puts an `ipod-lyrics` command on your path:

```bash
pip install .
```

## Usage

```bash
./lyrics.py                                              # whole library (current dir)
./lyrics.py FLACs/Justice                                # one folder
./lyrics.py "FLACs/Justice/Woman/10. Justice - Close Call.flac"
./lyrics.py -a                                           # auto-write exact matches, ask about the rest
./lyrics.py -y                                           # no prompts at all
./lyrics.py --fix-tags                                   # repair tags only, no lyrics lookup
```

Paths can be files or folders. Folders are scanned recursively for `.mp3`,
`.m4a` and `.flac` files. Files that already have lyrics are skipped unless
you pass `--force`.

### Options

| Flag | Effect |
| --- | --- |
| `-f`, `--force` | Also process files that already have lyrics |
| `-y`, `--yes` | No prompts: write exact and close matches, skip the rest |
| `-a`, `--auto-exact` | Write exact matches without asking, prompt for the rest |
| `--include-skipped` | Ask again about songs you rejected in an earlier run |
| `-v`, `--verbose` | Print every skipped file |
| `--fix-tags` | Only repair tags for the iPod (ID3v2.3 for MP3, LF lyrics for MP3/M4A) |

### Interactive prompts

For each song the script prints the tags, the LRCLIB match and a preview of the
lyrics, then asks:

- `y` write the lyrics to the file
- `n` reject, and remember the rejection
- `v` show the full lyrics
- `e` edit the artist and title and search again
- `q` quit

When nothing is found you can `e`dit the search, `r`etry, `s`kip or `q`uit.

## How matching works

1. Read artist, title, album and duration from the tags. Untagged files fall
   back to the folder layout `Artist/Album/01. Artist - Title.ext`.
2. Ask LRCLIB for an exact match on all four fields.
3. If that fails, run a fuzzy search. Noise such as `(feat. X)` or
   `- Remastered 2009` is stripped from the title first.
4. Among the search results, pick the one whose duration is closest to the
   file. A difference of 5 seconds or less is reported as a close match,
   anything larger as fuzzy.

Only plain lyrics are written. Tracks LRCLIB marks as instrumental are skipped.

## Where lyrics go

| Format | Tag |
| --- | --- |
| MP3 | `USLT` frame, file saved as ID3v2.3 |
| M4A | `©lyr` atom |
| FLAC | `LYRICS` Vorbis comment |

## Skip list

Rejected and not-found songs are recorded in `.lyrics-skip.json` next to the
script, so re-running on the whole library does not ask about them again.
Pass `--include-skipped` to revisit them. Network errors are not recorded as
rejections.

## Notes

- Requests to LRCLIB are retried with backoff on 429 and 5xx responses.
- Colors are only used when stdout is a terminal.
