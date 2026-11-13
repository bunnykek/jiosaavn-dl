#!/usr/bin/env python3
"""Download and tag JioSaavn tracks, albums, and playlists."""

import argparse
from html import unescape
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sanitize_filename import sanitize
from mutagen import MutagenError
from mutagen.mp4 import MP4, MP4Cover

API_URL = "https://www.jiosaavn.com/api.php"
COLLECTION_WORKERS = 4
__version__ = "0.3.0"
SITE_HOSTS = frozenset(("jiosaavn.com", "www.jiosaavn.com", "m.jiosaavn.com",
                        "saavn.com", "www.saavn.com", "m.saavn.com"))
SHORT_HOSTS = frozenset(("saavn.app.link", "jiosaavn.app.link"))


class DownloadError(Exception):
    """An actionable download or metadata error."""


def config_path():
    if os.environ.get("XDG_CONFIG_HOME"):
        root = Path(os.environ["XDG_CONFIG_HOME"]).expanduser()
    elif sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path.home() / ".config"
    return root / "jiosaavn-dl/config.json"


def saved_output():
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("output"), str) or not data["output"].strip():
            raise ValueError("missing output folder")
        return data["output"]
    except FileNotFoundError:
        return "Downloads"
    except (OSError, ValueError) as exc:
        raise DownloadError(f"Cannot read settings at {path}: {exc}. Use --set-output to save a folder again, or -o for this download.") from exc


def save_output(folder):
    if not folder.strip():
        raise DownloadError("The output folder cannot be empty.")
    destination = Path(folder).expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise DownloadError(f"The output path is a file, not a folder: {destination}")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="config-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"output": str(destination)}, handle, indent=2)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return str(destination)


def clean_text(value):
    return unescape(str(value or ""))


def safe_name(value, fallback="Unknown"):
    # Keep each component portable, bounded, and inside the output directory.
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", clean_text(value))
    value = sanitize(value).strip(" .")[:160].rstrip(" .")
    return value or fallback


def normalized_url(url):
    url = unescape(url.strip())
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname not in SITE_HOSTS | SHORT_HOSTS:
        raise DownloadError("Use a JioSaavn song, album, or playlist URL.")
    if parts.username or parts.password or parts.port not in (None, 80, 443):
        raise DownloadError("The URL contains unsupported credentials or a port.")
    return url


def parse_url(url):
    """Return (kind, token) for supported public JioSaavn URLs."""
    parts = urlsplit(normalized_url(url))
    segments = parts.path.strip("/").split("/")
    if len(segments) >= 3 and segments[0] in ("song", "album", "playlist", "featured"):
        kind = "playlist" if segments[0] == "featured" else segments[0]
    elif len(segments) >= 4 and segments[0] in ("s", "p") and segments[1] in ("song", "album", "playlist", "featured"):
        kind = "playlist" if segments[1] == "featured" else segments[1]
    else:
        raise DownloadError("Unsupported URL. Expected a song, album, or playlist link.")
    token = unquote(segments[-1])
    if not token or any(character in token for character in "/\\?#\r\n\x00") or token in (".", ".."):
        raise DownloadError("The URL has an invalid item token.")
    return kind, token


def resolve_url(url, client):
    url = normalized_url(url)
    if urlsplit(url).hostname not in SHORT_HOSTS:
        return parse_url(url)
    # App sharing links require a redirect to reveal the collection type/token.
    # Follow only recognized hosts, with a bound independent of Session defaults.
    for _ in range(6):
        with client.request(url, stream=True, allow_redirects=False) as response:
            if response.status_code not in (301, 302, 303, 307, 308):
                raise DownloadError("This share link did not redirect to a song, album, or playlist. Copy its full website URL instead.")
            location = response.headers.get("Location")
            if not location:
                raise DownloadError("The share link returned no destination URL.")
            url = normalized_url(urljoin(url, location))
        if urlsplit(url).hostname in SITE_HOSTS:
            return parse_url(url)
    raise DownloadError("Too many redirects in the share link. Copy its full website URL instead.")


def input_urls(urls, input_files):
    """Read positional URLs followed by newline-delimited URL lists."""
    yield from urls
    for filename in input_files:
        try:
            if filename == "-":
                lines = sys.stdin.read().splitlines()
            else:
                lines = Path(filename).expanduser().read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            raise DownloadError(f"Cannot read URL list {filename}: {exc}") from exc
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def is_true(value):
    return str(value).lower() in ("1", "true", "yes")


class Jiosaavn:
    def __init__(self, output="Downloads", quality="320", timeout=30,
                 overwrite=False, lyrics=True, cover=True, session=None):
        self.output = Path(output).expanduser()
        self.quality = str(quality)
        self.timeout = timeout
        self.overwrite = overwrite
        self.lyrics = lyrics
        self.cover = cover
        self._provided_session = session is not None
        self.session = session if session is not None else self._new_session()
        self._worker_local = threading.local()
        self._worker_sessions = []
        self._worker_sessions_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        if self._provided_session:
            self._configure_session(self.session)

        self.downloaded = 0
        self.skipped = 0
        self.failed = 0

    @staticmethod
    def _configure_session(session):
        retries = Retry(total=3, backoff_factor=0.5,
                        status_forcelist=(429, 500, 502, 503, 504),
                        allowed_methods=frozenset(("GET",)))
        session.mount("https://", HTTPAdapter(max_retries=retries))

    def _new_session(self):
        session = requests.Session()
        self._configure_session(session)
        return session

    def _increment_counter(self, name):
        with self._counter_lock:
            setattr(self, name, getattr(self, name) + 1)

    def _request_session(self):
        return getattr(self._worker_local, "session", self.session)

    def request(self, url, **kwargs):
        try:
            response = self._request_session().get(url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            raise DownloadError(f"Request failed: {exc}") from exc

    def api(self, call, **params):
        response = self.request(API_URL, params={
            "__call": call, "_format": "json", "_marker": "0",
            "ctx": "web6dot0", **params,
        })
        try:
            # Older webapi responses may wrap their JSON in a callback.
            text = response.text.strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start < 0 or end < start:
                    raise ValueError("No JSON object in response")
                data = json.loads(text[start:end + 1])
        except (ValueError, TypeError) as exc:
            raise DownloadError("JioSaavn returned an unreadable response. Try again later.") from exc
        if not isinstance(data, dict) or not data:
            raise DownloadError("JioSaavn returned no data for this item.")
        if data.get("error"):
            raise DownloadError(f"JioSaavn: {data['error']}")
        return data

    def metadata(self, kind, token):
        return self.api("webapi.get", token=token, type=kind, n=1000)

    def getCdnURL(self, encurl):
        data = self.api("song.generateAuthToken", url=encurl,
                        bitrate=self.quality, api_version="4")
        url = data.get("auth_url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise DownloadError("No audio URL is available for this track.")
        # The web endpoint sometimes returns its web stream instead of AAC.
        parts = urlsplit(url)
        host = parts.netloc.replace("web", "aac", 1)
        return parts._replace(netloc=host).geturl()

    def download_file(self, url, path):
        """Stream to a temporary file; leave the destination intact on failure."""
        temp = path.with_name(path.name + ".part")
        try:
            with self.request(url, stream=True) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/" in content_type or "json" in content_type:
                    raise DownloadError("The server returned an error page instead of media.")
                size = 0
                with temp.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=128 * 1024):
                        if chunk:
                            handle.write(chunk)
                            size += len(chunk)
                if not size:
                    raise DownloadError("The server returned an empty file.")
                expected = response.headers.get("Content-Length")
                if expected and expected.isdigit() and not response.headers.get("Content-Encoding"):
                    if size != int(expected):
                        raise DownloadError("Download was incomplete. Please retry.")
            return temp
        except requests.RequestException as exc:
            temp.unlink(missing_ok=True)
            raise DownloadError(f"Download interrupted: {exc}") from exc
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def tagger(self, song, song_path, album_artist=None, pos=1, total=1, artwork=None):
        audio = MP4(song_path)
        for atom, key in (("\xa9nam", "song"), ("\xa9alb", "album"),
                          ("\xa9ART", "primary_artists"), ("\xa9wrt", "music"),
                          ("\xa9day", "release_date"), ("cprt", "copyright_text")):
            value = clean_text(song.get(key))
            if value:
                audio[atom] = value
        audio["aART"] = clean_text(album_artist or song.get("primary_artists"))
        audio["trkn"] = [(pos, total)]
        audio["rtng"] = [4 if is_true(song.get("explicit_content")) else 2]
        for label, key in (("Record label", "label"), ("Language", "language"),
                           ("Featured artists", "featured_artists"),
                           ("Singers", "singers"), ("Starring", "starring")):
            value = clean_text(song.get(key))
            if value:
                audio[f"----:com.apple.iTunes:{label}"] = value.encode("utf-8")
        if self.lyrics and is_true(song.get("has_lyrics")) and song.get("id"):
            try:
                lyrics = self.api("lyrics.getLyrics", lyrics_id=song["id"], api_version="4")
                if lyrics.get("lyrics"):
                    audio["\xa9lyr"] = clean_text(re.sub(r"<br\s*/?>", "\n", lyrics["lyrics"]))
            except DownloadError as exc:
                print(f"  Warning: lyrics unavailable: {exc}", file=sys.stderr)
        if artwork:
            image_format = MP4Cover.FORMAT_PNG if artwork.startswith(b"\x89PNG") else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(artwork, imageformat=image_format)]
        audio.pop("\xa9too", None)
        audio.save()

    def processTrack(self, song_id, album_artist=None, song_pos=1, total_tracks=1,
                     isPlaylist=False):
        metadata = self.metadata("song", song_id)
        if "song" in metadata:
            song = metadata
        else:
            candidates = metadata.get("songs")
            if not isinstance(candidates, list):
                candidates = metadata.values()
            song = next((value for value in candidates
                         if isinstance(value, dict) and "song" in value), None)
        if song is None:
            raise DownloadError("Track metadata is missing or has an unsupported format.")
        title = clean_text(song.get("song")) or song_id
        artists = clean_text(album_artist or song.get("primary_artists")) or "Unknown Artist"
        if isPlaylist:
            folder = safe_name(isPlaylist)
        else:
            artist = "Various Artists" if artists.count(",") >= 2 else artists
            folder = safe_name(f"{clean_text(song.get('album')) or 'Unknown Album'} - {artist} [{song.get('year') or 'Unknown'}]")
        directory = self.output / folder
        destination = directory / f"{song_pos:02d}. {safe_name(title)}.m4a"
        print(f"[{song_pos}/{total_tracks}] {title} — {artists}", flush=True)
        if destination.exists() and destination.stat().st_size and not self.overwrite:
            try:
                MP4(destination)
            except (MutagenError, ValueError):
                print("  Existing file is invalid; downloading again.")
            else:
                self._increment_counter("skipped")
                print("  Skipped (already exists; use --overwrite to replace).")
                return
        encrypted = song.get("encrypted_media_url")
        if not encrypted:
            raise DownloadError("Audio is unavailable for this track (it may be restricted in your region).")
        directory.mkdir(parents=True, exist_ok=True)
        audio_url = self.getCdnURL(encrypted)
        artwork = None
        if self.cover and song.get("image"):
            try:
                cover_url = song["image"].replace("150x150", "500x500")
                with self.request(cover_url) as response:
                    artwork = response.content
                if not artwork.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
                    raise DownloadError("Invalid cover image.")
            except DownloadError as exc:
                artwork = None
                print(f"  Warning: cover unavailable: {exc}", file=sys.stderr)
        temp = self.download_file(audio_url, destination)
        try:
            self.tagger(song, temp, album_artist, song_pos, total_tracks, artwork)
            temp.replace(destination)
        finally:
            temp.unlink(missing_ok=True)
        self._increment_counter("downloaded")
        print(f"  Saved: {destination}", flush=True)

    def _process_collection_track(self, kind, data, name, pos, total, song):
        try:
            if not isinstance(song, dict) or not song.get("perma_url"):
                raise DownloadError("Track is missing its URL.")
            track_kind, track_token = parse_url(song["perma_url"])
            if track_kind != "song":
                raise DownloadError("Collection item is not a song URL.")
            if not self._provided_session and not hasattr(self._worker_local, "session"):
                worker_session = self._new_session()
                self._worker_local.session = worker_session
                with self._worker_sessions_lock:
                    self._worker_sessions.append(worker_session)
            self.processTrack(track_token,
                              data.get("primary_artists") if kind == "album" else None,
                              pos, total, f"Playlist - {name}" if kind == "playlist" else False)
        except (DownloadError, OSError, MutagenError, ValueError, TypeError, KeyError) as exc:
            self._increment_counter("failed")
            print(f"  Error on track {pos}: {exc}", file=sys.stderr)

    def process_collection(self, kind, token):
        data = self.metadata(kind, token)
        songs = data.get("songs")
        if not isinstance(songs, list) or not songs:
            raise DownloadError("This collection has no available tracks.")
        name = clean_text(data.get("title") or data.get("listname")) or "Untitled"
        print(f"{kind.title()}: {name} ({len(songs)} tracks)")
        try:
            reported_total = int(data.get("list_count", len(songs)))
        except (ValueError, TypeError):
            reported_total = len(songs)
        if reported_total > len(songs):
            print(f"Warning: JioSaavn returned {len(songs)} of {reported_total} tracks.", file=sys.stderr)
        try:
            with ThreadPoolExecutor(max_workers=min(COLLECTION_WORKERS, len(songs))) as executor:
                list(executor.map(
                    lambda item: self._process_collection_track(
                        kind, data, name, item[0], len(songs), item[1]),
                    enumerate(songs, 1)))
        finally:
            with self._worker_sessions_lock:
                worker_sessions, self._worker_sessions = self._worker_sessions, []
            for worker_session in worker_sessions:
                worker_session.close()

    def processAlbum(self, album_id):
        self.process_collection("album", album_id)

    def processPlaylist(self, playlist_id):
        self.process_collection("playlist", playlist_id)


def positive_timeout(value):
    import math
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number")
    return number


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jiosaavn-dl",
        description="Download JioSaavn songs, albums, and playlists with metadata and artwork.",
        epilog="Example: jiosaavn-dl 'https://www.jiosaavn.com/album/album-name/token' -o ~/Music",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("urls", nargs="*", metavar="URL", help="song, album, or playlist URLs (can be mixed)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-i", "--input-file", action="append", default=[], metavar="FILE",
                        help="read one URL per line; repeat for multiple files, or use - for stdin")
    parser.add_argument("-o", "--output", metavar="PATH",
                        help="folder for this run; otherwise use the saved folder or ./Downloads")
    parser.add_argument("--set-output", metavar="PATH", help="save a default download folder for future runs")
    parser.add_argument("--show-output", action="store_true", help="show the effective download folder")
    parser.add_argument("-q", "--quality", choices=("12", "48", "96", "160", "320"), default="320",
                        help="requested bitrate in kbps (availability depends on the track)")
    parser.add_argument("--timeout", type=positive_timeout, default=30, help="timeout per network request in seconds")
    parser.add_argument("--overwrite", action="store_true", help="replace existing tracks")
    parser.add_argument("--no-lyrics", action="store_true", help="skip lyrics lookup")
    parser.add_argument("--no-cover", action="store_true", help="skip cover download and embedding")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.urls and not args.input_file and args.set_output is None and not args.show_output:
        parser.error("provide at least one URL or --input-file")
    try:
        default_output = save_output(args.set_output) if args.set_output is not None else None
        if default_output is not None:
            print(f"Default download folder saved: {default_output}")
        output = args.output if args.output is not None else (default_output or saved_output())
        if not output.strip():
            raise DownloadError("The output folder cannot be empty.")
        if args.show_output:
            print(Path(output).expanduser().resolve())
    except (DownloadError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not args.urls and not args.input_file:
        return 0
    client = Jiosaavn(output, args.quality, args.timeout, args.overwrite,
                      not args.no_lyrics, not args.no_cover)
    try:
        found_url = False
        for url in input_urls(args.urls, args.input_file):
            found_url = True
            try:
                kind, token = resolve_url(url, client)
                if kind == "song":
                    client.processTrack(token)
                else:
                    client.process_collection(kind, token)
            except (DownloadError, OSError, MutagenError, ValueError, TypeError, KeyError) as exc:
                client.failed += 1
                print(f"Error: {url}: {exc}", file=sys.stderr)
        if not found_url:
            raise DownloadError("The input lists contain no URLs.")
    except DownloadError as exc:
        client.failed += 1
        print(f"Error: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nInterrupted. Run the command again to retry unfinished tracks.", file=sys.stderr)
        return 130
    finally:
        client.session.close()
    print(f"\nFinished: {client.downloaded} downloaded, {client.skipped} skipped, {client.failed} failed.")
    return 1 if client.failed else 0


if __name__ == "__main__":
    sys.exit(main())
