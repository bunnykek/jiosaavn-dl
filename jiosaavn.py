#!/usr/bin/env python3
"""Command-line interface for jiosaavn-dl."""

import argparse
import math
from pathlib import Path
import sys

from mutagen import MutagenError

from jiosaavn_core import (
    API_URL,
    COLLECTION_PAGE_SIZE,
    COLLECTION_WORKERS,
    DownloadError,
    Jiosaavn,
    MAX_WORKERS,
    SHORT_HOSTS,
    SITE_HOSTS,
    __version__,
    clean_text,
    config_path,
    is_true,
    normalized_url,
    parse_url,
    resolve_url,
    safe_name,
    save_failed_urls,
    save_output,
    saved_output,
)


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


def positive_timeout(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number")
    return number


def worker_count(value):
    message = f"workers must be a whole number from 1 to {MAX_WORKERS}"
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(message) from exc
    if not 1 <= number <= MAX_WORKERS:
        raise argparse.ArgumentTypeError(message)
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
    parser.add_argument("--workers", type=worker_count, default=COLLECTION_WORKERS, metavar="N",
                        help=f"tracks to download at once from albums and playlists "
                             f"(1-{MAX_WORKERS}; lower it if you see rate-limit errors)")
    parser.add_argument("--overwrite", action="store_true", help="replace existing tracks")
    parser.add_argument("--no-lyrics", action="store_true", help="skip lyrics lookup")
    parser.add_argument("--no-cover", action="store_true", help="skip artwork download and embedding")
    parser.add_argument("--dry-run", action="store_true",
                        help="list planned downloads without fetching audio, covers, or lyrics")
    parser.add_argument("--failed-file", metavar="FILE",
                        help="save the URLs of failed items to FILE so `-i FILE` can retry them; "
                             "FILE is replaced on each run, and removed when nothing fails "
                             "(only if this option wrote it)")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--quiet", action="store_true",
                              help="suppress routine output and progress; errors are still shown")
    output_group.add_argument("--verbose", action="store_true",
                              help="show per-track details during downloads")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.urls and not args.input_file and args.set_output is None and not args.show_output:
        parser.error("provide at least one URL or --input-file")
    try:
        default_output = save_output(args.set_output) if args.set_output is not None else None
        if default_output is not None and not args.quiet:
            print(f"Default download folder saved: {default_output}")
        output = args.output if args.output is not None else (default_output or saved_output())
        if not output.strip():
            raise DownloadError("The output folder cannot be empty.")
        if args.show_output and not args.quiet:
            print(Path(output).expanduser().resolve())
    except (DownloadError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not args.urls and not args.input_file:
        return 0
    client = Jiosaavn(output, args.quality, args.timeout, args.overwrite,
                      not args.no_lyrics, not args.no_cover,
                      quiet=args.quiet, verbose=args.verbose, dry_run=args.dry_run,
                      workers=args.workers)
    client.progress_enabled = not args.quiet and sys.stderr.isatty()
    saved_failed = 0
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
                client.record_failure(url, exc, url)
                print(f"Error: {url}: {exc}", file=sys.stderr)
        if not found_url:
            raise DownloadError("The input lists contain no URLs.")
    except DownloadError as exc:
        client.record_failure("Input", exc)
        print(f"Error: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nInterrupted. Run the command again to retry unfinished tracks.", file=sys.stderr)
        return 130
    finally:
        client.session.close()
        if args.failed_file:
            try:
                saved_failed = save_failed_urls(args.failed_file, client.failures)
            except (DownloadError, OSError) as exc:
                print(f"Warning: could not save the failed list: {exc}", file=sys.stderr)
    if not args.quiet:
        if args.dry_run:
            print(f"\nFinished: {client.planned} planned, {client.skipped} skipped, {client.failed} failed.")
        else:
            print(f"\nFinished: {client.downloaded} downloaded, {client.skipped} skipped, {client.failed} failed.")
        if client.failures:
            print(f"\nFailed ({len(client.failures)}):")
            for failure in client.failures:
                print(f"  {failure.label}: {failure.reason}")
        if saved_failed:
            plural = "" if saved_failed == 1 else "s"
            print(f"\nSaved {saved_failed} failed URL{plural} to {args.failed_file}. "
                  f"Retry with: jiosaavn-dl -i {args.failed_file}")
    return 1 if client.failed else 0


if __name__ == "__main__":
    sys.exit(main())
