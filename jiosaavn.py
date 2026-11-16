#!/usr/bin/env python3
"""Command-line interface for jiosaavn-dl."""

import argparse
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    TEMPLATE_PLACEHOLDERS,
    __version__,
    clean_text,
    config_path,
    is_true,
    load_config,
    normalize_genre,
    normalized_url,
    parse_url,
    resolve_url,
    render_template,
    reset_config,
    safe_name,
    save_config,
    save_failed_urls,
    save_output,
    saved_output,
)


def template_text(value):
    """Validate an output template, returning it unchanged."""
    render_template(value, {name: "" for name in TEMPLATE_PLACEHOLDERS})
    return value


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


def genre_text(value):
    genre = normalize_genre(value)
    if genre is None:
        raise argparse.ArgumentTypeError("genre cannot be blank")
    return genre


def speed_limit(value):
    """Parse a speed limit like '5M', '500K', '1000' into bytes/sec."""
    value = value.strip().upper()
    if not value:
        raise argparse.ArgumentTypeError("speed limit cannot be blank")
    multipliers = {"K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}
    if value[-1] in multipliers:
        try:
            num = float(value[:-1])
            return int(num * multipliers[value[-1]])
        except ValueError:
            raise argparse.ArgumentTypeError("invalid speed limit format (e.g. 5M, 500K, 1000)")
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid speed limit format (e.g. 5M, 500K, 1000)")


def interactive_setup(client, input_fn=None, output_fn=None):
    """Search for downloadable items and confirm run settings interactively.

    Returns the selected result URLs, or None when the user cancels.
    """
    input_fn = input_fn or input
    output_fn = output_fn or print

    while True:
        query = input_fn("Search JioSaavn (or q to cancel): ").strip()
        if query.lower() in ("q", "quit", "cancel"):
            output_fn("Cancelled.")
            return None
        if not query:
            output_fn("Enter a search query, or q to cancel.")
            continue
        results = client.search(query)
        if results:
            break
        output_fn("No supported songs, albums, or playlists found. Try another query.")

    output_fn("Search results:")
    for index, result in enumerate(results, 1):
        subtitle = f" — {result['subtitle']}" if result.get("subtitle") else ""
        output_fn(f"  {index}. [{result['kind']}] {result['title']}{subtitle}")

    while True:
        choice = input_fn("Select one or more (comma-separated, a=all, q=cancel) [1]: ").strip()
        if choice.lower() in ("q", "quit", "cancel"):
            output_fn("Cancelled.")
            return None
        if not choice:
            selected_indices = [0]
            break
        if choice.lower() in ("a", "all"):
            selected_indices = list(range(len(results)))
            break
        try:
            numbers = [int(value.strip()) for value in choice.split(",")]
            if (not numbers or len(set(numbers)) != len(numbers)
                    or any(number < 1 or number > len(results) for number in numbers)):
                raise ValueError
        except ValueError:
            output_fn(f"Choose numbers from 1 to {len(results)}, a for all, or q to cancel.")
            continue
        selected_indices = [number - 1 for number in numbers]
        break

    selected = [results[index] for index in selected_indices]
    current_output = str(client.output)
    while True:
        entered_output = input_fn(f"Output folder [{current_output}] (q=cancel): ").strip()
        if entered_output.lower() in ("q", "quit", "cancel"):
            output_fn("Cancelled.")
            return None
        chosen_output = entered_output or current_output
        if not chosen_output.strip():
            output_fn("The output folder cannot be empty.")
            continue
        output_path = Path(chosen_output).expanduser()
        if output_path.exists() and not output_path.is_dir():
            output_fn(f"Output path is a file: {output_path}")
            continue
        break

    while True:
        quality = input_fn(f"Audio quality in kbps ({'/'.join(('12', '48', '96', '160', '320'))}) "
                           f"[{client.quality}]: ").strip()
        if not quality:
            break
        if quality in ("12", "48", "96", "160", "320"):
            client.quality = quality
            break
        output_fn("Choose 12, 48, 96, 160, or 320 kbps.")

    default_overwrite = "Y/n" if client.overwrite else "y/N"
    while True:
        overwrite = input_fn(f"Replace existing tracks? [{default_overwrite}]: ").strip().lower()
        if not overwrite:
            break
        if overwrite in ("y", "yes"):
            client.overwrite = True
            break
        if overwrite in ("n", "no"):
            client.overwrite = False
            break
        output_fn("Enter y or n.")

    client.output = output_path
    output_fn("\nReady to download:")
    for result in selected:
        output_fn(f"  [{result['kind']}] {result['title']}")
    output_fn(f"Output: {client.output}")
    output_fn(f"Quality: {client.quality} kbps")
    output_fn(f"Replace existing tracks: {'yes' if client.overwrite else 'no'}")
    if input_fn("Start download? [y/N]: ").strip().lower() not in ("y", "yes"):
        output_fn("Cancelled.")
        return None
    return [result["url"] for result in selected]


def build_parser():
        # Load saved config for defaults
        try:
            cfg = load_config()
        except DownloadError:
            # Fall back to defaults if config is invalid
            cfg = {
                "output": "Downloads",
                "quality": "320",
                "workers": COLLECTION_WORKERS,
                "genre": None,
                "template": None,
                "overwrite": False,
                "no_lyrics": False,
                "no_cover": False,
                "timeout": 30,
                "max_speed": None,
                "proxy": None,
            }
        parser = argparse.ArgumentParser(
            prog="jiosaavn-dl",
            description="Download JioSaavn songs, albums, and playlists with metadata and artwork.",
            epilog="Example: jiosaavn-dl 'https://www.jiosaavn.com/album/album-name/token' -o ~/Music",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        parser.add_argument("urls", nargs="*", metavar="URL", help="song, album, playlist, or artist URLs (can be mixed)")
        parser.add_argument("-I", "--interactive", action="store_true",
                            help="search for items and confirm download settings interactively")
        parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
        parser.add_argument("-i", "--input-file", action="append", default=[], metavar="FILE",
                            help="read one URL per line; repeat for multiple files, or use - for stdin")
        parser.add_argument("-o", "--output", metavar="PATH",
                            help="folder for this run; otherwise use the saved folder or ./Downloads")
        parser.add_argument("--set-output", metavar="PATH", help="save a default download folder for future runs")
        parser.add_argument("--show-output", action="store_true", help="show the effective download folder")
        parser.add_argument("-q", "--quality", choices=("12", "48", "96", "160", "320"), default=cfg["quality"],
                            help="requested bitrate in kbps (availability depends on the track)")
        parser.add_argument("--timeout", type=positive_timeout, default=cfg["timeout"], help="timeout per network request in seconds")
        parser.add_argument("--workers", type=worker_count, default=cfg["workers"], metavar="N",
                            help=f"tracks to download at once from albums and playlists "
                                 f"(1-{MAX_WORKERS}; lower it if you see rate-limit errors)")
        parser.add_argument("--genre", type=genre_text, default=cfg["genre"], metavar="TEXT",
                            help="write TEXT as the genre tag of every track downloaded in this run "
                                 "(replaces any genre JioSaavn returns)")
        parser.add_argument("--template", type=template_text, default=cfg["template"], metavar="PATH",
                            help="organize tracks into a custom folder layout using "
                                 "{artist}, {album}, {title}, {track}, {year}, {genre}, "
                                 "{quality}, and {playlist}, e.g. {artist}/{album}/{track:02d} - {title}")
        parser.add_argument("--overwrite", action="store_true", default=cfg["overwrite"], help="replace existing tracks")
        parser.add_argument("--no-lyrics", action="store_true", default=cfg["no_lyrics"], help="skip lyrics lookup")
        parser.add_argument("--no-cover", action="store_true", default=cfg["no_cover"], help="skip artwork download and embedding")
        parser.add_argument("--dry-run", action="store_true",
                            help="list planned downloads without fetching audio, covers, or lyrics")
        parser.add_argument("--failed-file", metavar="FILE",
                            help="save the URLs of failed items to FILE so `-i FILE` can retry them; "
                                 "FILE is replaced on each run, and removed when nothing fails "
                                 "(only if this option wrote it)")

        # New options
        parser.add_argument("--max-speed", metavar="BYTES", type=speed_limit,
                            default=cfg["max_speed"],
                            help="max download speed (e.g. 5M, 500K, 1000); no limit by default")
        parser.add_argument("--proxy", metavar="URL", default=cfg["proxy"],
                            help="proxy URL (e.g. socks5://127.0.0.1:1080, http://proxy:8080)")
        parser.add_argument("--incremental", action="store_true",
                            help="incremental playlist sync: only download tracks not already present "
                                 "(uses JioSaavn song ID to detect existing tracks)")
        parser.add_argument("--fallback-quality", metavar="Kbps", action="append",
                            choices=("320", "160", "96", "48", "12"),
                            help="fallback quality to try if requested quality is unavailable "
                                 "(can be used multiple times, e.g. --fallback-quality 160 --fallback-quality 96)")
        parser.add_argument("--export-playlist", metavar="FORMAT", choices=("m3u", "m3u8", "pls"),
                            help="export playlist to M3U/M3U8/PLS file after download (only for playlists)")
        parser.add_argument("--save-lyrics-file", action="store_true",
                            help="save lyrics as separate .lrc file alongside the audio file")
        parser.add_argument("--save-cover-file", action="store_true",
                            help="save cover art as folder.jpg in the album/playlist folder")
        parser.add_argument("--parallel-urls", type=int, default=1, metavar="N",
                            help="process multiple URLs concurrently (1-4, default 1)")
        parser.add_argument("--post-hook", metavar="CMD",
                            help="run CMD after each successful download (receives file path as argument)")
        parser.add_argument("--no-explicit", action="store_true",
                            help="skip tracks marked as explicit content")
        parser.add_argument("--after-year", type=int, metavar="YEAR",
                            help="only download tracks released after YEAR (inclusive)")
        parser.add_argument("--before-year", type=int, metavar="YEAR",
                            help="only download tracks released before YEAR (inclusive)")
        parser.add_argument("--search", metavar="QUERY",
                            help="search for songs/albums/playlists and download matching results")
        parser.add_argument("--search-limit", type=int, default=10, metavar="N",
                            help="number of search results to consider (default 10)")
        parser.add_argument("--search-type", choices=("song", "album", "playlist", "all"),
                            default="all", help="type of results to search for")

        output_group = parser.add_mutually_exclusive_group()
        output_group.add_argument("--quiet", action="store_true",
                                  help="suppress routine output and progress; errors are still shown")
        output_group.add_argument("--verbose", action="store_true",
                                  help="show per-track details during downloads")
        return parser


def config_show():
        """Print all saved configuration."""
        try:
            cfg = load_config()
        except (DownloadError, OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        for key, value in cfg.items():
            if key == "output" and value:
                value = str(Path(value).expanduser().resolve())
            print(f"{key}: {value}")
        return 0


def config_set(key, value):
        """Save a configuration key."""
        try:
            # Validate key
            valid_keys = {"output", "quality", "workers", "genre", "template",
                          "overwrite", "no_lyrics", "no_cover", "timeout",
                          "max_speed", "proxy"}
            if key not in valid_keys:
                print(f"Unknown config key: {key}. Valid keys: {', '.join(sorted(valid_keys))}", file=sys.stderr)
                return 2
            # Convert value based on key type
            if key in ("overwrite", "no_lyrics", "no_cover"):
                if value.lower() in ("true", "1", "yes", "on"):
                    value = True
                elif value.lower() in ("false", "0", "no", "off"):
                    value = False
                else:
                    print(f"Boolean value expected for {key} (true/false)", file=sys.stderr)
                    return 2
            elif key == "workers":
                value = int(value)
                if not 1 <= value <= MAX_WORKERS:
                    print(f"workers must be 1-{MAX_WORKERS}", file=sys.stderr)
                    return 2
            elif key == "quality":
                if value not in ("12", "48", "96", "160", "320"):
                    print("quality must be 12, 48, 96, 160, or 320", file=sys.stderr)
                    return 2
            elif key == "timeout":
                value = float(value)
                if not math.isfinite(value) or value <= 0:
                    print("timeout must be a positive number", file=sys.stderr)
                    return 2
            elif key == "max_speed":
                if value.lower() in ("none", "0", "unlimited"):
                    value = None
                else:
                    value = speed_limit(value)
            elif key == "genre":
                value = normalize_genre(value)
            elif key == "template":
                # Validate template
                render_template(value, {name: "" for name in TEMPLATE_PLACEHOLDERS})
            elif key == "proxy":
                if value.lower() in ("none", ""):
                    value = None
            save_config(**{key: value})
            print(f"Config saved: {key} = {value}")
        except (DownloadError, OSError, ValueError, argparse.ArgumentTypeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0


def config_reset():
        """Remove the saved configuration."""
        previous = reset_config()
        print(f"Removed all saved configuration (output was: {previous}). "
              f"Downloads will use defaults again.")
        return 0


CONFIG_COMMANDS = {"show": config_show, "set": config_set, "reset": config_reset}


def handle_config(argv):
        """Dispatch a `config` subcommand. `argv` excludes the `config` token."""
        if not argv:
            print("Usage: jiosaavn-dl config show|set KEY VALUE|reset", file=sys.stderr)
            return 2
        name, *rest = argv
        handler = CONFIG_COMMANDS.get(name)
        if handler is None:
            print(f"Unknown config command: {name}. "
                  f"Choose from {', '.join(sorted(CONFIG_COMMANDS))}.", file=sys.stderr)
            return 2
        if name == "set":
            if len(rest) != 2:
                print("Usage: jiosaavn-dl config set KEY VALUE", file=sys.stderr)
                return 2
            return handler(rest[0], rest[1])
        if rest:
            print(f"jiosaavn-dl config {name} takes no arguments", file=sys.stderr)
            return 2
        return handler()


def export_playlist(client, playlist_token, format_type, output_dir):
    """Export a playlist to M3U/M3U8/PLS format."""
    from pathlib import Path
    import urllib.parse

    try:
        data = client.metadata("playlist", playlist_token)
    except DownloadError as exc:
        print(f"Warning: Could not fetch playlist metadata for export: {exc}", file=sys.stderr)
        return

    name = clean_text(data.get("title") or data.get("listname")) or "Untitled"
    songs = data.get("songs", [])

    if not songs:
        print("Warning: No tracks in playlist to export")
        return

    # Sanitize playlist name for filename
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    if not safe_name:
        safe_name = "playlist"

    # Determine file extension
    ext = format_type if format_type != "m3u8" else "m3u"
    playlist_file = Path(output_dir) / f"{safe_name}.{ext}"

    try:
        if format_type in ("m3u", "m3u8"):
            _export_m3u(playlist_file, songs, name)
        elif format_type == "pls":
            _export_pls(playlist_file, songs, name)
        print(f"Exported playlist to {playlist_file}")
    except OSError as exc:
        print(f"Warning: Could not write playlist file: {exc}", file=sys.stderr)


def _export_m3u(path, songs, playlist_name):
    """Export playlist to M3U/M3U8 format."""
    lines = ["#EXTM3U", f"#PLAYLIST:{playlist_name}"]
    for song in songs:
        title = clean_text(song.get("song", "Unknown"))
        artist = clean_text(song.get("primary_artists", "Unknown Artist"))
        duration = song.get("duration", "0")
        try:
            duration = int(duration)
        except (ValueError, TypeError):
            duration = 0
        # Find the actual file path
        # This is a simplified approach - in reality we'd need to track downloaded files
        # For now, use a placeholder
        lines.append(f"#EXTINF:{duration},{artist} - {title}")
        lines.append(f"{title} - {artist}.m4a")  # Placeholder
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _export_pls(path, songs, playlist_name):
    """Export playlist to PLS format."""
    lines = ["[playlist]", f"NumberOfEntries={len(songs)}", f"Title={playlist_name}"]
    for i, song in enumerate(songs, 1):
        title = clean_text(song.get("song", "Unknown"))
        artist = clean_text(song.get("primary_artists", "Unknown Artist"))
        duration = song.get("duration", "0")
        try:
            duration = int(duration)
        except (ValueError, TypeError):
            duration = -1
        lines.append(f"File{i}={title} - {artist}.m4a")
        lines.append(f"Title{i}={artist} - {title}")
        lines.append(f"Length{i}={duration}")
    lines.append("Version=2")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _process_url(client, url, args, output):
    """Process a single URL (helper for parallel URL processing)."""
    kind, token = resolve_url(url, client)
    if kind == "song":
        client.processTrack(token)
    elif kind == "artist":
        client.process_artist(token)
    else:
        client.process_collection(kind, token)
    # Export playlist if requested and this was a playlist
    if args.export_playlist and kind == "playlist":
        export_playlist(client, token, args.export_playlist, args.output or output)


def main(argv=None):
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.urls and args.urls[0] == "config":
            return handle_config(args.urls[1:])
        if args.interactive and (args.urls or args.input_file):
            parser.error("--interactive cannot be combined with URLs or --input-file")
        if args.interactive and not sys.stdin.isatty():
            print("Error: --interactive requires an interactive terminal (stdin must be a TTY).",
                  file=sys.stderr)
            return 2
        if not args.urls and not args.input_file and not args.interactive and args.set_output is None and not args.show_output:
            parser.error("provide at least one URL or --input-file")
        try:
            default_output = save_output(args.set_output) if args.set_output is not None else None
            if default_output is not None and not args.quiet:
                print(f"Default download folder saved: {default_output}")
            if args.output is not None:
                output = args.output
            else:
                if default_output is not None:
                    output = default_output
                else:
                    output = saved_output()
            if not output or not output.strip():
                raise DownloadError("The output folder cannot be empty.")
            if args.show_output and not args.quiet:
                print(Path(output).expanduser().resolve())
        except (DownloadError, OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if not args.urls and not args.input_file and not args.interactive and not args.search:
            return 0
        client = Jiosaavn(output, args.quality, args.timeout, args.overwrite,
                          not args.no_lyrics, not args.no_cover,
                          quiet=args.quiet, verbose=args.verbose, dry_run=args.dry_run,
                          workers=args.workers, genre=args.genre, template=args.template,
                          max_speed=args.max_speed, proxy=args.proxy,
                          incremental=args.incremental,
                          fallback_qualities=args.fallback_quality,
                          save_lyrics_file=args.save_lyrics_file,
                          save_cover_file=args.save_cover_file,
                          post_hook=args.post_hook,
                          no_explicit=args.no_explicit,
                          after_year=args.after_year,
                          before_year=args.before_year)
        client.progress_enabled = not args.quiet and sys.stderr.isatty()
        saved_failed = 0
        try:
            if args.interactive:
                urls = interactive_setup(client)
                if urls is None:
                    return 0
            else:
                urls = list(input_urls(args.urls, args.input_file))

            # Handle search and download
            if args.search:
                search_results = client.search(args.search, limit=args.search_limit)
                # Filter by type if specified
                if args.search_type != "all":
                    search_results = [r for r in search_results if r["kind"] == args.search_type]
                if not search_results:
                    print(f"No {args.search_type} results found for: {args.search}")
                else:
                    print(f"Found {len(search_results)} {args.search_type} result(s) for: {args.search}")
                    for i, result in enumerate(search_results, 1):
                        print(f"  {i}. [{result['kind']}] {result['title']} — {result['subtitle']}")
                    # Ask for confirmation unless in non-interactive mode
                    if not args.interactive and sys.stdin.isatty():
                        confirm = input("Download these? [y/N]: ").strip().lower()
                        if confirm not in ("y", "yes"):
                            print("Cancelled.")
                            return 0
                    # Add search result URLs to download queue
                    for result in search_results:
                        urls.append(result["url"])

            if not urls:
                raise DownloadError("The input lists contain no URLs.")

            # Process URLs in parallel if requested
            if args.parallel_urls > 1 and len(urls) > 1:
                with ThreadPoolExecutor(max_workers=min(args.parallel_urls, len(urls))) as executor:
                    futures = {}
                    for url in urls:
                        future = executor.submit(_process_url, client, url, args, output)
                        futures[future] = url
                    for future in as_completed(futures):
                        url = futures[future]
                        try:
                            future.result()
                        except (DownloadError, OSError, MutagenError, ValueError, TypeError, KeyError) as exc:
                            client.record_failure(url, exc, url)
                            print(f"Error: {url}: {exc}", file=sys.stderr)
            else:
                for url in urls:
                    try:
                        kind, token = resolve_url(url, client)
                        if kind == "song":
                            client.processTrack(token)
                        elif kind == "artist":
                            client.process_artist(token)
                        else:
                            client.process_collection(kind, token)
                        # Export playlist if requested and this was a playlist
                        if args.export_playlist and kind == "playlist":
                            export_playlist(client, token, args.export_playlist, args.output or output)
                    except (DownloadError, OSError, MutagenError, ValueError, TypeError, KeyError) as exc:
                        client.record_failure(url, exc, url)
                        print(f"Error: {url}: {exc}", file=sys.stderr)
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
