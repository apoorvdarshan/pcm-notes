# Store release automation

GitHub Actions builds, signs and uploads PCM Notes to the App Store, keeps the App
Store listing in this repository, and creates the GitHub Release for each tag.

**Nothing reaches the App Store unless its switch is on.** A `v*` tag runs
[`ios-release.yml`](../.github/workflows/ios-release.yml); every App Store
operation is behind its own repository Variable, and all of them default to off.

**Xcode Cloud no longer builds PCM Notes:** its `Default` workflow and product were
removed from App Store Connect on 2026-10-14, after this pipeline passed
validation. Xcode Cloud triggers live in App Store Connect, not in this repo, so a
GitHub switch cannot stop them: never re-create an Xcode Cloud workflow for this
app, or a tag would build and upload twice.

## Release flow (`v*` tag)

```
vX.Y.Z tag ─► offline store-tooling tests ─► resolve switches (the tag must equal
              MARKETING_VERSION exactly: v1.0.1 ⇔ 1.0.1; a run that builds also
              checks the project declares export compliance)
  └─ IOS_RELEASE_AUTOMATION off ─► GitHub Release only
  └─ on:
       IOS_CREATE_VERSION ─► create the tag's App Store version if it is missing
                             (after the offline file checks, before the read-only
                             check and before anything is built)
       listing / What's New / screenshots / review on
                    ─► check the repo's files for the switches that are on, and that
                       the App Store version exists, can take those edits and the
                       submission, and has every locale (read-only) — before
                       anything is built
       IOS_UPLOAD_BUILD ─► iOS tests (unit + UI, iPhone and iPad simulators)
                        ─► next build number from App Store Connect
                        ─► archive Release ─► verify app id/version/build, export
                           compliance, and that every tracked note page is bundled
                        ─► cloud-signed export + upload
                        ─► wait-for-build job: wait until THIS build is VALID
                           (poll 30 s, up to 120 min from upload start)
       IOS_UPLOAD_LISTING / IOS_UPLOAD_WHATS_NEW / IOS_UPLOAD_SCREENSHOTS
                        ─► version metadata for every locale in store/metadata/ios/
                           (only fields that differ are sent)
       IOS_SUBMIT_REVIEW ─► select this run's build, read it back, set release type
                         ─► recheck build + selection ─► add any Ready-to-Submit
                            in-app purchases and the version to one draft review
                            submission ─► submit it once
  └─ GitHub Release (only if every enabled step succeeded)
```

A manual **Run workflow** (`workflow_dispatch`) is a validation run: it archives,
signs, exports and validates the IPA with App Store Connect (`altool
--validate-app`), and never uploads, edits the listing, submits or creates a GitHub
Release. Apple refuses versions it has already approved, so fill in the optional
**marketing_version** input with the next version (e.g. `1.0.1`); the build is
validated as that version without changing the project.

Submission stops instead of continuing when the build times out, fails
processing, is expired, is ambiguous, belongs to another app or version, was
uploaded before this run (or App Store Connect does not report its upload date),
or is not the build selected on the version. An older build is never
substituted. The wait covers build processing only, not App Review or the public
release.

Waiting for the uploaded build is its own job (`wait-for-build`), which hands the
processed build's id to the `app-store-connect` job. If a listing, screenshot or
review step fails, fix the cause and use **Re-run failed jobs**: the re-run keeps
the finished wait and its build id instead of waiting again against the original
upload deadline (which would always time out). The store step is idempotent, so
this also works after the first attempt got further: only fields that differ
are sent, a build or release type already set is left alone, and once the
version sits in a draft review submission (*Ready for Review*: text can still
change, screenshots cannot) or waits for review (nothing can change), the re-run
only checks that App Store Connect already holds the repository's values and
stops on any difference. A re-run of a version already waiting for review
succeeds only when review holds this run's build. When the build job fails, its
archive/export/upload logs are kept for 7 days as the
`ios-release-logs-<run id>-<attempt>` artifact, with the App Store Connect key ID
and issuer ID replaced by placeholders; a file that cannot be rewritten (binary,
UTF-16) and still holds either ID is left out, and nothing is uploaded unless
that redaction succeeded.

Tag runs share one concurrency group with `queue: max`, so releases run one at a
time and a tag pushed while another release is running waits its turn instead of
being dropped. Manual validation runs have their own group and never block or
cancel a release.

`RELEASE_NOTES.md` only feeds the GitHub Release. A tag without a section (or a
missing `RELEASE_NOTES.md`) still gets a GitHub Release, with GitHub's generated
notes; an empty section, or two sections for the same tag, fails before anything
is built. The App Store What's New comes from `whats_new.txt` and is checked
(present for every locale, at most 4000 characters) before the build only when
`IOS_UPLOAD_WHATS_NEW` is on; with it off, a missing or unfit `whats_new.txt`
never blocks a release. The same holds for the listing and screenshot files:
the release checks them only when their switch is on (the offline tests it runs
skip them; the **Store release checks** workflow tests them on every pull
request that changes them).

## Switches

Repository **Variables** (Settings → Secrets and variables → Actions → Variables).
Missing means off. Any value other than `true/false/yes/no/on/off/1/0` fails the
run, even when the master switch is off (the printed switch summary shows it as
`INVALID`). Copy the names from [`gates.env.example`](gates.env.example).

| Variable | When `true` |
|----------|-------------|
| `IOS_RELEASE_AUTOMATION` | Master switch. When off, every App Store operation below is skipped and the tag only creates the GitHub Release |
| `IOS_CREATE_VERSION` | Create the tag's App Store version (*Prepare for Submission*) when App Store Connect does not have it, before the read-only check below. An existing version is left alone. Refuses, before anything is built and without changing anything, when another version is still on its way to the store (anything but *Ready for Distribution*, replaced or removed), the tag is not newer than every existing version, or the bundle id resolves to another app. Tag runs only, never validation runs |
| `IOS_UPLOAD_BUILD` | Run the iOS tests, then build, sign and upload the binary to App Store Connect and wait until it is processed |
| `IOS_UPLOAD_LISTING` | Name, subtitle, privacy URL, description, keywords, promotional text, marketing and support URLs for every locale in `store/metadata/ios/` |
| `IOS_UPLOAD_WHATS_NEW` | What's New (`store/metadata/ios/<locale>/whats_new.txt`, required for every locale; refused if it still repeats the live version's text) |
| `IOS_UPLOAD_SCREENSHOTS` | iPhone 6.7" (`APP_IPHONE_67`) and iPad 12.9" (`APP_IPAD_PRO_3GEN_129`) screenshots from `store/metadata/screenshots/ios/` |
| `IOS_SUBMIT_REVIEW` | Select this run's build and submit the version **and** any Ready-to-Submit products from `catalog/products.json` for review, together in one review submission. Requires `IOS_UPLOAD_BUILD` |
| `IOS_PUBLIC_RELEASE` | Release automatically after approval. Off = you release it by hand. Requires `IOS_SUBMIT_REVIEW` |
| `IOS_BUILD_POLL_SECONDS` | Poll interval, default 30 (10–600) |
| `IOS_BUILD_WAIT_MINUTES` | Total wait from upload start, default 120 (10–300) |

Listing, What's New, screenshot uploads and review submission need the App Store
version to exist in App Store Connect in a state that can take them (*Prepare for
Submission* takes everything; *Ready for Review* takes text and the submission,
not screenshots), with a localization for every locale in `store/metadata/ios/`.
With `IOS_CREATE_VERSION` on, the tag run creates a missing version itself;
otherwise create it in App Store Connect before tagging. The run checks all of
this, read-only, before building. A version created by a run whose later steps
fail stays in *Prepare for Submission*, and the next run for the same tag reuses
it. A new run never submits a version already *Waiting for Review*: its new
build could not replace the one in review.

## Secrets

| Secret | Used for |
|--------|----------|
| `APP_STORE_CONNECT_API_KEY_ID` | Admin-role App Store Connect API key: build numbers, cloud-managed distribution signing, upload, build readiness, listing, review |
| `APP_STORE_CONNECT_ISSUER_ID` | API key issuer |
| `APP_STORE_CONNECT_API_KEY_P8` | API private key (`.p8` body) |
| `IOS_DEV_SIGNING_P12_BASE64` | CI Apple Development identity (base64 `.p12`; certificate "Apple Development: Created via API", ID `NVNGDQGCKK`, expires 2027-10-14) that signs the archive. App Store signing happens at export with Apple's cloud-managed distribution certificate |
| `IOS_DEV_SIGNING_P12_PASSWORD` | Password for that `.p12` |

The same CI identity signs the archives of Fud AI, Verceltics, Abysm, Quit All,
Scowld and Sesslet: Apple allows one API-created development certificate per API
key, and its private key exists only in those repositories' `IOS_DEV_SIGNING_*`
secrets. **Rotation:** before it expires (2027-10-14) or if it is revoked, revoke
it (App Store Connect API, `DELETE /v1/certificates/<id>`), create a new one
(`certificateType: DEVELOPMENT`), and replace both `IOS_DEV_SIGNING_*` secrets in
every one of those repositories. Then run **iOS Release → Run workflow** to
validate.

## Listing metadata (the repository is the source of truth)

```
store/metadata/ios/<locale>/
  name.txt  subtitle.txt  privacy_url.txt            (app information)
  description.txt  keywords.txt  promotional_text.txt
  marketing_url.txt  support_url.txt  whats_new.txt  (version)
```

`en-US` (the primary and only locale today) was exported from the live 1.0 listing
with [`export_listing.py`](../scripts/store/export_listing.py), which only reads
App Store Connect. Edit these files and turn on `IOS_UPLOAD_LISTING` to publish
them; after editing the listing in App Store Connect by hand, run the exporter to
bring the repository back in line. To add a language, add it in App Store Connect
and add a `store/metadata/ios/<locale>/` directory. Character limits (name and
subtitle 30, keywords 100, promotional text 170, description and What's New 4000)
are checked before anything is uploaded. `APPSTORE.md` records the 1.0 submission
and is not read by the pipeline.

Before releasing a new version with `IOS_UPLOAD_WHATS_NEW`, write its notes in
`whats_new.txt` (1.0 had none). Upload is refused while the file still holds the
live version's What's New.

## Screenshots

The App Store screenshots are mockups, byte-identical to the ones live for 1.0:
`store/metadata/screenshots/ios/6.7/*.png` (1290×2796, iPhone 6.7") and
`store/metadata/screenshots/ios/12.9/*.png` (2048×2732, iPad 12.9"), uploaded in file-name order
to the primary locale (other locales fall back to it). Sizes are checked before
upload, a set that already matches (by checksum) is left alone, and a changed set
is replaced and waited on until App Store Connect has processed it. With the
switch on and no PNGs in a directory, the run fails with a clear message.

## Export compliance

The app declares its export compliance answer in its build settings
(`INFOPLIST_KEY_ITSAppUsesNonExemptEncryption = NO` in the app target's Debug
and Release configurations, so every build's `Info.plist` carries
`ITSAppUsesNonExemptEncryption = false`). Without it, App Store Connect asks
about each build by hand, and review submission fails with
`usesNonExemptEncryption` missing (HTTP 409).

`NO` is right while the app only uses exempt encryption: HTTPS (RevenueCat's
network calls), the Keychain and Apple's system crypto. The app itself encrypts
nothing; RevenueCat also hashes data and verifies signed server responses,
which are exempt too. If the app ever adds its own encryption, set it to `YES`
and add `INFOPLIST_KEY_ITSEncryptionExportComplianceCode` with the code App
Store Connect issues for the app's encryption documentation.

Every run that builds (release or validation) checks it twice:
`ios_release.py gates` reads each configuration (Debug and Release) of the app
target before anything is built, and `ios_release.py export-compliance` reads
the archived app's `Info.plist` right after the archive. A key missing from
either configuration, values that differ or are unknown, or `YES` without the
code in that configuration fails the run; the test targets' settings never
count.

## In-app purchases

[`catalog/products.json`](catalog/products.json) lists the four consumable tips
(`com.apoorvdarshan.iitjee.tip.small|medium|large|generous`). With
`IOS_SUBMIT_REVIEW` on, products from this catalog that App Store Connect shows as
*Ready to Submit* go to review with the version: their draft versions join the
same review submission as the app version (Review Submission Items with the
`inAppPurchaseVersion` relationship), after the build recheck, and everything is
submitted once. Apple's deprecated `inAppPurchaseSubmissions` endpoint, which sent
a product to review on its own, is not used. Approved products (all four today)
are never resubmitted, and products not in the catalog are ignored.
[`validate_catalog.py`](../scripts/store/validate_catalog.py) keeps the catalog in
line with `TipProductID` in `TipStore.swift` and `ios/PCMNotes/PCMNotes.storekit`.
RevenueCat records the tips; there is nothing to sync, so there is no
RevenueCat switch.

## Scripts

| Script | Role |
|--------|------|
| [`scripts/store/ios_release.py`](../scripts/store/ios_release.py) | Switches, export compliance (project and archive), read-only preflight, build number, wait for this run's build |
| [`scripts/store/asc_release.py`](../scripts/store/asc_release.py) | Version creation (`--create-version`), listing, What's New, screenshots, build selection, review submission |
| [`scripts/store/export_listing.py`](../scripts/store/export_listing.py) | Read-only export of the live listing into `store/metadata/` |
| [`scripts/store/validate_catalog.py`](../scripts/store/validate_catalog.py) | Catalog ↔ app ↔ StoreKit configuration check |
| [`scripts/store/print_gates.py`](../scripts/store/print_gates.py) | Prints the switches as the release reads them (`INVALID` for values that fail a run) |
| [`scripts/store/redact_logs.py`](../scripts/store/redact_logs.py) | Removes the App Store Connect key and issuer IDs from build logs before they are kept as an artifact |
| [`scripts/store/test_ios_release.py`](../scripts/store/test_ios_release.py) | Offline tests: disabled/invalid switches, tag mismatch, validation mode, export compliance (project and archived `Info.plist`), version creation (left alone when it exists, refused while another version is in flight or not newer), preflight, build wait timeout/failure/ambiguity/mismatch/old build/missing upload date, selection read-back and recheck before any review write, token refresh, listing, What's New (strict only when its switch is on), screenshots, products, idempotent reruns, log redaction, release notes, the printed switches |
| [`scripts/release_notes.py`](../scripts/release_notes.py) | GitHub Release notes from `RELEASE_NOTES.md` (generated notes when the tag has no section or the file is missing) |

The **Store release checks** workflow runs the offline tests, the catalog check and
a dry run of the listing and screenshots on every pull request that touches this
tooling, then reruns the release's offline tests and release-notes step on a copy
of the checkout without `RELEASE_NOTES.md`, which must pass because a missing
file only means generated notes. Every iOS Release run also runs the offline
tests first, except the ones that check the repository's listing and screenshot
files (`STORE_TESTS_SKIP_REPOSITORY_FILES=1`); the check that `RELEASE_NOTES.md`
repeats no section runs only when the file exists. Release jobs install
[`requirements-store.txt`](../requirements-store.txt) (PyJWT, hash-pinned) into a
virtual environment only when they talk to App Store Connect, and run the store
scripts unbuffered (`python3 -u`) so the build wait's progress shows live.
Dependabot proposes monthly updates for it, the pinned GitHub Actions, the Swift
packages and the website's npm tooling.

## Releasing

1. Bump `MARKETING_VERSION` in the Xcode project (every target) to the new version.
2. If the listing, What's New, screenshots or review will be uploaded, the X.Y.Z
   App Store version must exist. With `IOS_CREATE_VERSION` on, the tag run
   creates it; otherwise create it in App Store Connect first (it stays in
   *Prepare for Submission*). The run stops before building without it. Update
   `store/metadata/ios/<locale>/whats_new.txt`.
3. Optionally add one `## vX.Y.Z` section to `RELEASE_NOTES.md`.
4. Run **iOS Release → Run workflow** with `marketing_version` = the new version
   to validate signing and export (nothing is uploaded).
5. Turn on only the switches you want (start with `IOS_RELEASE_AUTOMATION` +
   `IOS_UPLOAD_BUILD`, plus `IOS_CREATE_VERSION` if the run should create the
   App Store version; leave `IOS_SUBMIT_REVIEW` and `IOS_PUBLIC_RELEASE` off until
   you intend to submit).
6. Push the tag: `git tag -a vX.Y.Z -m "PCM Notes X.Y.Z" && git push origin vX.Y.Z`.

## Dry run

```bash
python3 -m unittest scripts/store/test_ios_release.py
python3 scripts/store/print_gates.py
python3 scripts/store/validate_catalog.py
UPLOAD_LISTING=true UPLOAD_SCREENSHOTS=true SUBMIT_IOS_REVIEW=true \
  python3 scripts/store/asc_release.py --dry-run --build-id DRY-RUN --build-number 1
python3 scripts/release_notes.py v1.0.1
```
