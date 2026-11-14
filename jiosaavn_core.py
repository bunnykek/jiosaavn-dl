#!/usr/bin/env python3
"""Download and tag JioSaavn tracks, albums, and playlists."""

from html import unescape
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NamedTuple, Optional
from urllib.parse import unquote, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sanitize_filename import sanitize
from mutagen import MutagenError
from mutagen.mp4 import MP4, MP4Cover

API_URL = "https://www.jiosaavn.com/api.php"
COLLECTION_PAGE_SIZE = 1000
COLLECTION_WORKERS = 4  # default number of tracks downloaded at once
MAX_WORKERS = 16  # above this, rate limiting from the service is likelier than a speedup
__version__ = "0.4.0"
SITE_HOSTS = frozenset(("jiosaavn.com", "www.jiosaavn.com", "m.jiosaavn.com",
                        "saavn.com", "www.saavn.com", "m.saavn.com"))
SHORT_HOSTS = frozenset(("saavn.app.link", "jiosaavn.app.link"))
# First line of a file written by --failed-file; marks it as safe to replace or remove.
FAILED_FILE_MARKER = "# jiosaavn-dl failed tracks"


class DownloadError(Exception):
    """An actionable download or metadata error."""


class Failure(NamedTuple):
    """One item that could not be downloaded, and how to retry it."""
    label: str
    reason: str
    url: Optional[str] = None


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


def save_failed_urls(path, failures):
    """Write the URLs of failed items to `path` so `-i path` can retry them.

    Returns how many URLs were written. With nothing to retry, a file written
    by an earlier run (it starts with FAILED_FILE_MARKER) is removed so it
    cannot go stale. A file that does not start with the marker is never
    replaced or removed, so pointing this at your own URL list is harmless.
    """
    path = Path(path).expanduser()
    try:
        with path.open(encoding="utf-8-sig") as handle:
            ours = handle.readline().strip() == FAILED_FILE_MARKER
        exists = True
    except FileNotFoundError:
        ours = exists = False
    urls = list(dict.fromkeys(failure.url for failure in failures if failure.url))
    if not urls:
        if exists and ours:
            path.unlink()
        return 0
    if exists and not ours:
        raise DownloadError(f"{path} already exists and was not created by --failed-file; "
                            "choose another file name.")
    lines = [FAILED_FILE_MARKER, f"# Retry with: jiosaavn-dl -i {path.name}", *urls]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(urls)


def display_width(text):
    """Approximate number of terminal columns `text` occupies.

    Wide (CJK) characters count as 2; combining marks and control characters
    count as 0. When unsure it errs on the wide side, so a line that is cut to
    fit never wraps and breaks the progress redraw.
    """
    width = 0
    for char in text:
        if unicodedata.category(char) in ("Mn", "Me", "Cf", "Cc"):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def fit_width(text, width):
    """Cut `text` with an ellipsis so it takes at most `width` columns."""
    width = max(width, 1)
    if display_width(text) <= width:
        return text
    kept, used = [], 0
    for char in text:
        char_width = display_width(char)
        if used + char_width > width - 1:
            break
        kept.append(char)
        used += char_width
    return "".join(kept) + "…"


def is_true(value):
    return str(value).lower() in ("1", "true", "yes")


class Jiosaavn:
    def __init__(self, output="Downloads", quality="320", timeout=30,
                 overwrite=False, lyrics=True, cover=True, session=None,
                 quiet=False, verbose=False, dry_run=False, workers=COLLECTION_WORKERS):
        self.output = Path(output).expanduser()
        self.workers = max(1, min(int(workers), MAX_WORKERS))
        self.quality = str(quality)
        self.timeout = timeout
        self.overwrite = overwrite
        self.lyrics = lyrics
        self.cover = cover
        self.quiet = quiet
        self.verbose = verbose
        self.dry_run = dry_run
        self.progress_enabled = False
        self._progress_lock = threading.Lock()
        self._progress_total = 0
        self._progress_done = 0
        self._progress_active = False  # True while the bar is on screen
        self._active_titles = {}  # {track number: title} shown on the bar line
        self._provided_session = session is not None
        self.session = session if session is not None else self._new_session()
        self._worker_local = threading.local()
        self._worker_sessions = []
        self._worker_sessions_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        # Tagged song IDs per output folder, read once per run (see _folder_index).
        self._index_lock = threading.Lock()
        self._folder_ids = {}
        # Song IDs being downloaded right now, per folder: {directory: {id: path}}.
        self._folder_claims = {}
        if self._provided_session:
            self._configure_session(self.session)

        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.planned = 0
        self.failures = []  # Failure records, in the order they happened

    def _emit(self, message, stream=None):
        """Print a line, keeping the progress bar (if any) on the last line."""
        stream = stream or sys.stdout
        if not self._progress_active:
            print(message, file=stream, flush=True)
            return
        with self._progress_lock:
            sys.stderr.write("\r\033[K")  # erase the bar, print the line, redraw the bar
            print(message, file=stream, flush=True)
            self._write_progress()

    def _status(self, message, verbose_only=False):
        if self.quiet or (verbose_only and not self.verbose):
            return
        self._emit(message)

    def _warning(self, message):
        if not self.quiet:
            self._emit(message, sys.stderr)

    def _start_progress(self, total):
        if not self.progress_enabled or self.quiet:
            return
        with self._progress_lock:
            self._progress_total = total
            self._progress_done = 0
            self._active_titles = {}
            self._progress_active = True
            self._write_progress()

    def _advance_progress(self, pos=None):
        if not self._progress_active:
            return
        with self._progress_lock:
            self._active_titles.pop(pos, None)
            self._progress_done += 1
            self._write_progress()
            if self._progress_done >= self._progress_total:
                sys.stderr.write("\n")
                sys.stderr.flush()
                self._progress_active = False

    def _write_progress(self):
        """Redraw the bar line. The caller must hold self._progress_lock."""
        width = 24
        total = max(self._progress_total, 1)
        filled = int(width * self._progress_done / total)
        bar = "#" * filled + "-" * (width - filled)
        line = f"Downloading [{bar}] {self._progress_done}/{self._progress_total}"
        if self._active_titles:
            line += "  " + ", ".join(f"{pos:02d} {title}"
                                     for pos, title in sorted(self._active_titles.items()))
        columns = shutil.get_terminal_size((80, 24)).columns
        sys.stderr.write("\r\033[K" + fit_width(line, columns - 1))
        sys.stderr.flush()

    def _finish_progress(self):
        if not self._progress_active:
            return
        with self._progress_lock:
            self._active_titles = {}
            if self._progress_done < self._progress_total:
                sys.stderr.write("\n")
                sys.stderr.flush()
            self._progress_active = False

    def _track_started(self, pos, title):
        """Show `title` on the progress line while its track is downloading."""
        if not self._progress_active:
            return
        with self._progress_lock:
            self._active_titles[pos] = " ".join(str(title).split())
            self._write_progress()

    def _track_finished(self, pos, total, title, artists, outcome, announce=True):
        """Drop a track from the progress line and print how it ended."""
        if not self._progress_active:
            return
        with self._progress_lock:
            self._active_titles.pop(pos, None)
        if announce:
            self._emit(f"  [{pos}/{total}] {outcome}: {title} — {artists}")

    def _track_failed(self, pos, total, exc):
        with self._progress_lock:
            title = self._active_titles.pop(pos, None)
        self._emit(f"  [{pos}/{total}] Failed: {title or f'track {pos}'} — {exc}", sys.stderr)

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

    def record_failure(self, label, reason, url=None):
        """Count a failed item and remember it for the final summary."""
        with self._counter_lock:
            self.failed += 1
            self.failures.append(Failure(label, str(reason), url))

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

    def metadata(self, kind, token, page=None):
        params = {"token": token, "type": kind, "n": COLLECTION_PAGE_SIZE}
        if page is not None:
            params["p"] = page
        return self.api("webapi.get", **params)

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

    def partial_path(self, path):
        """Temporary file for an in-progress download of `path`.

        The requested bitrate is part of the name, so an interrupted download
        is only resumed at the same quality; resuming another quality's bytes
        would splice two different files together.
        """
        return path.with_name(f"{path.name}.{self.quality}.part")

    @staticmethod
    def _discard_partials(path):
        """Delete leftover partial downloads of `path` at any quality."""
        prefix = path.name + "."
        for partial in path.parent.glob("*.part"):
            if partial.name.startswith(prefix):
                partial.unlink(missing_ok=True)

    def download_file(self, url, path):
        """Resume a partial stream into a temporary file."""
        temp = self.partial_path(path)
        offset = temp.stat().st_size if temp.exists() else 0
        request_options = {"stream": True}
        if offset:
            request_options["headers"] = {
                "Range": f"bytes={offset}-",
                "Accept-Encoding": "identity",
            }
        try:
            with self.request(url, **request_options) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/" in content_type or "json" in content_type:
                    raise DownloadError("The server returned an error page instead of media.")
                append = offset > 0 and response.status_code == 206
                expected_total = None
                if append:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                    if not match or int(match.group(1)) != offset or int(match.group(2)) < offset:
                        temp.unlink(missing_ok=True)
                        raise DownloadError("The server returned an invalid range response.")
                    if match.group(3) != "*":
                        expected_total = int(match.group(3))
                        if int(match.group(2)) >= expected_total:
                            temp.unlink(missing_ok=True)
                            raise DownloadError("The server returned an invalid range response.")
                    if response.headers.get("Content-Encoding", "").lower() not in ("", "identity"):
                        temp.unlink(missing_ok=True)
                        raise DownloadError("The server encoded a ranged response; retry without resuming.")
                size = 0
                with temp.open("ab" if append else "wb") as handle:
                    for chunk in response.iter_content(chunk_size=128 * 1024):
                        if chunk:
                            handle.write(chunk)
                            size += len(chunk)
                if not size:
                    if not offset:
                        temp.unlink(missing_ok=True)
                    raise DownloadError("The server returned an empty file.")
                expected = response.headers.get("Content-Length")
                if expected and expected.isdigit() and not response.headers.get("Content-Encoding"):
                    if size != int(expected):
                        raise DownloadError("Download was incomplete. Please retry.")
                if expected_total is not None and temp.stat().st_size != expected_total:
                    raise DownloadError("Download was incomplete. Please retry.")
            return temp
        except requests.RequestException as exc:
            raise DownloadError(f"Download interrupted: {exc}") from exc

    def tagger(self, song, song_path, album_artist=None, pos=1, total=1, artwork=None,
               song_id=None):
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
        track_id = song.get("id") or song_id
        if track_id:
            audio["----:com.apple.iTunes:JioSaavn ID"] = clean_text(track_id).encode("utf-8")
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
                self._warning(f"  Warning: lyrics unavailable: {exc}")
        if artwork:
            image_format = MP4Cover.FORMAT_PNG if artwork.startswith(b"\x89PNG") else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(artwork, imageformat=image_format)]
        audio.pop("\xa9too", None)
        audio.save()

    @staticmethod
    def _tagged_song_id(path):
        try:
            tags = MP4(path).get("----:com.apple.iTunes:JioSaavn ID")
        except (MutagenError, OSError, ValueError):
            return None
        if not tags:
            return None
        try:
            return tags[0].decode("utf-8").strip()
        except (AttributeError, UnicodeDecodeError):
            return None

    def _folder_index(self, directory):
        """Map each .m4a in a folder to its tagged song ID.

        Tags are read once per folder per run instead of once per track, which
        keeps large playlists from rescanning the whole folder for every track.
        The caller must hold self._index_lock.
        """
        index = self._folder_ids.get(directory)
        if index is None:
            paths = directory.glob("*.m4a") if directory.is_dir() else ()
            index = {path: self._tagged_song_id(path) for path in paths}
            self._folder_ids[directory] = index
        return index

    def _remember_track(self, directory, path, track_id):
        """Record a saved track and keep its claim for duplicate suppression."""
        with self._index_lock:
            index = self._folder_ids.get(directory)
            if index is not None:
                index[path] = track_id
            claims = self._folder_claims.get(directory)
            if claims is not None and claims.get(track_id) == path:
                del claims[track_id]

    def _release_track(self, directory, path, track_id):
        """Undo a claim from _existing_destination when the download failed."""
        with self._index_lock:
            claims = self._folder_claims.get(directory)
            # Keep successful claims until this client run ends. This prevents
            # duplicate entries later in the same collection from overwriting
            # the same destination a second time.

    def _existing_destination(self, directory, destination, track_id, overwrite=False):
        """Return an existing path or reserve a destination for this song ID.

        Successful claims remain for this client run so repeated collection
        entries are not downloaded or overwritten more than once. Failed and
        dry-run claims are released by the caller.
        """
        with self._index_lock:
            claims = self._folder_claims.setdefault(directory, {})
            if track_id in claims:
                return claims[track_id], True
            index = self._folder_index(directory)
            for existing, existing_id in index.items():
                if existing_id != track_id:
                    continue
                # Confirm with this one file: it may have been deleted or
                # replaced since the folder was indexed.
                current_id = self._tagged_song_id(existing)
                if current_id == track_id:
                    if overwrite:
                        claims[track_id] = existing
                        return existing, False
                    return existing, True
                index[existing] = current_id
            conflict = False
            if destination in index:
                if index[destination] is not None:
                    conflict = True
                else:
                    # An untagged file at the destination counts as a download
                    # only if it is non-empty and readable.
                    try:
                        if destination.stat().st_size:
                            MP4(destination)
                            if overwrite:
                                claims[track_id] = destination
                                return destination, False
                            return destination, True
                    except (MutagenError, OSError, ValueError):
                        pass
            chosen = destination
            if conflict:
                stem = f"{destination.stem} [{safe_name(track_id)}]"
                chosen = destination.with_name(stem + destination.suffix)
                suffix = 2
                while chosen in index or chosen.exists():
                    chosen_id = (index[chosen] if chosen in index
                                 else self._tagged_song_id(chosen))
                    if chosen_id == track_id:
                        if overwrite:
                            claims[track_id] = chosen
                            return chosen, False
                        return chosen, True
                    chosen = destination.with_name(f"{stem} ({suffix}){destination.suffix}")
                    suffix += 1
            claims[track_id] = chosen
            return chosen, False

    def processTrack(self, song_id, album_artist=None, song_pos=1, total_tracks=1,
                     isPlaylist=False, song_data=None):
        if (isinstance(song_data, dict) and "song" in song_data
                and "encrypted_media_url" in song_data):
            song = song_data
        else:
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
            folder = safe_name(f"{clean_text(song.get('album')) or 'Unknown Album'} - {artists} [{song.get('year') or 'Unknown'}]")
        directory = self.output / folder
        destination = directory / f"{song_pos:02d}. {safe_name(title)}.m4a"
        self._worker_local.title = title  # lets a failure name the track
        self._track_started(song_pos, title)
        self._status(f"[{song_pos}/{total_tracks}] {title} — {artists}",
                     verbose_only=self.progress_enabled)
        track_id = clean_text(song.get("id") or song_id)
        self._status(f"  JioSaavn ID: {track_id}", verbose_only=True)
        destination, found = self._existing_destination(
            directory, destination, track_id, overwrite=self.overwrite)
        if found:
            self._increment_counter("skipped")
            self._status("  Skipped (song ID already exists; use --overwrite to replace).",
                         verbose_only=self.progress_enabled)
            self._track_finished(song_pos, total_tracks, title, artists, "Skipped")
            return
        if self.dry_run:
            self._increment_counter("planned")
            details = f"  Would download: {destination}"
            if self.verbose:
                details += f" (JioSaavn ID: {track_id})"
            self._status(details)
            self._track_finished(song_pos, total_tracks, title, artists,
                                 "Would download", announce=False)
            self._release_track(directory, destination, track_id)
            return
        try:
            self._fetch_track(song, song_id, directory, destination, track_id,
                              album_artist, song_pos, total_tracks)
        except BaseException:
            self._release_track(directory, destination, track_id)
            raise
        self._track_finished(song_pos, total_tracks, title, artists, "Downloaded")

    def _fetch_track(self, song, song_id, directory, destination, track_id,
                     album_artist, song_pos, total_tracks):
        """Download, tag and save one track to `destination`."""
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
                self._warning(f"  Warning: cover unavailable: {exc}")
        temp = self.download_file(audio_url, destination)
        try:
            self.tagger(song, temp, album_artist, song_pos, total_tracks, artwork,
                        song_id=song_id)
            temp.replace(destination)
            self._discard_partials(destination)
            self._remember_track(directory, destination, track_id)
        finally:
            temp.unlink(missing_ok=True)
        self._increment_counter("downloaded")
        self._status(f"  Saved: {destination}", verbose_only=self.progress_enabled)

    def _process_collection_track(self, kind, data, name, pos, total, song):
        self._worker_local.title = None
        retry_url = None  # set once the item is known to be a retryable song URL
        try:
            if not isinstance(song, dict) or not song.get("perma_url"):
                raise DownloadError("Track is missing its URL.")
            track_kind, track_token = parse_url(song["perma_url"])
            if track_kind != "song":
                raise DownloadError("Collection item is not a song URL.")
            retry_url = song["perma_url"]
            if (not self.dry_run and not self._provided_session
                    and not hasattr(self._worker_local, "session")):
                worker_session = self._new_session()
                self._worker_local.session = worker_session
                with self._worker_sessions_lock:
                    self._worker_sessions.append(worker_session)
            self.processTrack(track_token,
                              data.get("primary_artists") if kind == "album" else None,
                              pos, total, f"Playlist - {name}" if kind == "playlist" else False,
                              song_data=song)
        except Exception as exc:
            title = self._worker_local.title
            label = f"{title} (track {pos} of {name})" if title else f"Track {pos} of {name}"
            self.record_failure(label, exc, retry_url)
            if self._progress_active:
                self._track_failed(pos, total, exc)
            else:
                print(f"  Error on track {pos}: {exc}", file=sys.stderr)
        finally:
            self._advance_progress(pos)

    def process_collection(self, kind, token):
        data = self.metadata(kind, token)
        songs = data.get("songs")
        if not isinstance(songs, list) or not songs:
            raise DownloadError("This collection has no available tracks.")
        name = clean_text(data.get("title") or data.get("listname")) or "Untitled"
        try:
            reported_total = int(data.get("list_count", len(songs)))
        except (ValueError, TypeError):
            reported_total = len(songs)
        if kind == "playlist" and reported_total > len(songs):
            page_size = len(songs)
            max_pages = (reported_total + page_size - 1) // page_size

            def page_signature(items):
                return tuple(
                    (item.get("id") or item.get("perma_url") or repr(item))
                    if isinstance(item, dict) else repr(item)
                    for item in items)

            page_signatures = {
                page_signature(songs)
            }
            for page in range(2, max_pages + 1):
                page_data = self.metadata(kind, token, page=page)
                page_songs = page_data.get("songs")
                if not isinstance(page_songs, list) or not page_songs:
                    break
                signature = page_signature(page_songs)
                if signature in page_signatures:
                    break
                page_signatures.add(signature)
                songs.extend(page_songs)
                if len(songs) >= reported_total:
                    break
        self._status(f"{kind.title()}: {name} ({len(songs)} tracks)")
        if reported_total > len(songs):
            self._warning(f"Warning: JioSaavn returned {len(songs)} of {reported_total} tracks.")
        try:
            self._start_progress(len(songs))
            executor = ThreadPoolExecutor(max_workers=min(self.workers, len(songs)))
            futures = [
                executor.submit(self._process_collection_track,
                                kind, data, name, pos, len(songs), song)
                for pos, song in enumerate(songs, 1)
            ]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                # Cancel queued tracks on interruption or an unexpected worker error.
                # Running requests finish within their configured timeout before
                # their sessions are closed in the outer finally block.
                for future in futures:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
        finally:
            self._finish_progress()
            with self._worker_sessions_lock:
                worker_sessions, self._worker_sessions = self._worker_sessions, []
            for worker_session in worker_sessions:
                worker_session.close()

    def processAlbum(self, album_id):
        self.process_collection("album", album_id)

    def processPlaylist(self, playlist_id):
        self.process_collection("playlist", playlist_id)
