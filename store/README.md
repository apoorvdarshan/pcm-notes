# Store releases

PCM Notes' App Store releases run on
[apoorvdarshan/store-release](https://github.com/apoorvdarshan/store-release), a
shared set of reusable workflows and Python tooling. This repository's own release
scripts (`scripts/store/` at `517b61a`) moved there unchanged as the `pcm-notes`
flow, so a release does what it did before. Two thin caller workflows pin
store-release by commit SHA, with the version in a `# vX.Y.Z` comment (v0.3.0 today):

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| [`ios-release.yml`](../.github/workflows/ios-release.yml) | `vX.Y[.Z]` tag | release run; the tag must equal `MARKETING_VERSION` exactly (`v1.0.1` for 1.0.1) |
| | **Run workflow** | validation only: archive, cloud-signed export and App Store Connect validation of the IPA, plus a read-only store plan. Never uploads, edits the listing, submits or creates a GitHub Release |
| [`store-checks.yml`](../.github/workflows/store-checks.yml) | pull requests and `main` pushes that touch store files, **Run workflow** | checks the store data, [`store-release.toml`](store-release.toml) and the callers against the pinned version. Never uploads |

**Nothing reaches the App Store unless its switch is on**, and merging never
releases: only a tag push does. The callers are rendered by store-release
(`run.py callers render`); Store checks fails when one is edited by hand or pins
anything but a store-release release. Dependabot moves both pins to a new
store-release release in one PR of its own, seven days after it is published.

How the pipeline behaves is documented in store-release (v0.3.0):
[PCM Notes' flow](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/flows.md#pcm-notes-iosflow--pcm-notes),
[switches](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/switches.md),
[secrets](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/secrets.md),
[jobs and checks](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/workflows.md),
[store plan](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/plan.md),
[re-runs](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/rerun.md),
[the config file](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/config.md)
and [how a new release reaches this repository](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/release-process.md#how-apps-receive-a-release).
This file keeps only PCM Notes' own facts.

**Xcode Cloud no longer builds PCM Notes:** its `Default` workflow and product were
removed from App Store Connect on 2026-10-14. Xcode Cloud triggers live in App Store
Connect, not in this repository, so a switch here cannot stop one: never re-create an
Xcode Cloud workflow for `v*` tags, or a tag would build and upload twice.

## What this repository keeps

- [`store-release.toml`](store-release.toml): where the store files are and PCM
  Notes' fixed identities: bundle id `com.apoorvdarshan.iitjee`, App Store Connect app
  `6792598227`, team `23RV7FYH36`, the Xcode to build with, and the folders the build
  checks out (the app bundles one PDF per chapter generated from the note images in
  `physics/`, `chemistry/` and `mathematics/`).
- The switches (repository variables) and credentials (repository secrets) below.
- The listing, screenshots, images and catalog under `store/`, and
  [`RELEASE_NOTES.md`](../RELEASE_NOTES.md) (GitHub Release bodies).
- The gate a tag run that uploads a build must pass first:
  [`ios-tests.yml`](../.github/workflows/ios-tests.yml) (unit and UI tests on iPhone
  and iPad simulators). A tag run that uploads no build does not wait for it.
- [`scripts/ci/check_offline_notes.sh`](../scripts/ci/check_offline_notes.sh), which
  store-release runs right after the archive (`ios.build.post_archive`): the archived
  app's `OfflineNotes/manifest.json` must count every chapter and page git tracks for
  each subject, with one PDF per chapter.

## Current switches

Repository **Variables** (Settings → Secrets and variables → Actions → Variables),
as of 2026-10-21. Missing means off; any value other than
`true/false/yes/no/on/off/1/0` fails the run, even when the master switch is off.

| Variable | Value |
|----------|-------|
| `IOS_RELEASE_AUTOMATION` | `true` |
| `IOS_CREATE_VERSION` | `true` |
| `IOS_UPLOAD_BUILD` | `true` |
| `IOS_UPLOAD_LISTING` | `true` |
| `IOS_UPLOAD_WHATS_NEW` | `true` |
| `IOS_SUBMIT_REVIEW` | `true` |
| `IOS_PUBLIC_RELEASE` | `true` |
| `IOS_UPLOAD_SCREENSHOTS`, `IOS_UPLOAD_CREATIVE_ASSETS` | not set (off) |
| `IOS_BUILD_POLL_SECONDS`, `IOS_BUILD_WAIT_MINUTES` | not set (30 s, 120 min) |

With these values, a `vX.Y.Z` tag creates App Store version X.Y.Z when App Store
Connect does not have it yet, runs the iOS tests, builds, signs and uploads the
binary, waits until App Store Connect has processed this run's build, updates the
listing and What's New for every locale in `store/metadata/ios/`, selects the build
and submits the version (with any *Ready to Submit* products from the catalog) for
review, released automatically once Apple approves it. Screenshots and the header
and search results images are left alone. The GitHub Release
`PCM Notes vX.Y.Z (build N)` is created only when every enabled step succeeded, and
an existing release is left as it is.

Each **Run workflow** prints this plan for the switches as they are at that moment
(the `store-plan` job's summary).

## Secrets

| Secret | Used for |
|--------|----------|
| `APP_STORE_CONNECT_API_KEY_ID` | Admin-role App Store Connect API key: build numbers, cloud-managed distribution signing, upload, build readiness, listing, review |
| `APP_STORE_CONNECT_ISSUER_ID` | Its issuer |
| `APP_STORE_CONNECT_API_KEY_P8` | Its private key (`.p8` body) |
| `IOS_DEV_SIGNING_P12_BASE64` | The CI Apple Development identity (base64 `.p12`) that signs the archive; App Store signing happens at export with Apple's cloud-managed certificate |
| `IOS_DEV_SIGNING_P12_PASSWORD` | Its password |

The callers pass each reusable workflow exactly the secrets it declares, by name
(never `secrets: inherit`).

The CI Apple Development identity is certificate "Apple Development: Created via
API", ID `NVNGDQGCKK`, **expiring 2027-10-14**. The same identity signs the archives
of Fud AI, Verceltics, Abysm, Quit All, Scowld and Sesslet, because Apple allows one
API-created development certificate per API key. Renewing it:
[the shared CI signing identity](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/provenance.md#facts-that-only-concern-the-six-apps);
giving another app repository a copy without the private key leaving GitHub:
[sharing one development identity](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/secrets.md#sharing-one-development-identity-across-your-apps).
When it is replaced, update both `IOS_DEV_SIGNING_*` secrets here and in every
repository listed above.

## Releasing

1. Bump `MARKETING_VERSION` in the Xcode project (every target) to the new version.
2. Write the new version's What's New in
   `store/metadata/ios/<locale>/whats_new.txt` (required for every locale while
   `IOS_UPLOAD_WHATS_NEW` is on; refused while it still repeats the live version's
   text).
3. Optionally add one `## vX.Y.Z` section to `RELEASE_NOTES.md`. A tag without one
   (or without the file) gets GitHub's generated notes; an empty section, or two for
   the same tag, fails before anything is built.
4. Run **iOS Release → Run workflow** with `marketing_version` = the new version
   (Apple refuses versions it has already approved). It only validates. Read the
   `store-plan` job's summary: it lists every write the tag run would make with the
   current switches, and anything that would stop it.
5. Push the tag: `git tag -a vX.Y.Z -m "PCM Notes X.Y.Z" && git push origin vX.Y.Z`.
6. If a later step fails, fix the cause and use **Re-run failed jobs**
   ([re-runs](https://github.com/apoorvdarshan/store-release/blob/v0.3.0/docs/rerun.md)).

## Listing metadata (the repository is the source of truth)

```
store/metadata/ios/<locale>/
  name.txt  subtitle.txt  privacy_url.txt            (app information)
  description.txt  keywords.txt  promotional_text.txt
  marketing_url.txt  support_url.txt  whats_new.txt  (version)
```

`en-US` (the primary and only locale today) was exported from the live 1.0 listing.
After editing the listing in App Store Connect by hand, bring the repository back in
line with store-release's read-only exporter (`run.py ios export-listing`). To add a
language, add it in App Store Connect and add a `store/metadata/ios/<locale>/`
directory. Character limits (name and subtitle 30, keywords 100, promotional text
170, description and What's New 4000) are checked before anything is uploaded.
`APPSTORE.md` records the 1.0 submission and is not read by the pipeline.

## Screenshots

The App Store screenshots are mockups, byte-identical to the ones live for 1.0:
`store/metadata/screenshots/ios/6.7/*.png` (1290×2796, iPhone 6.7") and
`store/metadata/screenshots/ios/12.9/*.png` (2048×2732, iPad 12.9"), uploaded in
file-name order to the primary locale. iPhone Duo
(`store/metadata/screenshots/ios/iphone-duo/`) and Apple Watch
(`store/metadata/screenshots/ios/watch/`) sets go with them when their folders hold
PNGs; PCM Notes has none yet. The committed mockups are RGBA (fully opaque); App
Store Connect refuses any PNG that stores alpha, so they go up as RGB copies and the
committed files are never rewritten. A set is up to date when App Store Connect holds
either the committed files or their RGB copies.

## Header and search results images (iOS)

`store/creative/ios/header/` takes one PNG of exactly 3840×1646 and
`store/creative/ios/search-results/` one PNG or JPEG in 3:2 (1920×1280 to
3840×2560), neither storing alpha. PCM Notes has none yet (each folder only holds a
README), so leave `IOS_UPLOAD_CREATIVE_ASSETS` off until one is added.

## Export compliance

The app declares its answer in its build settings
(`INFOPLIST_KEY_ITSAppUsesNonExemptEncryption = NO` in the app target's Debug and
Release configurations, so every build's `Info.plist` carries
`ITSAppUsesNonExemptEncryption = false`). `NO` is right while the app only uses
exempt encryption: HTTPS (RevenueCat's network calls), the Keychain and Apple's
system crypto. If the app ever adds its own encryption, set it to `YES` and add
`INFOPLIST_KEY_ITSEncryptionExportComplianceCode` with the code App Store Connect
issues. Every run that builds checks both configurations before building and the
archived app's `Info.plist` right after the archive.

## In-app purchases

[`catalog/products.json`](catalog/products.json) lists the four consumable tips
(`com.apoorvdarshan.iitjee.tip.small|medium|large|generous`). With
`IOS_SUBMIT_REVIEW` on, products App Store Connect shows as *Ready to Submit* join
the version's review submission; approved products (all four today) are never
resubmitted, and products not in the catalog are ignored. The catalog must match
`TipProductID` in `ios/PCMNotes/PCMNotes/TipStore.swift` (in order) and
`ios/PCMNotes/PCMNotes.storekit`; Store checks enforces it on every pull request.
RevenueCat records the tips; there is nothing to sync.

## Checking locally

With Python 3.11 or later (CI uses 3.13), from the repository root:

```bash
git clone --branch v0.3.0 https://github.com/apoorvdarshan/store-release ../store-release
python3 -I -u ../store-release/run.py --app-root . config check --files
python3 -I -u ../store-release/run.py --app-root . catalog check
python3 -I -u ../store-release/run.py --app-root . release-notes --check-headings
python3 -I -u ../store-release/run.py --app-root . ios pr-checks
IOS_SUBMIT_REVIEW=maybe python3 -I -u ../store-release/run.py print-switches --platform ios   # shows INVALID
```

These read only this checkout and call no store API.
