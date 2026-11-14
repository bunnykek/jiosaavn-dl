#!/usr/bin/env python3
"""
Weekly JioSaavn API Compatibility Smoke Test

Tests song/album/playlist metadata retrieval and audio URL generation
without downloading audio files. Exits with non-zero code on failures.
"""

import sys
import os
import json
import logging
from typing import Dict, List, Optional, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jiosaavn_core as core


class SmokeTest:
    """Runs smoke tests against the live JioSaavn API."""

    # Test URLs - these should be stable, popular items
    # Note: tokens may expire; update if tests start failing
    # Song: "Tum Hi Ho" from Aashiqui 2 (perma_url token)
    # Album: "Aashiqui 2" (album_url token)
    # Playlist: "Viral Nation" featured playlist (perma_url token)
    TEST_URLS = {
        "song": "https://www.jiosaavn.com/song/tum-hi-ho/EToxUyFpcwQ",
        "album": "https://www.jiosaavn.com/album/aashiqui-2/-iNdCmFNV9o_",
        "playlist": "https://www.jiosaavn.com/featured/viral-nation/tfVkYjaAbZJieSJqt9HmOQ__",
    }

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.client = core.Jiosaavn(
            output="/tmp",  # Won't be used in dry-run
            quality="320",
            timeout=30,
            dry_run=True,  # Critical: no audio downloads
            cover=False,
            lyrics=False,
            quiet=not verbose,
        )
        self.results: Dict[str, Dict] = {}
        self.failures: List[str] = []

        # Configure logging
        logging.basicConfig(
            level=logging.DEBUG if verbose else logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s"
        )
        self.logger = logging.getLogger(__name__)

    def _log(self, level: int, msg: str):
        """Log message at given level."""
        if level >= logging.INFO or self.verbose:
            self.logger.log(level, msg)

    def test_song_metadata(self, token: str) -> Tuple[bool, Optional[Dict]]:
        """Test song metadata retrieval and audio URL generation."""
        self._log(logging.INFO, f"Testing song metadata for token: {token}")
        try:
            # Get song metadata
            metadata = self.client.metadata("song", token)
            self._log(logging.DEBUG, f"Song metadata keys: {list(metadata.keys())}")

            # Song API returns songs array
            songs = metadata.get("songs", [])
            if not isinstance(songs, list) or not songs:
                raise ValueError("No songs in metadata response")
            
            song = songs[0]

            # Validate required fields
            required_fields = ["song", "album", "year", "primary_artists", "encrypted_media_url", "id"]
            for field in required_fields:
                if field not in song:
                    raise ValueError(f"Missing required field: {field}")

            # Test audio URL generation
            encurl = song.get("encrypted_media_url")
            if not encurl:
                raise ValueError("No encrypted_media_url in metadata")

            audio_url = self.client.getCdnURL(encurl)
            self._log(logging.DEBUG, f"Generated audio URL: {audio_url}")

            # Validate audio URL
            if not audio_url.startswith(("https://", "http://")):
                raise ValueError(f"Invalid audio URL format: {audio_url}")

            # Verify it's an AAC URL (not web stream)
            if "web.aac" in audio_url or "web" in audio_url:
                self._log(logging.WARNING, f"Audio URL may be web stream: {audio_url}")

            result = {
                "success": True,
                "title": song.get("song"),
                "album": song.get("album"),
                "artist": song.get("primary_artists"),
                "year": song.get("year"),
                "song_id": song.get("id"),
                "audio_url": audio_url,
                "has_lyrics": song.get("has_lyrics") == "true",
                "explicit": song.get("explicit_content") == "1",
            }
            return True, result

        except Exception as e:
            self._log(logging.ERROR, f"Song test failed: {e}")
            return False, {"error": str(e)}

    def test_album_metadata(self, token: str) -> Tuple[bool, Optional[Dict]]:
        """Test album metadata retrieval and track listing."""
        self._log(logging.INFO, f"Testing album metadata for token: {token}")
        try:
            metadata = self.client.metadata("album", token)
            self._log(logging.DEBUG, f"Album metadata keys: {list(metadata.keys())}")

            # Validate required fields
            required_fields = ["title", "songs"]
            for field in required_fields:
                if field not in metadata:
                    raise ValueError(f"Missing required field: {field}")

            songs = metadata.get("songs", [])
            if not isinstance(songs, list):
                raise ValueError("Songs field is not a list")

            if not songs:
                self._log(logging.WARNING, "Album has no songs")

            # Validate first song has required fields
            if songs:
                first_song = songs[0]
                song_required = ["song", "album", "year", "primary_artists", "encrypted_media_url", "id"]
                for field in song_required:
                    if field not in first_song:
                        self._log(logging.WARNING, f"First song missing field: {field}")

                # Test audio URL generation for first song
                encurl = first_song.get("encrypted_media_url")
                if encurl:
                    audio_url = self.client.getCdnURL(encurl)
                    self._log(logging.DEBUG, f"First song audio URL: {audio_url}")

            result = {
                "success": True,
                "title": metadata.get("title"),
                "year": metadata.get("year"),
                "primary_artists": metadata.get("primary_artists"),
                "track_count": len(songs),
                "first_song": songs[0].get("song") if songs else None,
            }
            return True, result

        except Exception as e:
            self._log(logging.ERROR, f"Album test failed: {e}")
            return False, {"error": str(e)}

    def test_playlist_metadata(self, token: str) -> Tuple[bool, Optional[Dict]]:
        """Test playlist metadata retrieval and track listing."""
        self._log(logging.INFO, f"Testing playlist metadata for token: {token}")
        try:
            metadata = self.client.metadata("playlist", token)
            self._log(logging.DEBUG, f"Playlist metadata keys: {list(metadata.keys())}")

            # Playlist uses different field names: listname instead of title, content_list instead of songs
            # Validate required fields (accept both naming conventions)
            title = metadata.get("title") or metadata.get("listname")
            songs = metadata.get("songs") or metadata.get("content_list")
            
            if not title:
                raise ValueError("Missing required field: title/listname")
            if songs is None:
                raise ValueError("Missing required field: songs/content_list")
            
            if not isinstance(songs, list):
                raise ValueError("Songs field is not a list")

            if not songs:
                self._log(logging.WARNING, "Playlist has no songs")

            # Validate first song
            if songs:
                first_song = songs[0]
                song_required = ["song", "album", "year", "primary_artists", "encrypted_media_url", "id"]
                for field in song_required:
                    if field not in first_song:
                        self._log(logging.WARNING, f"First song missing field: {field}")

                # Test audio URL generation for first song
                encurl = first_song.get("encrypted_media_url")
                if encurl:
                    audio_url = self.client.getCdnURL(encurl)
                    self._log(logging.DEBUG, f"First song audio URL: {audio_url}")

            result = {
                "success": True,
                "title": title,
                "track_count": len(songs),
                "first_song": songs[0].get("song") if songs else None,
            }
            return True, result

        except Exception as e:
            self._log(logging.ERROR, f"Playlist test failed: {e}")
            return False, {"error": str(e)}

    def run_all_tests(self) -> int:
        """Run all smoke tests. Returns exit code (0 = success, 1 = failure)."""
        print("=" * 60)
        print("JioSaavn API Compatibility Smoke Test")
        print("=" * 60)
        print()

        all_passed = True

        # Test song
        print("Testing song metadata...")
        url = self.TEST_URLS["song"]
        kind, token = core.parse_url(url)
        success, result = self.test_song_metadata(token)
        self.results["song"] = result
        if success:
            print(f"  ✓ Song: {result.get('title')} — {result.get('artist')} ({result.get('year')})")
            print(f"    Audio URL: {result.get('audio_url')[:80]}...")
        else:
            print(f"  ✗ Song test failed: {result.get('error')}")
            self.failures.append(f"Song: {result.get('error')}")
            all_passed = False
        print()

        # Test album
        print("Testing album metadata...")
        url = self.TEST_URLS["album"]
        kind, token = core.parse_url(url)
        success, result = self.test_album_metadata(token)
        self.results["album"] = result
        if success:
            print(f"  ✓ Album: {result.get('title')} ({result.get('track_count')} tracks)")
            print(f"    First track: {result.get('first_song')}")
        else:
            print(f"  ✗ Album test failed: {result.get('error')}")
            self.failures.append(f"Album: {result.get('error')}")
            all_passed = False
        print()

        # Test playlist
        print("Testing playlist metadata...")
        url = self.TEST_URLS["playlist"]
        kind, token = core.parse_url(url)
        success, result = self.test_playlist_metadata(token)
        self.results["playlist"] = result
        if success:
            print(f"  ✓ Playlist: {result.get('title')} ({result.get('track_count')} tracks)")
            print(f"    First track: {result.get('first_song')}")
        else:
            print(f"  ✗ Playlist test failed: {result.get('error')}")
            self.failures.append(f"Playlist: {result.get('error')}")
            all_passed = False
        print()

        # Summary
        print("=" * 60)
        if all_passed:
            print("All smoke tests PASSED ✓")
        else:
            print("Some smoke tests FAILED ✗")
            for failure in self.failures:
                print(f"  - {failure}")
        print("=" * 60)

        return 0 if all_passed else 1


def main():
    """Main entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="JioSaavn API Compatibility Smoke Test")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    test = SmokeTest(verbose=args.verbose)
    exit_code = test.run_all_tests()

    if args.json:
        print(json.dumps(test.results, indent=2))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()