<p align="center">
  <img src="ios/PCMNotes/PCMNotes/Assets.xcassets/AppIcon.appiconset/AppIcon.png" alt="PCM Notes" width="160" height="160" />
</p>

<h1 align="center">PCM Notes</h1>

<p align="center"><em>Handwritten exam revision.</em></p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-iOS%20%7C%20iPadOS-091526?style=flat-square" />
  <img alt="iOS" src="https://img.shields.io/badge/iOS-17.6%2B-2f88ff?style=flat-square" />
  <img alt="Swift" src="https://img.shields.io/badge/Swift-5-f05138?style=flat-square" />
  <img alt="Reader" src="https://img.shields.io/badge/reader-PDFKit-9b62ff?style=flat-square" />
  <img alt="Archive" src="https://img.shields.io/badge/archive-91%20PDFs-22b983?style=flat-square" />
  <img alt="License: MIT (code)" src="https://img.shields.io/badge/license-MIT%20(code)-f2b632?style=flat-square" />
</p>

<p align="center">
  <a href="https://www.bestpractices.dev/projects/15501"><img src="https://www.bestpractices.dev/projects/15501/badge" alt="OpenSSF Best Practices" /></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/apoorvdarshan/pcm-notes"><img src="https://api.scorecard.dev/projects/github.com/apoorvdarshan/pcm-notes/badge" alt="OpenSSF Scorecard" /></a>
</p>

---

**PCM Notes** turns a large handwritten Physics, Chemistry, and Mathematics archive into a focused native reader for iPhone and iPad. Find a chapter, open its complete PDF, and keep revising without waiting for a connection.

Physics and Chemistry support JEE and NEET revision. Mathematics is organised for JEE Main and Advanced. Biology is not included.

> Less hunting. More revising.

<p align="center">
  <img src="assets/og-image.png" alt="PCM Notes handwritten Physics, Chemistry and Mathematics revision" width="900" />
</p>

## ✏️ Inside the notebook

| Subject | Chapters | Pages | Revision focus |
|---------|---------:|------:|----------------|
| ⚛️ Physics | 23 | 539 | JEE + NEET |
| 🧪 Chemistry | 37 | 993 | JEE + NEET |
| 📐 Mathematics | 31 | 641 | JEE Main + Advanced |
| **Total** | **91** | **2,173** | Complete handwritten archive |

## ✨ Features

- **Fully offline archive** — all 91 chapter PDFs are bundled with the app; reading and zooming never depend on the website.
- **Full-screen focus reader** — chapters open directly from the list into a clean, distraction-free PDF view.
- **Reading progress** — each chapter remembers the last page and offers one-tap resume from its subject index.
- **Fast navigation** — page slider, previous/next controls, native pinch-to-zoom, and local PDF sharing.
- **Adaptive reading on iPad and iPhone Duo** — browse chapters beside the PDF on a wide display; switch between continuous reading and a book-like two-page layout. Compact displays keep a single-page reader without changing the saved page or spread preference.
- **Chapter search** — filter by chapter title or number inside every subject.
- **System, Light, and Dark** — follow the device appearance or keep a fixed paper-and-ink theme.
- **Five native tabs** — Physics, Chemistry, Maths, Support, and About.
- **Notebook-first design** — ruled paper, hand-drawn science marks, subject inks, and a persistent native tab bar.
- **No account, subscription, or paywall** — every note and reader feature remains available without payment.
- **Optional Apple tips** — the Support tab offers four repeatable tips through StoreKit and RevenueCat; they unlock no content or features.

## 📖 Reader experience

The default reader is a vertically continuous, full-screen `PDFKit` view sized to the device. It keeps controls available without placing an intermediate chapter-detail screen between the chapter list and the notes.

On wide displays, including the iPhone Duo inner display, the reader offers a chapter sidebar and a two-page continuous spread with book-style page pairing. The same navigation hierarchy adapts when the available space changes. Reader layout, appearance, and per-chapter progress are stored locally with `UserDefaults`.

## 🛠️ Built with

- **SwiftUI** — adaptive iPhone and iPad interface.
- **PDFKit** — local PDF rendering, zoom, paging, and two-page spreads.
- **AppKit + PDFKit build tool** — converts the tracked handwritten JPEG archive into 91 chapter PDFs during an Xcode build.
- **UserDefaults / AppStorage** — local progress, layout, and appearance preferences.
- **StoreKit + RevenueCat** — four optional, repeatable tips processed by Apple and recorded by RevenueCat, with no subscription or unlocked entitlement.
- **Swift Testing + XCTest UI Tests** — catalog, bundled-PDF, reader, search and tip-jar unit tests, plus iPhone and iPad UI journeys, run in GitHub Actions.
- **HTML, CSS, and JavaScript** — responsive public notebook and browser reader.
- **Cloudflare Workers static assets** — production website delivery.

RevenueCat Purchases is the only third-party iOS runtime dependency. It wraps StoreKit for the optional tip flow and records purchase history using an anonymous RevenueCat App User ID.

## 🚀 Build and run

1. Open `ios/PCMNotes/PCMNotes.xcodeproj` in Xcode.
2. Select the **PCMNotes** scheme.
3. Choose an iPhone or iPad running **iOS 17.6+**.
4. Build and run.

The first build compiles the offline-notes generator and converts the 2,173 source images into 91 chapter PDFs. Later builds reuse the generated cache unless source pages change.

### Shared chapter catalog

`catalog.json` is the single source for chapter numbers, titles, folder names and page counts. Both the native app and website consume it. The website build and offline-PDF generator reject a catalog that differs from the source page files. After editing notes, update the catalog and run `npm run test:catalog`.

### Website

The website is built from the repository root and deployed as static assets through Cloudflare Workers:

```sh
npm install
npm run build
npm run deploy
```

`npm run deploy` requires an authenticated Wrangler session.

### Tests

```sh
bash scripts/test-ios.sh
```

Runs the unit tests and UI tests on task-owned iPhone and iPad simulators (see
[`docs/ios-testing.md`](docs/ios-testing.md)). The **iOS tests** workflow runs the
same script on every pull request and push to `main` that changes the app or its
notes.

Debug builds treat compiler warnings as errors. Other checks on pull requests:
CodeQL static analysis of the Python tooling, website JavaScript and workflows
(the Swift app weekly and for each release tag); Atheris fuzzing of the release parsers in [`fuzz/`](fuzz/)
(`python3 -m unittest discover -s fuzz -p 'test_*.py'` replays them locally);
dependency review; and offline tests of the release tooling. OpenSSF Scorecard
checks the repository weekly.

### Releases

GitHub Actions builds, signs and uploads the App Store binary and creates the GitHub
Release ([`ios-release.yml`](.github/workflows/ios-release.yml)). A release tag is
`v` + the project's `MARKETING_VERSION`, exactly: bump the version to `1.0.1`, then
push `v1.0.1`.

```sh
git tag -a v1.0.1 -m "PCM Notes 1.0.1"
git push origin v1.0.1
```

Release automation uses the SHA-pinned shared `apoorvdarshan/store-release` workflows, configured by [`store/store-release.toml`](store/store-release.toml). Build upload, listing, What's New, screenshots, creative assets, review submission and public release each have a separate repository switch. See [`store/README.md`](store/README.md) for the current defaults and controls. A manual run validates signing and export without uploading anything.

Xcode Cloud no longer builds this app (its workflow and product were removed on
2026-10-14); do not re-create an Xcode Cloud workflow, or a tag would build and
upload twice.

## 🔗 Links and support

- **App Store:** [Download PCM Notes for iPhone and iPad](https://apps.apple.com/us/app/pcm-notes-jee-neet/id6792598227)
- **Website:** [pcm-notes.apoorvdarshan.com](https://pcm-notes.apoorvdarshan.com/)
- **Privacy Policy:** [pcm-notes.apoorvdarshan.com/privacy.html](https://pcm-notes.apoorvdarshan.com/privacy.html)
- **Terms of Use:** [pcm-notes.apoorvdarshan.com/terms.html](https://pcm-notes.apoorvdarshan.com/terms.html)
- **Email support:** [ad13dtu@gmail.com](mailto:ad13dtu@gmail.com?subject=PCM%20Notes%20Support)
- **X:** [@apoorvdarshan](https://x.com/apoorvdarshan)
- **Instagram:** [@apoorvcodes](https://www.instagram.com/apoorvcodes/)

Ko-fi support and product links are available from the website. The iOS app separately offers four optional, repeatable tips processed by Apple through StoreKit and recorded by RevenueCat; tipping never unlocks notes or reader features.

## 🔒 Privacy

The iOS app has no account system, advertising, cross-app tracking, or profiling. It never sends reading activity to PCM Notes. Chapter PDFs are bundled locally, and only the last-read page, selected iPad reader layout, and System/Light/Dark preference are stored on the device.

If a user chooses one of the four optional tips, Apple processes the transaction and RevenueCat receives purchase history plus an anonymous App User ID for receipt validation, app functionality, and aggregate purchase analytics. Neither PCM Notes nor RevenueCat receives payment-card details, and no content or feature is tied to a tip.

App Store privacy label: **Purchases → Purchase History**, used for **App Functionality** and **Analytics**, **not linked to identity**, and **not used for tracking**.

## 🤝 Contributing

Bug reports, corrections to the notes, feature ideas and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## 📜 License

The source code (the iOS app, website, scripts and tooling) is open source under the [MIT License](LICENSE).

The handwritten notes, the chapter PDFs made from them, the app icon and other images, and the PCM Notes name and branding are **not** covered by the MIT License: they remain © 2026 Apoorv Darshan, all rights reserved. See [NOTICE.md](NOTICE.md) for exactly what each covers.

---

<p align="center"><sub>Made with ☕, paper, and a lot of revision.</sub></p>
