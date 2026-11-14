![jiosaavn-dl](assets/logo.svg)

# jiosaavn-dl

A Python CLI for downloading JioSaavn songs, albums, and playlists as AAC audio in an M4A container. Tracks include available metadata, lyrics, and embedded artwork.

## Install

Requires Python 3.9 or newer. FFmpeg is **not** required.

From this project directory, install with pipx to make the command available from any directory:

```sh
pipx install .
jiosaavn-dl --help
```

If pipx is not installed, install it with your operating system's package manager or `python3 -m pip install --user pipx`, then run `python3 -m pipx ensurepath` and reopen your terminal. On macOS with Homebrew, use `brew install pipx` followed by `pipx ensurepath`.

To get the source first:

```sh
git clone https://github.com/Pratikkk/jiosaavn-dl.git
cd jiosaavn-dl
pipx install .
```

The install command uses your local checkout. These changes must be pushed before a fresh clone includes them. The package is not published to PyPI.

### Install without pipx

Use a virtual environment on macOS/Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
jiosaavn-dl --help
```

On Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install .
jiosaavn-dl --help
```

For Command Prompt, activate with `.venv\Scripts\activate.bat`. Activate the environment again in new terminals, or run `.venv/bin/jiosaavn-dl` (Windows: `.venv\Scripts\jiosaavn-dl.exe`) directly.

To update a pipx installation after pulling new code, run `pipx install --force .`. For a virtual environment installation, rerun `python -m pip install .`. Uninstall with `pipx uninstall jiosaavn-dl` or `python -m pip uninstall jiosaavn-dl`, respectively.

## Usage

Pass a full song, album, or playlist URL. Quote URLs, especially those containing `&` or `?`.

```sh
jiosaavn-dl 'https://www.jiosaavn.com/album/o-saki-saki-from-batla-house/og4DkAosip0_'
jiosaavn-dl 'URL' -o ~/Music/JioSaavn
jiosaavn-dl 'URL_1' 'URL_2' --quality 160
jiosaavn-dl --input-file urls.txt -o ~/Music/JioSaavn
jiosaavn-dl 'URL' --no-lyrics --no-cover
jiosaavn-dl 'URL' --dry-run
jiosaavn-dl --help
```

### Choose your music folder

Save a default download folder once, for example a folder inside your music library:

```sh
jiosaavn-dl --set-output ~/Music/JioSaavn
jiosaavn-dl 'URL'
```

Future downloads go into that folder, even when you run the command from another directory. Use quotes for folder names containing spaces, such as `--set-output "$HOME/Music/My Library"`. On Windows, use `--set-output "C:\Users\YourName\Music\JioSaavn"`.

For a single download, choose a different location with `jiosaavn-dl 'URL' -o /path/to/folder`. This does not change your saved default. Run `jiosaavn-dl --show-output` to see the current destination. New download folders are created when tracks are saved, with album and playlist subfolders inside them.

Settings are saved in `~/Library/Application Support/jiosaavn-dl/config.json` on macOS, `%APPDATA%\jiosaavn-dl\config.json` on Windows, or `~/.config/jiosaavn-dl/config.json` on Linux. `XDG_CONFIG_HOME`, when set, overrides the configuration directory. Without a saved setting, downloads still go to `./Downloads`. Choosing a folder saves audio files there; it does not automatically import them into a music player.

Replace `URL`, `URL_1`, and `URL_2` with actual JioSaavn links. You can mix songs, albums, and playlists in one command.

Supported link forms include:

- Individual songs: `/song/NAME/TOKEN`, older `/s/song/LANGUAGE/NAME/TOKEN` links, and `/p/song/LANGUAGE/NAME/TOKEN` links.
- Albums: `/album/NAME/TOKEN`, older `/s/album/LANGUAGE/NAME/TOKEN` links, and `/p/album/LANGUAGE/NAME/TOKEN` links.
- Playlists: `/s/playlist/USER/NAME/TOKEN`, `/p/playlist/USER/NAME/TOKEN`, `/playlist/NAME/TOKEN`, and featured lists at `/featured/NAME/TOKEN`, `/s/featured/LANGUAGE/NAME/TOKEN`, or `/p/featured/LANGUAGE/NAME/TOKEN`.
- Desktop and mobile JioSaavn hosts, older `saavn.com` hosts, HTTP/HTTPS, and links pasted without `https://`.
- Shared links with query parameters, fragments, or trailing slashes.
- `saavn.app.link` and `jiosaavn.app.link` share links that redirect to a supported website URL. If a link only opens an app or landing page, copy the full song/album/playlist URL from the website instead.

For a list of songs or mixed collections, create `urls.txt` with one URL per line:

```text
# Each album or playlist downloads its tracks
https://www.jiosaavn.com/song/NAME/SONG_TOKEN
https://www.jiosaavn.com/album/NAME/ALBUM_TOKEN
https://www.jiosaavn.com/featured/NAME/PLAYLIST_TOKEN
```

Blank lines and lines starting with `#` are ignored. Repeat `-i` to read multiple files, combine it with positional URLs, or use `-i -` to read from standard input. Artist, podcast, search, and homepage links are not supported as music collections.

| Option | Behavior |
| --- | --- |
| `-i`, `--input-file FILE` | Read URLs from a text file; use `-` for standard input |
| `-o`, `--output PATH` | Folder for this run; overrides the saved default |
| `--set-output PATH` | Save a default download folder; can run without a URL |
| `--show-output` | Show the effective download folder; can run without a URL |
| `-q`, `--quality KBPS` | Request `12`, `48`, `96`, `160`, or `320` kbps; default `320` |
| `--timeout SECONDS` | Positive timeout per network request; default `30` |
| `--overwrite` | Replace existing tracks after a successful download and tagging |
| `--no-lyrics` | Skip lyrics lookup |
| `--no-cover` | Skip artwork download and embedding |
| `--dry-run` | List planned downloads without fetching audio, covers, or lyrics; metadata is still requested |
| `--quiet` | Suppress routine output and progress; errors are still shown |
| `--verbose` | Show per-track details during downloads; cannot be combined with `--quiet` |
| `-h`, `--help` | Show usage |
| `--version` | Show installed version |

The service determines which quality is available. Files keep their original AAC audio; the program does not transcode them.

Album tracks are stored in `ALBUM - ARTIST [YEAR]` folders, and playlists in `Playlist - NAME` folders. Tracks are numbered in collection order. Filenames are sanitized for portability; metadata keeps the original punctuation. Album folders use the artist names returned by JioSaavn instead of guessing that several artists mean “Various Artists.”
Collection tracks download concurrently, with up to four tracks in progress at once. In interactive terminals a progress bar stays on the last line, showing the titles currently downloading, and each finished track is printed above it as downloaded, skipped or failed. When output is piped or redirected, plain per-track lines are printed instead. Playlist pages are fetched when the collection exceeds 1,000 tracks, and track metadata included in those pages is reused.

## Reliability and troubleshooting

- Completed, readable M4A files are skipped by default. The JioSaavn song ID is stored in each downloaded file and used to recognize tracks after playlist reordering. If the same song appears more than once in one playlist, only the first copy is downloaded and the repeats are counted as skipped. Use `--overwrite` to refresh their audio or metadata. Empty or invalid existing files are downloaded again.
- Audio streams into a `.part` file and moves into place only after tagging succeeds. Interrupted downloads keep their partial data and resume with an HTTP range request when supported; if the server ignores the range, the file restarts from the beginning. Partial files are named with the requested quality (for example `.320.part`), so rerunning with a different `--quality` starts that track fresh instead of continuing the other quality's data. Failed downloads do not replace an existing track.
- Transient HTTP errors are retried up to three times. Increase `--timeout` for slow connections.
- Missing optional metadata, lyrics, or artwork does not prevent downloading a track. Artwork is embedded in each track rather than saved as a separate cover file.
- `--dry-run` still requests JioSaavn metadata to identify and list tracks, but does not request audio URLs, audio, artwork, or lyrics and does not create download files.
- Artwork URLs use the `500x500` image variant. A live check found it available and the corresponding `1000x1000` URL unavailable for the sampled cover; larger artwork may depend on the source image.
- A failed track does not stop the rest of an album, playlist, or batch. The final summary counts downloaded, skipped, and failed items. Rerun the command to retry failures.
- Exit codes: `0` for success (including skipped files), `1` if any item failed, `2` for invalid CLI arguments, and `130` when interrupted with Ctrl+C.
- Playlist requests are paginated when the service reports more tracks than the first page contains. If the service returns incomplete or repeated pages, the CLI warns about the remaining tracks.
- Regional restrictions or JioSaavn API changes may make some tracks unavailable. Errors appear in the terminal. If optional lookups fail, try `--no-lyrics --no-cover`.

If the command is not found after a pipx install, run `pipx ensurepath` and reopen your terminal. With a virtual environment, activate it or use the full path to its command. You can also run `python -m jiosaavn` with the Python interpreter where the package is installed.

## Development

Install an editable checkout and run the offline regression suite:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Build a wheel and source distribution with `python -m pip install build` followed by `python -m build`. Runtime dependencies are maintained in `requirements.txt` and loaded by the package configuration.

The reusable API, metadata, download, and tagging implementation lives in `jiosaavn_core.py`; `jiosaavn.py` provides CLI parsing and dispatch. For Python callers, `parse_url(url)` validates and extracts the item kind and token, while `Jiosaavn(output=...)` provides `processTrack(song_id)` and `process_collection(kind, token)` operations with the same download, tagging, and error behavior as the CLI. `config_path()`, `saved_output()`, and `save_output(folder)` expose output-folder settings. GitHub Actions runs the unit suite on Python 3.9, 3.11, and 3.13 for pushes and pull requests.

Tests mock network calls and use a tiny M4A fixture containing generated silence to verify real metadata tagging. They do not require a JioSaavn account or network access. The fixture was created with:

```sh
ffmpeg -f lavfi -i anullsrc=r=44100:cl=mono -t 0.1 -c:a aac -map_metadata -1 tests/fixtures/silence.m4a
```

Use downloads only where you have permission to do so.
