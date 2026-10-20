# Contributing to PCM Notes

Thanks for helping improve PCM Notes. Bug reports, feature ideas, corrections to
the notes and code changes are all welcome. Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting bugs

Open a [bug report](https://github.com/apoorvdarshan/pcm-notes/issues/new?template=bug_report.yml).
Please include:

- what you did, step by step, so someone else can reproduce it;
- what you expected, and what happened instead;
- the app version (**About** in the app) and your device and iOS/iPadOS version,
  or the browser for a website problem;
- a screenshot or screen recording, if it helps.

Search [existing issues](https://github.com/apoorvdarshan/pcm-notes/issues?q=is%3Aissue)
first; adding detail to an existing report is more useful than a duplicate. Issues
stay public and searchable after they are closed.

**Security vulnerabilities do not go in issues.** Report them privately as
described in [SECURITY.md](SECURITY.md).

### Mistakes in the notes

For a mistake on a page (a wrong formula, a missing step, an unreadable or
misordered page), open a bug report naming the subject, chapter and page number.
The notes are handwritten by the author, so please describe the correction
rather than sending replacement pages.

## Requesting features

Open a [feature request](https://github.com/apoorvdarshan/pcm-notes/issues/new?template=feature_request.yml)
describing the problem you want solved, not only a solution. Requests are read and
answered, but PCM Notes stays a focused, offline, account-free reader, so not every
idea will fit.

## Contributing code

The project has one maintainer, who reviews and merges every pull request.

1. For anything bigger than a small fix, open an issue first so the approach can be
   agreed before you spend time on it.
2. Fork the repository and branch from `main`.
3. Keep each pull request to one focused change, and fill in the pull request
   template.
4. Add or update tests (see [Tests](#tests)) and make sure the checks pass.
5. Open the pull request against `main`. The `main` branch only changes through
   pull requests, and every required check must pass before a merge.

By contributing, you agree that your contribution is licensed under the
[MIT License](LICENSE), like the rest of the source code. The notes, images and
branding are not open source (see [NOTICE.md](NOTICE.md)), so please do not
submit note pages or artwork.

### Development setup

- **iOS app:** Xcode 26.6 or newer with an iOS 17.6 or newer simulator. Open
  `ios/PCMNotes/PCMNotes.xcodeproj`, choose the **PCMNotes** scheme, and run. The first
  build converts the note images into 91 chapter PDFs, which takes a few minutes;
  later builds reuse them.
- **Website:** Node.js. `npm install`, then `npm run build` writes the site to
  `dist/`. Open the HTML files directly to preview.
- **Scripts:** Python 3.11 or newer. The release tooling's pinned dependencies are
  in `requirements-store.txt`; the fuzzers' are in `fuzz/requirements.txt`.

### Code style

- **Swift:** follow the existing code and the
  [Swift API Design Guidelines](https://www.swift.org/documentation/api-design-guidelines/).
  Four-space indentation, SwiftUI views, value types where practical, and the
  project's default main-actor isolation. Avoid force unwrapping and `try!` in app
  code. Write doc comments for behaviour that is not obvious from the name.
- **Compiler warnings:** Debug builds of the app and its tests treat Swift and
  Clang warnings as errors, so the iOS tests fail on any new warning. Fix the cause
  rather than silencing the warning.
- **Python:** standard library first, type hints, and `argparse` command lines,
  matching `scripts/`. New third-party dependencies must be pinned with hashes.
- **Website:** plain HTML, CSS and JavaScript with no framework; keep pages fast
  and readable without JavaScript where possible.
- **Text:** British English for user-facing copy, matching the app.

### Tests

Every pull request that adds or changes functionality must add or update automated
tests that cover it, including invalid input and failure paths. Bug fixes should
add a test that fails without the fix.

| What changed | Tests to add or update | Run them |
|---|---|---|
| App logic | Swift Testing unit tests in `ios/PCMNotes/PCMNotesTests/` | `bash scripts/test-ios.sh` |
| User-visible flows | XCTest UI tests in `ios/PCMNotes/PCMNotesUITests/` | `bash scripts/test-ios.sh` |
| Release tooling | `scripts/store/test_ios_release.py` | `python3 -m unittest -v scripts/store/test_ios_release.py` |
| Test-result checker | `scripts/test_ios_test_summary.py` | `python3 -m unittest scripts/test_ios_test_summary.py` |
| Parsers of untrusted input | A fuzz target in `fuzz/` | `python3 -m unittest discover -s fuzz -p 'test_*.py'` |

`scripts/test-ios.sh` creates its own simulators and deletes them afterwards; see
[docs/ios-testing.md](docs/ios-testing.md). GitHub Actions runs all of these on
pull requests, so you can also rely on the checks on your pull request.

### Automated checks

Pull requests run, depending on the files changed:

- **iOS tests:** unit and UI tests on iPhone and iPad simulators;
- **Store release checks:** offline tests of the release tooling;
- **CodeQL:** static analysis of the Python, JavaScript and workflow code (the
  Swift app is analysed weekly and for each release tag);
- **Fuzzing:** the fuzz targets in `fuzz/` with Atheris;
- **Dependency review:** blocks newly added dependencies with known high-severity
  vulnerabilities.

Security alerts from CodeQL, fuzzing or Dependabot are fixed before a release.
[OpenSSF Scorecard](https://scorecard.dev/viewer/?uri=github.com/apoorvdarshan/pcm-notes)
checks the repository's security practices every week.

## Releases

Releases use [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`):
set `MARKETING_VERSION` to the new version, for example `1.0.1`, and tag the
release `v<MARKETING_VERSION>` (`v1.0.1`). Each release has human-readable notes in
[RELEASE_NOTES.md](RELEASE_NOTES.md). The release process is in
[store/README.md](store/README.md).
