# Changelog

Versions follow [semantic versioning](https://semver.org/). Releases are tagged
`vX.Y.Z`; the version number is defined once, as `__version__` in
`jiosaavn_core.py`. For anything older than 0.5.0, see the git history.

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
