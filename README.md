# DJ Analyzer Pro

Identify the tracks played in a DJ session recording and write the tracklist
back into the MP3.

`djanalyzerpro.py` detects likely transitions with FFmpeg + NumPy, samples the
audio periodically and around each transition, and identifies each sample with
Shazam (via [ShazamIO](https://github.com/shazamio/ShazamIO)).
`tag_mp3.sh` then embeds the resulting tracklist as ID3 tags.

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe`
- `pip install shazamio numpy`
- For tagging: `jq`, and optionally `mid3v2` (`python3-mutagen`)

## Usage

### 1. Analyze a session

```bash
python3 djanalyzerpro.py /path/to/session.mp3
```

Results are written to `results_dj/` in the current directory, which is
overwritten on each run:

| File | Contents |
|---|---|
| `tracklist.txt` | Human-readable tracklist with approximate start/end times |
| `analisis_completo.json` | Full results: grouped tracklist (times in seconds) and every sample |
| `analisis_completo.csv` | One row per Shazam query |
| `analisis_verbose.log` | Detailed log |

Sampling interval, clip length, detector thresholds and request rate are
constants at the top of `djanalyzerpro.py`.

### 2. Tag the MP3

```bash
./tag_mp3.sh session.mp3 [results_dj/analisis_completo.json] [--no-backup] [--force]
```

This writes ID3v2.4 tags without re-encoding the audio:

- **Chapters** (`CHAP`/`CTOC`): one per track, titled `Artist - Title`
- **Comment** (`COMM`): the tracklist as text. Without `mid3v2` it falls back
  to `TXXX:comment`, which many players don't show.
- `genre=DJ Mix`, and `album_artist=Various Artists` if missing

Existing tags and cover art are kept. It also writes a `session.cue` next to
the MP3.

Safety checks:

- Before changing anything, the script checks that the MP3's duration matches
  the one in the JSON, so you don't tag the wrong file. `--force` skips this
  check.
- It creates `session.mp3.bak` and never overwrites an existing backup.
  `--no-backup` skips the backup.

## Player support

- Chapters and CUE sheets work in foobar2000, VLC/mpv, Mp3tag and podcast apps.
- Navidrome currently reads only the comment, genre and album artist. It
  supports neither chapters nor CUE files.

## Notes

- Track boundaries are estimates: a track's start is the first sample in which
  it was recognized, not the exact mix-in point.
- Shazam may not identify unreleased tracks, edits or heavily layered sections.
