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
import time
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


class _RetryWithRetryAfter(Retry):
    """Retry class that respects Retry-After header for 429 responses."""
    def increment(self, method, url, response=None, _pool=None, _stacktrace=None):
        # Check for Retry-After on 429
        if response is not None and response.status == 429:
            retry_after = self.get_retry_after(response)
            if retry_after is not None:
                # Create a new Retry with the specific backoff
                new_retry = self.new(
                    total=self.total - 1,
                    backoff_factor=retry_after,
                    status_forcelist=self.status_forcelist,
                    allowed_methods=self.allowed_methods,
                )
                return new_retry.increment(method, url, response, _pool, _stacktrace)
        return super().increment(method, url, response, _pool, _stacktrace)

# ANSI color codes
class Colors:
    """ANSI color codes for terminal output."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    
    # Foreground colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"
    
    # Bright foreground colors
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

    @classmethod
    def enabled(cls):
        """Check if color output should be enabled."""
        import sys
        return sys.stderr.isatty() and sys.stdout.isatty()

    @classmethod
    def disable(cls):
        """Disable colors by setting all to empty string."""
        for attr in dir(cls):
            if not attr.startswith('_') and attr not in ('enabled', 'disable'):
                setattr(cls, attr, '')

    @classmethod
    def colorize(cls, text, color):
        """Apply color to text if colors are enabled."""
        if cls.enabled():
            return f"{color}{text}{cls.RESET}"
        return text

    @classmethod
    def red(cls, text): return cls.colorize(text, cls.RED)
    @classmethod
    def green(cls, text): return cls.colorize(text, cls.GREEN)
    @classmethod
    def yellow(cls, text): return cls.colorize(text, cls.YELLOW)
    @classmethod
    def blue(cls, text): return cls.colorize(text, cls.BLUE)
    @classmethod
    def cyan(cls, text): return cls.colorize(text, cls.CYAN)
    @classmethod
    def gray(cls, text): return cls.colorize(text, cls.GRAY)
    @classmethod
    def bold(cls, text): return cls.colorize(text, cls.BOLD)
    @classmethod
    def dim(cls, text): return cls.colorize(text, cls.DIM)


class Spinner:
    """Simple spinner for long-running operations."""
    
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    
    def __init__(self, message="", delay=0.1):
        self.message = message
        self.delay = delay
        self._running = False
        self._thread = None
        self._index = 0
    
    def start(self):
        """Start the spinner in a background thread."""
        if not Colors.enabled():
            return
        self._running = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
    
    def stop(self, final_message=""):
        """Stop the spinner and optionally show a final message."""
        if not Colors.enabled():
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
        if final_message:
            sys.stderr.write(f"\r\033[K{final_message}\n")
            sys.stderr.flush()
    
    def _spin(self):
        while self._running:
            frame = self.FRAMES[self._index % len(self.FRAMES)]
            self._index += 1
            sys.stderr.write(f"\r\033[K{cyan(self.FRAMES[self._index % len(self.FRAMES)])} {self.message}")
            sys.stderr.flush()
            time.sleep(self.delay)


def color_enabled():
    """Check if color output is enabled."""
    return Colors.enabled()


def red(text): return Colors.red(text)
def green(text): return Colors.green(text)
def yellow(text): return Colors.yellow(text)
def blue(text): return Colors.blue(text)
def cyan(text): return Colors.cyan(text)
def gray(text): return Colors.gray(text)
def bold(text): return Colors.bold(text)
def dim(text): return Colors.dim(text)


API_URL = "https://www.jiosaavn.com/api.php"
COLLECTION_PAGE_SIZE = 1000
COLLECTION_WORKERS = 4  # default number of tracks downloaded at once
MAX_WORKERS = 16  # above this, rate limiting from the service is likelier than a speedup
__version__ = "0.5.0"
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


def load_config():
    """Load the full config dict, returning defaults for missing keys.
    Raises DownloadError for invalid config (corrupt JSON, wrong types)."""
    path = config_path()
    defaults = {
        "output": "Downloads",
        "quality": "320",
        "workers": COLLECTION_WORKERS,
        "genre": None,
        "template": None,
        "overwrite": False,
        "no_lyrics": False,
        "no_cover": False,
        "timeout": 30,
        "max_speed": None,  # bytes/sec, None = unlimited
        "proxy": None,
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")
        # Merge with defaults, keeping only known keys
        return {k: data.get(k, v) for k, v in defaults.items()}
    except FileNotFoundError:
        return defaults
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise DownloadError(f"Cannot read settings at {path}: {exc}. Use 'jiosaavn-dl config reset' to clear, or fix the config file.") from exc


def save_config(**kwargs):
    """Save configuration keys, preserving existing ones."""
    path = config_path()
    current = load_config()
    current.update({k: v for k, v in kwargs.items() if v is not None})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="config-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(current, handle, indent=2)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return current


def reset_config():
        """Remove the saved configuration, returning the previous output folder."""
        previous = None
        try:
            previous = load_config().get("output")
        except DownloadError:
            previous = None
        path = config_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DownloadError(f"Cannot remove settings at {path}: {exc}.") from exc
        return previous or "Downloads"


def saved_output():
    return load_config()["output"]


def save_output(folder):
        if not folder.strip():
            raise DownloadError("The output folder cannot be empty.")
        destination = Path(folder).expanduser().resolve()
        if destination.exists() and not destination.is_dir():
            raise DownloadError(f"The output path is a file, not a folder: {destination}")
        save_config(output=str(destination))
        return str(destination)


def clean_text(value):
    return unescape(str(value or ""))


def normalize_genre(value):
    """Collapse whitespace; blank or missing means no genre."""
    if value is None:
        return None
    return " ".join(str(value).split()) or None


def _truncate_utf8(value, max_bytes):
    """Return the longest prefix of `value` that fits in `max_bytes`."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", "ignore")


def safe_name(value, fallback="Unknown", max_bytes=255):
    # Keep each component portable, bounded, and inside the output directory.
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", clean_text(value))
    value = sanitize(value).strip(" .")
    if max_bytes < 1:
        return fallback
    if len(value.encode("utf-8")) > max_bytes:
        suffix = "…"
        suffix_bytes = len(suffix.encode("utf-8"))
        if max_bytes < suffix_bytes:
            return fallback
        prefix = _truncate_utf8(value, max(max_bytes - suffix_bytes, 0))
        value = (prefix.rstrip(" .") + suffix) if prefix else suffix
        value = _truncate_utf8(value, max_bytes)
    return value or fallback


TEMPLATE_PLACEHOLDERS = frozenset((
    "artist", "album", "title", "track", "year", "genre", "quality", "playlist",
))
_TEMPLATE_PATTERN = re.compile(r"\{(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?::(?P<fmt>[^}]*))?\}")


def render_template(template, values):
        """Substitute `{placeholder}` fields in `template`.

        `values` maps a placeholder name to its replacement. A `:format` suffix
        such as `{track:02d}` is applied to integers only; unknown placeholders
        raise DownloadError so a typo fails loudly instead of passing through.
        A placeholder that maps to None is treated as unavailable, so a missing
        field (for example `{playlist}` for a single song) also fails loudly.
        """
        def replace(match):
            name = match.group("name")
            if name not in TEMPLATE_PLACEHOLDERS:
                raise DownloadError(f"Unknown template placeholder {{{name}}}")
            if name not in values or values[name] is None:
                raise DownloadError(f"Template placeholder {{{name}}} is not available for this track")
            value = values[name]
            fmt = match.group("fmt")
            if fmt and isinstance(value, int):
                return format(value, fmt)
            return str(value)

        return _TEMPLATE_PATTERN.sub(replace, template)


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
    if len(segments) >= 3 and segments[0] in ("song", "album", "playlist", "featured", "artist"):
        kind = "playlist" if segments[0] == "featured" else segments[0]
    elif len(segments) >= 4 and segments[0] in ("s", "p") and segments[1] in ("song", "album", "playlist", "featured", "artist"):
        kind = "playlist" if segments[1] == "featured" else segments[1]
    else:
        raise DownloadError("Unsupported URL. Expected a song, album, playlist, or artist link.")
    # Fully decode token to catch double-encoded characters (e.g. %252F -> %2F -> /)
    token = segments[-1]
    for _ in range(10):  # Prevent DoS via deeply nested encoding
        decoded = unquote(token)
        if decoded == token:
            break
        token = decoded
    if not token or any(character in token for character in "/\\?#\r\n\x00") or token in (".", ".."):
        raise DownloadError("The URL has an invalid item token.")
    return kind, token


def resolve_url(url, client):
    url = normalized_url(url)
    if urlsplit(url).hostname not in SHORT_HOSTS:
        return parse_url(url)
    # App sharing links require a redirect to reveal the collection type/token.
    # Follow only recognized hosts, with a bound independent of Session defaults.
    visited = set()
    for _ in range(6):
        if url in visited:
            raise DownloadError("Redirect loop detected in share link. Copy its full website URL instead.")
        visited.add(url)
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


_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def display_width(text):
    """Approximate number of terminal columns `text` occupies.

    ANSI CSI escape sequences are ignored. Wide and ambiguous-width characters
    count as 2 so truncation errs on the side of not wrapping on narrow
    terminals (Termux/mobile). Emoji ZWJ sequences count as 2 columns total.
    Combining marks and control characters count as 0.
    """
    width = 0
    in_zwj_sequence = False
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\x1b":
            match = _ANSI_CSI_RE.match(text, i)
            if match:
                i = match.end()
                continue
        cat = unicodedata.category(char)
        if cat in ("Mn", "Me", "Cc"):
            i += 1
            continue
        if cat == "Cf":  # Format characters (includes ZWJ U+200D)
            in_zwj_sequence = True
            i += 1
            continue
        is_emoji = cat == "So"
        if is_emoji and in_zwj_sequence:
            in_zwj_sequence = False
            i += 1
            continue
        char_width = 2 if unicodedata.east_asian_width(char) in ("W", "F", "A") else 1
        width += char_width
        in_zwj_sequence = is_emoji
        i += 1
    return width


def fit_width(text, width):
    """Cut `text` with an ellipsis so it takes at most `width` columns.

    ANSI CSI escape sequences are preserved and never split. A reset sequence
    is appended when truncation occurs inside active styling so styling does
    not leak into following output.
    """
    width = max(width, 1)
    if display_width(text) <= width:
        return text
    target = max(width - 1, 0)
    kept = []
    used = 0
    truncated = False
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\x1b":
            match = _ANSI_CSI_RE.match(text, i)
            if match:
                seq = match.group(0)
                if used >= target and target >= 0:
                    truncated = True
                    break
                kept.append(seq)
                i = match.end()
                continue
        cat = unicodedata.category(char)
        if cat in ("Mn", "Me", "Cc"):
            kept.append(char)
            i += 1
            continue
        if cat == "Cf":
            kept.append(char)
            i += 1
            continue
        is_emoji = cat == "So"
        char_width = 2 if unicodedata.east_asian_width(char) in ("W", "F", "A") else 1
        if used + char_width > target:
            truncated = True
            break
        kept.append(char)
        used += char_width
        i += 1
    result = "".join(kept)
    if truncated:
        result += "\033[0m…" if "\033[" in result else "…"
    return result


def is_true(value):
    return str(value).lower() in ("1", "true", "yes")


class Jiosaavn:
    def __init__(self, output="Downloads", quality="320", timeout=30,
                 overwrite=False, lyrics=True, cover=True, session=None,
                 quiet=False, verbose=False, dry_run=False, workers=COLLECTION_WORKERS,
                 genre=None, template=None, max_speed=None, proxy=None,
                 incremental=False, fallback_qualities=None, save_lyrics_file=False,
                 save_cover_file=False, post_hook=None, no_explicit=False,
                 after_year=None, before_year=None, progress_style="single"):
        self.output = Path(output).expanduser()
        self.genre = normalize_genre(genre)
        self.workers = max(1, min(int(workers), MAX_WORKERS))
        self.quality = str(quality)
        self.timeout = timeout
        self.overwrite = overwrite
        self.lyrics = lyrics
        self.cover = cover
        self.quiet = quiet
        self.verbose = verbose
        self.dry_run = dry_run
        self.template = template
        self.max_speed = max_speed  # bytes/sec, None = unlimited
        self.proxy = proxy
        self.incremental = incremental
        self.fallback_qualities = fallback_qualities or []
        self.save_lyrics_file = save_lyrics_file
        self.save_cover_file = save_cover_file
        self.post_hook = post_hook
        self.no_explicit = no_explicit
        self.after_year = after_year
        self.before_year = before_year
        self.progress_style = progress_style
        self.progress_enabled = False
        self._progress_lock = threading.Lock()
        self._progress_total = 0
        self._progress_done = 0
        self._progress_bytes_received = 0
        self._progress_started_at = None
        self._collection_started_at = None
        self._progress_speed = "0.0MB/s"
        self._progress_track_bytes = {}
        self._progress_active = False  # True while the bar is on screen
        self._active_titles = {}  # {track number: title} shown on the bar line
        self._active_amount = {}  # {track number: "45%" or "3.1MB"} shown next to the title
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
            if Colors.enabled():
                message = yellow(f"Warning: {message}")
            else:
                message = f"Warning: {message}"
            self._emit(message, sys.stderr)

    def _start_progress(self, total, collection_started_at=None):
        if not self.progress_enabled or self.quiet:
            return
        with self._progress_lock:
            self._progress_total = total
            self._progress_done = 0
            self._progress_bytes_received = 0
            self._progress_started_at = time.monotonic()
            self._collection_started_at = (
                self._progress_started_at if collection_started_at is None
                else collection_started_at)
            self._progress_speed = "0.0MB/s"
            self._progress_track_bytes = {}
            self._active_titles = {}
            self._active_amount = {}
            self._progress_active = True
            self._write_progress()

    def _advance_progress(self, pos=None):
        if not self._progress_active:
            return
        with self._progress_lock:
            self._active_titles.pop(pos, None)
            self._active_amount.pop(pos, None)
            self._progress_track_bytes.pop(pos, None)
            self._progress_done += 1
            self._write_progress()
            if self._progress_done >= self._progress_total:
                sys.stderr.write("\n")
                sys.stderr.flush()
                self._progress_active = False

    def _write_progress(self):
        """Redraw the progress display. The caller must hold self._progress_lock."""
        if self.progress_style == "detailed":
            self._write_detailed_progress()
        elif self.progress_style == "compact":
            self._write_compact_progress()
        elif self.progress_style == "pulse":
            self._write_pulse_progress()
        else:
            self._write_single_progress()

    def _write_single_progress(self):
        """Redraw the single-line progress bar. The caller must hold self._progress_lock."""
        width = 8
        total = max(self._progress_total, 1)
        filled = int(width * self._progress_done / total)
        # Color the progress bar
        bar_filled = green("#" * filled) if Colors.enabled() else "#" * filled
        bar_empty = gray("-" * (width - filled)) if Colors.enabled() else "-" * (width - filled)
        bar = bar_filled + bar_empty
        elapsed = max(0, int(time.monotonic() - self._collection_started_at))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        # Colorize key metrics
        progress_text = bold(f"{self._progress_done}/{self._progress_total}") if Colors.enabled() else f"{self._progress_done}/{self._progress_total}"
        speed_text = cyan(self._progress_speed) if Colors.enabled() else self._progress_speed
        elapsed_text = blue(f"{hours}:{minutes:02d}:{seconds:02d}") if Colors.enabled() else f"{hours}:{minutes:02d}:{seconds:02d}"
        line = (f"Downloading [{bar}] {progress_text} "
                f"{speed_text} Elapsed {elapsed_text}")
        remaining = self._progress_total - self._progress_done
        if self._progress_done >= 2 and remaining > 0:
            eta = int(elapsed * remaining / self._progress_done)
            eta_hours, eta_remainder = divmod(eta, 3600)
            eta_minutes, eta_seconds = divmod(eta_remainder, 60)
            if eta_hours:
                eta_text = f"{eta_hours}h{eta_minutes}m"
            elif eta_minutes:
                eta_text = f"{eta_minutes}m{eta_seconds}s"
            else:
                eta_text = f"{eta_seconds}s"
            eta_text = yellow(eta_text) if Colors.enabled() else eta_text
            line += f" ETA {eta_text}"
        if self._active_titles:
            line += " " + ", ".join(
                f"{pos:02d} {self._active_amount[pos] + ' ' if pos in self._active_amount else ''}{title}"
                for pos, title in sorted(self._active_titles.items()))
        columns = shutil.get_terminal_size((80, 24)).columns
        sys.stderr.write("\r\033[K" + fit_width(line, columns - 1))
        sys.stderr.flush()

    def _write_detailed_progress(self):
        """Redraw the detailed multi-line progress display. The caller must hold self._progress_lock."""
        elapsed = max(0, int(time.monotonic() - self._collection_started_at))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)

        columns = shutil.get_terminal_size((80, 24)).columns
        max_line = max(columns - 1, 20)

        # Header line
        progress_text = bold(f"{self._progress_done}/{self._progress_total}") if Colors.enabled() else f"{self._progress_done}/{self._progress_total}"
        speed_text = cyan(self._progress_speed) if Colors.enabled() else self._progress_speed
        elapsed_text = blue(f"{hours}:{minutes:02d}:{seconds:02d}") if Colors.enabled() else f"{hours}:{minutes:02d}:{seconds:02d}"

        header = (f"Downloading {progress_text}  {speed_text}  Elapsed {elapsed_text}")
        remaining = self._progress_total - self._progress_done
        if self._progress_done >= 2 and remaining > 0:
            eta = int(elapsed * remaining / self._progress_done)
            eta_hours, eta_remainder = divmod(eta, 3600)
            eta_minutes, eta_seconds = divmod(eta_remainder, 60)
            if eta_hours:
                eta_text = f"{eta_hours}h{eta_minutes}m"
            elif eta_minutes:
                eta_text = f"{eta_minutes}m{eta_seconds}s"
            else:
                eta_text = f"{eta_seconds}s"
            eta_text = yellow(eta_text) if Colors.enabled() else eta_text
            header += f"  ETA {eta_text}"

        lines = [header]

        # Individual track progress bars
        if self._active_titles:
            for pos in sorted(self._active_titles.keys()):
                title = self._active_titles[pos]
                amount = self._active_amount.get(pos, "")
                bar_width = 20
                if amount.endswith("%"):
                    try:
                        pct = int(amount.rstrip("%"))
                        filled = int(bar_width * pct / 100)
                    except ValueError:
                        filled = 0
                else:
                    filled = 0
                bar_filled = green("#" * filled) if Colors.enabled() else "#" * filled
                bar_empty = gray("-" * (bar_width - filled)) if Colors.enabled() else "-" * (bar_width - filled)
                track_bar = bar_filled + bar_empty
                track_line = f"  {pos:02d} [{track_bar}] {amount} {title}".rstrip()
                lines.append(track_line)

        # Overall progress bar at bottom
        width = 30
        total = max(self._progress_total, 1)
        filled = int(width * self._progress_done / total)
        bar_filled = green("#" * filled) if Colors.enabled() else "#" * filled
        bar_empty = gray("-" * (width - filled)) if Colors.enabled() else "-" * (width - filled)
        overall_bar = bar_filled + bar_empty
        lines.append(f"Overall [{overall_bar}] {progress_text}")

        lines = [fit_width(line, max_line) for line in lines]
        output = "\r\033[K" + "\n".join(lines)
        sys.stderr.write(output)
        sys.stderr.flush()

    def _write_compact_progress(self):
        """Redraw the compact progress display. The caller must hold self._progress_lock."""
        elapsed = max(0, int(time.monotonic() - self._collection_started_at))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)

        columns = shutil.get_terminal_size((80, 24)).columns
        max_line = max(columns - 1, 20)
        active_count = len(self._active_titles)

        # ETA
        remaining = self._progress_total - self._progress_done
        eta_text = ""
        if self._progress_done >= 2 and remaining > 0:
            eta = int(elapsed * remaining / self._progress_done)
            eta_hours, eta_remainder = divmod(eta, 3600)
            eta_minutes, eta_seconds = divmod(eta_remainder, 60)
            if eta_hours:
                eta_text = f"{eta_hours}h{eta_minutes}m"
            elif eta_minutes:
                eta_text = f"{eta_minutes}m{eta_seconds}s"
            else:
                eta_text = f"{eta_seconds}s"

        progress_text = bold(f"{self._progress_done}/{self._progress_total}") if Colors.enabled() else f"{self._progress_done}/{self._progress_total}"
        speed_text = cyan(self._progress_speed) if Colors.enabled() else self._progress_speed
        elapsed_str = f"{hours}:{minutes:02d}:{seconds:02d}"
        elapsed_text = blue(elapsed_str) if Colors.enabled() else elapsed_str
        bar_width = 20
        total = max(self._progress_total, 1)
        filled = int(bar_width * self._progress_done / total)
        bar_filled = green("#" * filled) if Colors.enabled() else "#" * filled
        bar_empty = gray("-" * (bar_width - filled)) if Colors.enabled() else "-" * (bar_width - filled)

        parts = [
            f"Downloading {progress_text}",
            f"[{bar_filled}{bar_empty}]",
            speed_text,
            f"Elapsed {elapsed_text}",
        ]
        if active_count:
            parts.append(cyan(f"({active_count} active)") if Colors.enabled() else f"({active_count} active)")
        if eta_text:
            parts.append(yellow(f"ETA {eta_text}") if Colors.enabled() else f"ETA {eta_text}")
        header = "  ".join(parts)

        active_lines = []
        for pos in sorted(self._active_titles.keys())[:4]:
            title = self._active_titles[pos]
            amount = self._active_amount.get(pos, "")
            tbar_width = 12
            if amount.endswith("%"):
                try:
                    pct = int(amount.rstrip("%"))
                    filled_t = int(tbar_width * pct / 100)
                except ValueError:
                    filled_t = 0
            else:
                filled_t = 0
            tbar_filled = green("#" * filled_t) if Colors.enabled() else "#" * filled_t
            tbar_empty = gray("-" * (tbar_width - filled_t)) if Colors.enabled() else "-" * (tbar_width - filled_t)
            track_line = f"  {pos:02d} [{tbar_filled}{tbar_empty}] {amount} {title}".rstrip()
            active_lines.append(track_line)

        while len(active_lines) < 4:
            active_lines.append("")

        lines = [fit_width(header, max_line)] + [fit_width(line, max_line) for line in active_lines]
        # Truncate each line to the terminal width so it never wraps. Wrapped
        # lines break the fixed cursor-up redraw and cause repeated/corrupted
        # output on narrow terminals (e.g. Termux).
        output = "\r\033[K" + "\n".join(lines) + "\033[4A"
        sys.stderr.write(output)
        sys.stderr.flush()

    def _write_pulse_progress(self):
        """Redraw the pulse-style progress display (yt-dlp style). The caller must hold self._progress_lock."""
        elapsed = max(0, int(time.monotonic() - self._collection_started_at))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        # Current status based on what's happening
        status = "Downloading"
        if self._active_titles:
            first_pos = min(self._active_titles.keys())
            title = self._active_titles[first_pos]
            if len(title) > 40:
                title = title[:37] + "..."
            status = f"Downloading {title}"
        else:
            status = "Downloading"
        
        # Global progress
        total = max(self._progress_total, 1)
        progress_pct = self._progress_done / total
        filled = int(40 * self._progress_done / max(self._progress_total, 1))
        
        # Build pulse bar with Unicode characters
        filled_bar = "━" * filled
        if filled < 40:
            arrow = "╸"
            empty_bar = " " * (40 - filled - 1)
        else:
            arrow = ""
            empty_bar = ""
        
        bar_str = filled_bar + arrow + empty_bar
        
        # Percentage
        pct = int(self._progress_done / max(self._progress_total, 1) * 100)
        
        # ETA
        elapsed_time = max(0, int(time.monotonic() - self._collection_started_at))
        remaining = self._progress_total - self._progress_done
        if self._progress_done >= 2 and remaining > 0:
            eta = int(elapsed * remaining / self._progress_done)
            eta_hours, eta_remainder = divmod(eta, 3600)
            eta_minutes, eta_seconds = divmod(eta_remainder, 60)
            if eta_hours:
                eta_text = f"{eta_hours}h{eta_minutes}m"
            elif eta_minutes:
                eta_text = f"{eta_minutes}m{eta_seconds}s"
            else:
                eta_text = f"{eta_seconds}s"
            eta_str = f"  ETA {eta_text}"
        else:
            eta_str = ""
        
        # Build pulse bar
        filled = int(40 * self._progress_done / max(self._progress_total, 1))
        if filled < 40:
            arrow = "╸"
            empty_bar = " " * (40 - filled - 1)
            bar_str = "━" * filled + "╸" + " " * (40 - filled - 1)
        else:
            bar_str = "━" * 40
        
        pct = int(self._progress_done / max(self._progress_total, 1) * 100)
        
        # Build the pulse-style line
        bar_colored = green(bar_str) if Colors.enabled() else bar_str
        pct_str = f"{pct}%"
        status_text = f" {bold('Downloading') if Colors.enabled() else 'Downloading'}"
        
        # ETA
        elapsed_time = max(0, int(time.monotonic() - self._collection_started_at))
        remaining = self._progress_total - self._progress_done
        if self._progress_done >= 2 and remaining > 0:
            eta = int(elapsed * remaining / self._progress_done)
            eta_hours, eta_remainder = divmod(eta, 3600)
            eta_minutes, eta_seconds = divmod(eta_remainder, 60)
            if eta_hours:
                eta_text = f"{eta_hours}h{eta_minutes}m"
            elif eta_minutes:
                eta_text = f"{eta_minutes}m{eta_seconds}s"
            else:
                eta_text = f"{eta_seconds}s"
            eta_str = f"  ETA {eta_text}"
        else:
            eta_str = ""
        
        bar = "━" * filled + ("╸" if filled < 40 else "") + " " * max(0, 40 - filled - 1)
        bar_colored = green(bar) if Colors.enabled() else bar
        pct_str = bold(f"{pct}%") if Colors.enabled() else f"{pct}%"
        status_text = bold("Downloading") if Colors.enabled() else "Downloading"
        line = f" {status_text}  {pct_str}  {bar_colored}{eta_str}"
        columns = shutil.get_terminal_size((80, 24)).columns
        sys.stderr.write("\r\033[K" + fit_width(line, max(columns - 1, 20)))
        sys.stderr.flush()

    def _finish_progress(self):
        if not self._progress_active:
            return
        with self._progress_lock:
            self._active_titles = {}
            self._active_amount = {}
            self._progress_track_bytes = {}
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
            self._progress_track_bytes[pos] = 0
            self._write_progress()

    def _track_finished(self, pos, total, title, artists, outcome, announce=True):
        """Drop a track from the progress line and print how it ended."""
        if not self._progress_active:
            return
        with self._progress_lock:
            self._active_titles.pop(pos, None)
            self._active_amount.pop(pos, None)
            self._progress_track_bytes.pop(pos, None)
        if announce:
            # Colorize the outcome
            if Colors.enabled():
                if outcome == "Downloaded":
                    outcome = green(outcome)
                elif outcome == "Skipped":
                    outcome = yellow(outcome)
                elif outcome == "Failed":
                    outcome = red(outcome)
                elif outcome == "Would download":
                    outcome = cyan(outcome)
            self._emit(f"  [{pos}/{total}] {outcome}: {title} — {artists}")

    def _track_progress(self, pos, done, total):
        """Show how much of a track has downloaded next to its title."""
        if not self._progress_active:
            return
        if total:
            amount = f"{min(done * 100 // total, 100)}%"
        else:
            amount = f"{done / (1024 * 1024):.1f}MB"
        with self._progress_lock:
            # Redraw only when the shown value changes, and never for a track
            # that has already finished (a late callback).
            if pos in self._active_titles:
                previous = self._progress_track_bytes.get(pos, 0)
                self._progress_bytes_received += done - previous if done >= previous else done
                self._progress_track_bytes[pos] = done
                elapsed = max(time.monotonic() - self._progress_started_at, 0.001)
                speed_mbps = self._progress_bytes_received / elapsed / (1024 * 1024)
                if speed_mbps >= 1000:
                    speed = f"{speed_mbps / 1024:.1f}GB/s"
                else:
                    speed = f"{speed_mbps:.1f}MB/s"
                changed = speed != self._progress_speed
                self._progress_speed = speed
                if self._active_amount.get(pos) != amount:
                    self._active_amount[pos] = amount
                    changed = True
                if changed:
                    self._write_progress()

    def _track_progress_baseline(self, pos, done):
        """Set the saved-byte baseline so resumed data is not counted as received."""
        if not self._progress_active:
            return
        with self._progress_lock:
            if pos in self._active_titles:
                self._progress_track_bytes[pos] = done

    def _track_failed(self, pos, total, exc):
        with self._progress_lock:
            title = self._active_titles.pop(pos, None)
            self._active_amount.pop(pos, None)
        failed_text = red("Failed") if Colors.enabled() else "Failed"
        self._emit(f"  [{pos}/{total}] {failed_text}: {title or f'track {pos}'} — {exc}", sys.stderr)

    def _configure_session(self, session):
        retries = _RetryWithRetryAfter(total=3, backoff_factor=0.5,
                                       status_forcelist=(429, 500, 502, 503, 504),
                                       allowed_methods=frozenset(("GET",)))
        session.mount("https://", HTTPAdapter(max_retries=retries))
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}

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
                # Extract first complete JSON object (handles nested objects)
                start = text.find("{")
                if start < 0:
                    raise ValueError("No JSON object in response")
                depth = 0
                in_string = False
                escape = False
                end = -1
                for i, ch in enumerate(text[start:], start):
                    if escape:
                        escape = False
                        continue
                    if ch == "\\":
                        escape = True
                        continue
                    if ch == '"' and not escape:
                        in_string = not in_string
                        continue
                    if not in_string:
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                end = i
                                break
                if end < 0:
                    raise ValueError("No complete JSON object in response")
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

    @staticmethod
    def _normalize_album_title(title):
        """Normalize album title for deduplication by removing edition suffixes.

        Removes text in parentheses/brackets that indicate editions:
        e.g., "Dangerous Woman (Tenth Anniversary Edition)" -> "Dangerous Woman"
        Also handles suffixes after colons: "Album Deluxe: Bonus" -> "Album"
        """
        import re
        # Remove text in parentheses or brackets that looks like edition info
        # Patterns: (Edition), [Edition], (Deluxe), [Deluxe], (Remastered), etc.
        normalized = re.sub(r'\s*[\(\[].*?(edition|deluxe|remaster|remastered|anniversary|expanded|bonus|reissue|special|collector|limited|demo|acoustic|live|instrumental|version|remix).*?[\)\]]', '', title, flags=re.IGNORECASE)
        # Also remove edition suffixes after colon: "Album Deluxe: Bonus Tracks" -> "Album"
        normalized = re.sub(r'\s*:\s*(edition|deluxe|remaster|remastered|anniversary|expanded|bonus|reissue|special|collector|limited|demo|acoustic|live|instrumental|version|remix).*$', '', normalized, flags=re.IGNORECASE)
        # Also remove "Word:" suffixes like "Album Deluxe:" -> "Album"
        normalized = re.sub(r'\s+\b(edition|deluxe|remaster|remastered|anniversary|expanded|bonus|reissue|special|collector|limited|demo|acoustic|live|instrumental|version|remix):\s*.*$', '', normalized, flags=re.IGNORECASE)
        # Also remove trailing version numbers like " (2021)", " [2021]"
        normalized = re.sub(r'\s*[\(\[](\d{4})[\)\]]', '', normalized)
        return normalized.strip()

    def artist_albums(self, artist_id, page=1, limit=COLLECTION_PAGE_SIZE):
        """Fetch all albums for an artist.

        Uses search API to find songs by the artist, then extracts unique album URLs.
        Falls back to artist.getAlbums API if available.

        Deduplicates albums by normalizing titles (removing edition suffixes)
        so that "Album (Deluxe)" and "Album (Anniversary Edition)" are treated as same.

        Args:
            artist_id: Artist token from artist URL (or artist name)
            page: Page number (1-indexed) for pagination
            limit: Number of results per page

        Returns:
            Dict with album list extracted from search results
        """
        # First try the official artist.getAlbums API
        try:
            return self.api("artist.getAlbums", artist_id=artist_id, p=page, n=limit)
        except DownloadError:
            pass  # Fall through to search-based approach

        # Fallback: Search for songs by artist_id/name, extract unique album URLs
        # Use artist_id as search query - could be a name or ID
        search_query = artist_id
        albums_seen = set()  # Track normalized titles for deduplication
        albums = []

        # Search for songs by this artist (use primary_artists_id if numeric)
        search_limit = min(limit * 5, 100)  # Get more songs to find unique albums
        spinner = Spinner(f"Fetching albums for {search_query}...")
        spinner.start()
        try:
            data = self.api("search.getResults", q=search_query, p=page, n=search_limit,
                            _marker="0", ctx="web6dot0")
        finally:
            spinner.stop(f"Found albums for {search_query}")
        results = data.get("results", [])

        for result in results:
            if not isinstance(result, dict):
                continue
            album_url = result.get("album_url")
            album_title = result.get("album")
            album_id = result.get("albumid")
            if not album_url or not album_title:
                continue
            # Normalize title for deduplication (compare base album name)
            normalized_title = self._normalize_album_title(album_title)
            if not normalized_title:
                continue
            if normalized_title in albums_seen:
                continue
            albums_seen.add(normalized_title)
            albums.append({
                "id": album_id,
                "title": album_title,
                "perma_url": album_url,
                "image": result.get("image"),
                "year": result.get("year"),
                "primary_artists": result.get("primary_artists"),
            })
            if len(albums) >= limit:
                break

        return {"albums": albums, "artist_name": search_query}

    def search(self, query, limit=10):
        """Search songs, albums, and playlists, returning supported public URLs."""
        query = str(query).strip()
        if not query:
            raise DownloadError("Search query cannot be blank.")
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise DownloadError("Search result limit must be a positive integer.") from exc
        if limit < 1 or limit > 50:
            raise DownloadError("Search result limit must be between 1 and 50.")

        spinner = Spinner(f"Searching for '{query}'...")
        spinner.start()
        try:
            data = self.api("search.getResults", q=query, p=1, n=limit)
        finally:
            spinner.stop(f"Search completed for '{query}'")
        results = data.get("results")
        if not isinstance(results, list):
            raise DownloadError("JioSaavn returned an unsupported search response.")

        supported = []
        for result in results:
            if not isinstance(result, dict):
                continue
            raw_url = result.get("perma_url") or result.get("url")
            if not isinstance(raw_url, str) or not raw_url.strip():
                continue
            raw_url = raw_url.strip()
            if raw_url.startswith("/"):
                raw_url = "https://www.jiosaavn.com" + raw_url
            try:
                url = normalized_url(raw_url)
                kind, _ = parse_url(url)
            except (DownloadError, ValueError):
                continue
            if kind not in ("song", "album", "playlist"):
                continue
            title = clean_text(result.get("title") or result.get("song")
                               or result.get("album") or "Untitled")
            subtitle = clean_text(result.get("subtitle") or result.get("primary_artists")
                                  or result.get("singers") or "")
            supported.append({"kind": kind, "url": url, "title": title,
                              "subtitle": subtitle})
            if len(supported) >= limit:
                break
        return supported

    def getCdnURL(self, encurl, fallback_qualities=None):
        """Get CDN URL for a track, with optional quality fallback.

        Args:
            encurl: Encrypted media URL from JioSaavn
            fallback_qualities: List of quality strings to try in order (e.g. ["320", "160", "96"])
                               If None, uses self.quality only.
        """
        qualities = [self.quality] if fallback_qualities is None else [self.quality] + fallback_qualities
        # Deduplicate while preserving order
        seen = set()
        qualities = [q for q in qualities if not (q in seen or seen.add(q))]

        last_error = None
        for quality in qualities:
            try:
                data = self.api("song.generateAuthToken", url=encurl,
                                bitrate=quality, api_version="4")
                url = data.get("auth_url")
                if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                    raise DownloadError("No audio URL is available for this track.")
                # The web endpoint sometimes returns its web stream instead of AAC.
                # Known host patterns: web.* -> aac.*, or return as-is if already aac/cdn
                parts = urlsplit(url)
                host = parts.netloc
                if host.startswith("web."):
                    host = "aac." + host[4:]
                elif "web" in host:
                    # Fallback: replace first occurrence of "web" with "aac"
                    host = host.replace("web", "aac", 1)
                # If host didn't change, assume it's already a direct CDN URL
                if quality != self.quality:
                    self._warning(f"  Falling back to {quality} kbps (requested {self.quality} kbps unavailable)")
                return parts._replace(netloc=host).geturl()
            except DownloadError as exc:
                last_error = exc
                if "unavailable" in str(exc).lower() or "not available" in str(exc).lower():
                    continue  # Try next quality
                raise  # Other errors (auth, etc.) - don't fallback

        # All qualities exhausted
        raise DownloadError(f"No audio URL available at any quality (tried: {', '.join(qualities)}). Last error: {last_error}")

    def _template_values(self, song, artists, isPlaylist, song_pos):
        """Build the placeholder map for the output template."""
        year = clean_text(song.get('year')) or "Unknown"
        genre = self.genre or clean_text(song.get('genre')) or "Unknown"
        playlist = safe_name(isPlaylist) if isPlaylist else "Unknown"
        return {
            "artist": artists,
            "album": clean_text(song.get('album')) or "Unknown Album",
            "title": clean_text(song.get('song')) or "Unknown",
            "track": song_pos,
            "year": year,
            "genre": genre,
            "quality": self.quality,
            "playlist": playlist,
        }

    def _template_folder(self, song, artists, isPlaylist, song_pos):
        """Render the output template into a relative folder path.

        When no template is set, the default album or playlist folder name is
        returned unchanged. Each path component is sanitized separately so a
        placeholder cannot escape the output directory.
        """
        if not self.template:
            if isPlaylist:
                return safe_name(isPlaylist)
            return safe_name(
                f"{clean_text(song.get('album')) or 'Unknown Album'} - {artists} "
                f"[{song.get('year') or 'Unknown'}]")
        rendered = render_template(self.template,
                                   self._template_values(song, artists, isPlaylist, song_pos))
        components = [safe_name(part) for part in rendered.split("/") if part.strip()]
        return "/".join(components) if components else "Unknown"

    def partial_path(self, path):
        """Temporary file for an in-progress download of `path`.

        The requested bitrate is part of the name, so an interrupted download
        is only resumed at the same quality; resuming another quality's bytes
        would splice two different files together.
        """
        return path.with_name(f"{path.name}.{self.quality}.part")

    def _partial_meta_path(self, path):
        """Sidecar file storing ETag/Content-MD5 for resume validation."""
        return self.partial_path(path).with_suffix(".part.meta")

    @staticmethod
    def _discard_partials(path):
        """Delete leftover partial downloads of `path` at any quality."""
        prefix = path.name + "."
        for partial in path.parent.glob("*.part"):
            if partial.name.startswith(prefix):
                partial.unlink(missing_ok=True)
                # Also remove sidecar meta file
                meta = partial.with_suffix(".part.meta")
                meta.unlink(missing_ok=True)

    def download_file(self, url, path, progress=None):
        """Resume a partial stream into a temporary file.

        `progress(done, total)`, if given, is called after each chunk with the
        bytes saved so far (including a resumed prefix) and the full file size,
        or None when the server does not say how large the file is.

        Validates ETag/Content-MD5 on resume to prevent corrupted spliced files.
        """
        temp = self.partial_path(path)
        meta_path = self._partial_meta_path(path)
        offset = temp.stat().st_size if temp.exists() else 0

        # Load stored validators for resume validation
        stored_etag = stored_md5 = None
        if offset and meta_path.exists():
            try:
                import json
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                stored_etag = data.get("etag")
                stored_md5 = data.get("md5")
            except (OSError, json.JSONDecodeError):
                pass  # Corrupt/missing meta; will restart

        for attempt in range(2):
            request_options = {"stream": True}
            if offset:
                request_options["headers"] = {
                    "Range": f"bytes={offset}-",
                    "Accept-Encoding": "identity",
                }
            try:
                with self.request(url, **request_options) as response:
                    if response.status_code == 416:
                        if attempt == 0 and offset:
                            temp.unlink(missing_ok=True)
                            meta_path.unlink(missing_ok=True)
                            offset = 0
                            continue
                        raise DownloadError(
                            "The server rejected the download range twice; "
                            "the stale partial download was discarded. Retry the track."
                        )

                    # Validate ETag/Content-MD5 on resume (206 Partial Content)
                    if offset and response.status_code == 206:
                        current_etag = response.headers.get("ETag", "").strip('"')
                        current_md5 = response.headers.get("Content-MD5", "")
                        if stored_etag and current_etag and stored_etag != current_etag:
                            self._warning("  ETag mismatch on resume; restarting download.")
                            temp.unlink(missing_ok=True)
                            meta_path.unlink(missing_ok=True)
                            offset = 0
                            continue
                        if stored_md5 and current_md5 and stored_md5 != current_md5:
                            self._warning("  Content-MD5 mismatch on resume; restarting download.")
                            temp.unlink(missing_ok=True)
                            meta_path.unlink(missing_ok=True)
                            offset = 0
                            continue

                    return self._save_response(temp, response, offset, progress, meta_path)
            except requests.RequestException as exc:
                raise DownloadError(f"Download interrupted: {exc}") from exc
        raise DownloadError("The download could not be started.")

    def _save_response(self, temp, response, offset, progress, meta_path=None):
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
                if meta_path:
                    meta_path.unlink(missing_ok=True)
                raise DownloadError("The server returned an invalid range response.")
            if match.group(3) != "*":
                expected_total = int(match.group(3))
                if int(match.group(2)) >= expected_total:
                    temp.unlink(missing_ok=True)
                    if meta_path:
                        meta_path.unlink(missing_ok=True)
                    raise DownloadError("The server returned an invalid range response.")
            if response.headers.get("Content-Encoding", "").lower() not in ("", "identity"):
                temp.unlink(missing_ok=True)
                if meta_path:
                    meta_path.unlink(missing_ok=True)
                raise DownloadError("The server encoded a ranged response; retry without resuming.")
        size = 0
        saved_before = offset if append else 0
        total_size = expected_total
        if total_size is None and not response.headers.get("Content-Encoding"):
            length = response.headers.get("Content-Length")
            if length and length.isdigit():
                total_size = saved_before + int(length)
        with temp.open("ab" if append else "wb") as handle:
            last_time = time.monotonic()
            bytes_since_throttle = 0
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if chunk:
                    handle.write(chunk)
                    size += len(chunk)
                    if progress:
                        progress(saved_before + size, total_size)

                    # Throttle download speed if max_speed is set
                    if self.max_speed:
                        bytes_since_throttle += len(chunk)
                        now = time.monotonic()
                        elapsed = now - last_time
                        if elapsed > 0:
                            current_rate = bytes_since_throttle / elapsed
                            if current_rate > self.max_speed:
                                # Sleep to throttle
                                target_time = bytes_since_throttle / self.max_speed
                                sleep_time = target_time - elapsed
                                if sleep_time > 0:
                                    time.sleep(sleep_time)
                        # Reset counters after each chunk to avoid accumulation
                        last_time = time.monotonic()
                        bytes_since_throttle = 0
        if not size:
            if not offset:
                temp.unlink(missing_ok=True)
                if meta_path:
                    meta_path.unlink(missing_ok=True)
            raise DownloadError("The server returned an empty file.")
        expected = response.headers.get("Content-Length")
        if expected and expected.isdigit() and not response.headers.get("Content-Encoding"):
            if size != int(expected):
                raise DownloadError("Download was incomplete. Please retry.")
        if expected_total is not None and temp.stat().st_size != expected_total:
            raise DownloadError("Download was incomplete. Please retry.")

        # Store ETag/Content-MD5 for future resume validation
        if meta_path and not append:  # Only on fresh start (200 OK), not on resume
            etag = response.headers.get("ETag", "").strip('"')
            md5 = response.headers.get("Content-MD5", "")
            if etag or md5:
                import json
                try:
                    meta_path.write_text(json.dumps({"etag": etag, "md5": md5}), encoding="utf-8")
                except OSError:
                    pass  # Non-fatal

        return temp

    def tagger(self, song, song_path, album_artist=None, pos=1, total=1, artwork=None,
               song_id=None):
        audio = MP4(song_path)
        for atom, key in (("\xa9nam", "song"), ("\xa9alb", "album"),
                          ("\xa9ART", "primary_artists"), ("\xa9wrt", "music"),
                          ("\xa9day", "release_date"), ("\xa9gen", "genre"),
                          ("cprt", "copyright_text")):
            value = clean_text(song.get(key))
            if value:
                audio[atom] = value
        audio["aART"] = clean_text(album_artist or song.get("primary_artists"))
        audio["trkn"] = [(pos, total)]
        if self.genre:
            audio["\xa9gen"] = self.genre
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

        # Add ISRC if available
        isrc = clean_text(song.get("isrc"))
        if isrc:
            audio["----:com.apple.iTunes:ISRC"] = isrc.encode("utf-8")

        # Add disc number if available
        disc_number = song.get("disc_number") or song.get("disc")
        if disc_number:
            try:
                disc_num = int(disc_number)
                # MP4 uses disk number as a tuple (disc_number, total_discs)
                total_discs = song.get("total_discs") or 1
                try:
                    total_discs = int(total_discs)
                except (ValueError, TypeError):
                    total_discs = 1
                audio["disk"] = [(disc_num, total_discs)]
            except (ValueError, TypeError):
                pass

        # Add compilation flag if available
        compilation = song.get("compilation") or song.get("is_compilation")
        if is_true(compilation):
            audio["cpil"] = [1]

        if self.lyrics and is_true(song.get("has_lyrics")) and song.get("id"):
            try:
                lyrics = self.api("lyrics.getLyrics", lyrics_id=song["id"], api_version="4")
                if lyrics.get("lyrics"):
                    lyrics_text = clean_text(re.sub(r"<br\s*/?>", "\n", lyrics["lyrics"]))
                    audio["\xa9lyr"] = lyrics_text
                    # Save lyrics as separate .lrc file if requested
                    if self.save_lyrics_file:
                        lrc_path = song_path.with_suffix(".lrc")
                        try:
                            lrc_path.write_text(lyrics_text, encoding="utf-8")
                        except OSError as exc:
                            self._warning(f"  Warning: Could not save lyrics file: {exc}")
            except Exception as exc:
                # Catch all exceptions (API errors, malformed response, regex errors)
                # so lyrics failure never prevents track download
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
        """Record a saved track and retain overwrite claims for this client run."""
        with self._index_lock:
            index = self._folder_ids.get(directory)
            if index is not None:
                index[path] = track_id
            # Normal downloads can rely on the folder index for duplicate
            # detection. In overwrite mode, retain the claim so a duplicate
            # later in this same collection does not overwrite the file twice.
            claims = self._folder_claims.get(directory)
            if (not self.overwrite and claims is not None
                    and claims.get(track_id) == path):
                del claims[track_id]

    def _release_track(self, directory, path, track_id):
        """Release a claim when a dry run or failed download leaves no saved file."""
        with self._index_lock:
            claims = self._folder_claims.get(directory)
            if claims is not None and claims.get(track_id) == path:
                del claims[track_id]

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

        # Filter by explicit content
        if self.no_explicit and is_true(song.get("explicit_content")):
            self._increment_counter("skipped")
            self._status(f"  Skipped (explicit content): {clean_text(song.get('song')) or song_id}",
                         verbose_only=self.progress_enabled)
            return

        # Filter by release year
        year_str = clean_text(song.get("year"))
        if year_str and year_str.isdigit():
            year = int(year_str)
            if self.after_year is not None and year < self.after_year:
                self._increment_counter("skipped")
                self._status(f"  Skipped (year {year} before --after-year {self.after_year}): {clean_text(song.get('song')) or song_id}",
                             verbose_only=self.progress_enabled)
                return
            if self.before_year is not None and year > self.before_year:
                self._increment_counter("skipped")
                self._status(f"  Skipped (year {year} after --before-year {self.before_year}): {clean_text(song.get('song')) or song_id}",
                             verbose_only=self.progress_enabled)
                return

        title = clean_text(song.get("song")) or song_id
        artists = clean_text(album_artist or song.get("primary_artists")) or "Unknown Artist"
        folder = self._template_folder(song, artists, isPlaylist, song_pos)
        directory = self.output / folder
        prefix = f"{song_pos:02d}. "
        suffix = ".m4a"
        partial_suffix = f".{self.quality}.part"
        title = safe_name(
            title,
            max_bytes=255 - len((prefix + suffix + partial_suffix).encode("utf-8")),
        )
        destination = directory / f"{prefix}{title}{suffix}"
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
            would_download = cyan("Would download") if Colors.enabled() else "Would download"
            details = f"  {would_download}: {destination}"
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
        audio_url = self.getCdnURL(encrypted, fallback_qualities=self.fallback_qualities)
        artwork = None
        if self.cover and song.get("image"):
            try:
                cover_url = song["image"].replace("150x150", "500x500")
                with self.request(cover_url) as response:
                    artwork = response.content
                if not artwork.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
                    raise DownloadError("Invalid cover image.")
                # Save cover art as folder.jpg if requested
                if self.save_cover_file:
                    cover_path = directory / "folder.jpg"
                    try:
                        cover_path.write_bytes(artwork)
                    except OSError as exc:
                        self._warning(f"  Warning: Could not save cover file: {exc}")
            except DownloadError as exc:
                artwork = None
                self._warning(f"  Warning: cover unavailable: {exc}")
        partial = self.partial_path(destination)
        self._track_progress_baseline(
            song_pos, partial.stat().st_size if partial.exists() else 0)
        temp = self.download_file(
            audio_url, destination,
            progress=lambda done, total: self._track_progress(song_pos, done, total))
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
        # Execute post-hook if configured
        if self.post_hook:
            try:
                import subprocess  # nosec B404
                import shlex
                # Use shlex.split to safely parse the command without shell=True
                cmd = shlex.split(self.post_hook)
                subprocess.run(cmd, shell=False, check=False,  # nosec B603
                               env={**os.environ, "JIOSAAVN_FILE": str(destination)})
            except Exception as exc:
                self._warning(f"  Warning: post-hook failed: {exc}")

    def _process_collection_track(self, kind, data, name, pos, total, song):
        self._worker_local.title = None
        retry_url = None
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
                # Track session immediately to ensure cleanup on any exception
                with self._worker_sessions_lock:
                    self._worker_sessions.append(worker_session)
                self._worker_local.session = worker_session
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

    def _filter_existing_tracks(self, songs, kind):
        """Filter out tracks that already exist based on song ID.
        Returns list of songs that don't exist yet."""
        if not songs:
            return songs

        # Collect all unique folders that would be used for these tracks
        # We need to check each track's destination folder
        filtered = []
        for song in songs:
            track_id = clean_text(song.get("id") or song.get("perma_url", "").split("/")[-1])
            if not track_id:
                # Can't determine ID, keep the track
                filtered.append(song)
                continue

            # We need to check if this track ID exists anywhere in the output directory
            # Since we don't know the exact folder without processing the track,
            # we'll do a broad search across all subdirectories
            found = False
            try:
                for folder in self.output.iterdir():
                    if not folder.is_dir():
                        continue
                    for existing_file in folder.glob("*.m4a"):
                        existing_id = self._tagged_song_id(existing_file)
                        if existing_id == track_id:
                            found = True
                            break
                    if found:
                        break
            except OSError:
                pass

            if not found:
                filtered.append(song)

        return filtered

    def process_collection(self, kind, token):
        if kind not in ("album", "playlist"):
            raise DownloadError(f"Unsupported collection kind: {kind}. Expected 'album' or 'playlist'.")
        collection_started_at = time.monotonic()
        data = self.metadata(kind, token)
        songs = data.get("songs")
        if not isinstance(songs, list) or not songs:
            raise DownloadError("This collection has no available tracks.")
        name = clean_text(data.get("title") or data.get("listname")) or "Untitled"
        reported_total = len(songs)
        raw_reported_total = data.get("list_count")
        if isinstance(raw_reported_total, int) and raw_reported_total >= 0:
            reported_total = raw_reported_total
        elif isinstance(raw_reported_total, str) and raw_reported_total.strip().isdigit():
            reported_total = int(raw_reported_total)
        if kind == "playlist" and reported_total > len(songs):
            page_size = len(songs)
            max_pages = (reported_total + page_size - 1) // page_size

            def page_signature(items):
                """Create a stable signature for a page of items.
                Uses only stable identifiers (id, perma_url) to avoid false
                duplicates from dict repr differences."""
                sig = []
                for item in items:
                    if isinstance(item, dict):
                        # Prefer stable identifiers; skip items with neither
                        item_id = item.get("id") or item.get("perma_url")
                        if item_id:
                            sig.append(item_id)
                    # Items without stable IDs are ignored for deduplication
                return tuple(sig)

            first_sig = page_signature(songs)
            if not first_sig:
                # No stable IDs in first page; cannot reliably paginate
                self._warning("Warning: Cannot paginate playlist (no stable track IDs).")
            else:
                page_signatures = {first_sig}
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

        # Incremental sync: filter out tracks that already exist
        if self.incremental and kind == "playlist":
            original_count = len(songs)
            songs = self._filter_existing_tracks(songs, kind)
            if len(songs) < original_count:
                self._status(f"Incremental sync: {original_count - len(songs)} tracks already exist, {len(songs)} new tracks to download")
            if not songs:
                self._status("No new tracks to download.")
                return

        try:
            self._start_progress(len(songs), collection_started_at)
            with ThreadPoolExecutor(max_workers=min(self.workers, len(songs))) as executor:
                futures = []
                try:
                    for pos, song in enumerate(songs, 1):
                        futures.append(executor.submit(
                            self._process_collection_track,
                            kind, data, name, pos, len(songs), song))
                    for future in as_completed(futures):
                        future.result()
                except BaseException:
                    # Cancel queued tracks on interruption or an unexpected worker error.
                    # Running requests finish within their configured timeout before
                    # their sessions are closed in the outer finally block.
                    for future in futures:
                        future.cancel()
                    raise
        finally:
            self._finish_progress()
            with self._worker_sessions_lock:
                worker_sessions, self._worker_sessions = self._worker_sessions, []
            for worker_session in worker_sessions:
                worker_session.close()
            # Clear folder index cache to prevent memory leak across collections
            self._folder_ids.clear()

    def process_artist(self, artist_id):
        """Download all albums for an artist.

        Args:
            artist_id: Artist token from artist URL
        """
        collection_started_at = time.monotonic()
        page = 1
        all_albums = []

        # Fetch all pages of albums for the artist
        while True:
            data = self.artist_albums(artist_id, page=page)
            albums = data.get("albums") or data.get("data") or []

            if not isinstance(albums, list) or not albums:
                break

            all_albums.extend(albums)

            # Check if there are more pages
            if len(albums) < COLLECTION_PAGE_SIZE:
                break
            page += 1

        if not all_albums:
            raise DownloadError("This artist has no available albums.")

        artist_name = clean_text(all_albums[0].get("artist_name") or all_albums[0].get("artist") or "Unknown Artist")
        self._status(f"Artist: {artist_name} ({len(all_albums)} albums)")

        try:
            self._start_progress(len(all_albums), collection_started_at)
            with ThreadPoolExecutor(max_workers=min(self.workers, len(all_albums))) as executor:
                futures = []
                try:
                    for pos, album in enumerate(all_albums, 1):
                        album_id = album.get("id") or album.get("perma_url", "").split("/")[-1]
                        if not album_id:
                            continue
                        futures.append(executor.submit(
                            self.processAlbum, album_id))
                    for future in as_completed(futures):
                        future.result()
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
        finally:
            self._finish_progress()
            with self._worker_sessions_lock:
                worker_sessions, self._worker_sessions = self._worker_sessions, []
            for worker_session in worker_sessions:
                worker_session.close()
            self._folder_ids.clear()

    def processAlbum(self, album_id):
        self.process_collection("album", album_id)

    def processPlaylist(self, playlist_id):
        self.process_collection("playlist", playlist_id)
