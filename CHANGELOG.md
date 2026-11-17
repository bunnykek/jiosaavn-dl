# Changelog

Versions follow [semantic versioning](https://semver.org/). Releases are tagged
`vX.Y.Z`; the version number is defined once, as `__version__` in
`jiosaavn_core.py`. For anything older than 0.5.0, see the git history.

## Unreleased

### Added
- `--genre TEXT` writes a genre tag on every track downloaded in that run,
  replacing any genre JioSaavn returns (which is not always present). Without
  it, the service's genre is tagged when there is one. Blank values are
  rejected. Tracks that already exist are skipped with their tags unchanged,
  so add `--overwrite` to retag them.
- `--template PATH` organizes tracks into a custom folder layout using the
  placeholders `{artist}`, `{album}`, `{title}`, `{track}`, `{year}`,
  `{genre}`, `{quality}`, and `{playlist}`, for example
  `{artist}/{album}/{track:02d} - {title}`. Each path component is sanitized
  separately, so a placeholder cannot escape the output directory. `{track}`
  is the collection position and accepts an integer format such as
  `{track:02d}`. Unknown or unavailable placeholders fail loudly instead of
  passing through. The default layout is unchanged.
- Richer, consistent tagging: all genres/categories from JioSaavn are mapped
  when available (joined with `; `) unless `--genre` overrides them; composer
  and lyricist credits, record label, language, performers, ISRC, disc/track
  numbers, and normalized release dates are written consistently.
- `--save-lyrics-file` exports lyrics alongside each track as a `.lrc` file
  (synchronized/LRC when the service provides timing, otherwise plain text);
  embedded lyrics use the plain text.
- The interactive progress bar shows the aggregate download speed across all
  active tracks (in MB/s or GB/s), the elapsed collection time as `H:MM:SS`,
  and an estimated time remaining (`ETA`) once enough tracks have finished to
  base the estimate on. Resumed bytes and bytes from tracks that fail or are
  interrupted are excluded from the speed and ETA, so a stalled or interrupted
  collection shows the rate of what actually completed. These fields only
  appear on the interactive bar; plain per-track lines printed when output is
  piped or redirected are unchanged.

## 0.5.0 - 2026-11-14

### Added
- `--workers N` (1 to 16) sets how many tracks of a collection download at once.
  The default is unchanged at 4. Lower it if JioSaavn starts rate limiting or
  your connection is slow.
- Failed tracks are listed in the final summary with the reason, and
  `--failed-file FILE` saves their URLs so `jiosaavn-dl -i FILE` can retry them.
- The progress line shows the titles being downloaded, and each finished track
  is printed above it as downloaded, skipped or failed.
- The progress line also shows how much of each track has arrived: a
  percentage, or megabytes when the server does not report a size.

### Changed
- Tagged song IDs are read once per output folder instead of once per track,
  which removes a slowdown that grew with the size of the collection.
- A song listed more than once in a collection is downloaded once; the repeats
  count as skipped. Before, the outcome depended on timing.
- Partial downloads are named with the requested quality (for example
  `.320.part`) and only resumed at the same quality, so changing `--quality`
  between runs can no longer splice two different files together.
- Continuous integration runs on Ubuntu 24.04 with current GitHub Actions.

### Fixed
- Overwrite matching and cancelling a collection download, plus run-level
  duplicate suppression and executor shutdown when submitting tracks is
  interrupted (pull request #1, "reliability repair order").
