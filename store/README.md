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
       IOS_CREATE_PRODUCTS ─► read-only plan of every catalog product (fields, review
                              screenshot, price point, territories); then create the
                              missing ones, Ready to Submit (see Creating products)
       listing / What's New / screenshots / creative assets / review on
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
       IOS_UPLOAD_CREATIVE_ASSETS ─► header / search results images: upload and
                                     review new ones on their own, place approved ones
       IOS_SUBMIT_REVIEW ─► pick the version's draft review submission (read-only)
                         ─► select this run's build, read it back, set release type
                         ─► recheck build + selection ─► add any Ready-to-Submit
                            in-app purchases and the version to that draft (or a
                            new one) ─► check it holds nothing else ─► submit it once
  └─ GitHub Release (only if every enabled step succeeded)
```

A manual **Run workflow** (`workflow_dispatch`) is a validation run: it archives,
signs, exports and validates the IPA with App Store Connect (`altool
--validate-app`), and never uploads, edits the listing, submits or creates a GitHub
Release. Apple refuses versions it has already approved, so fill in the optional
**marketing_version** input with the next version (e.g. `1.0.1`); the build is
validated as that version without changing the project.

### App Store Connect CLI (`asc`)

Every App Store Connect call goes through the
[App Store Connect CLI](https://github.com/rorkai/App-Store-Connect-CLI) (`asc`,
MIT) instead of hand-written API code.
[`scripts/store/asc_cli.py`](../scripts/store/asc_cli.py) is the only place
that runs it:

- It is pinned to one release (`ASC_VERSION`, 5.15.0) and installed by
  `asc_cli.py install`, which refuses a binary whose SHA-256 differs from the
  pinned value for that platform (`linux_amd64` and `macOS_arm64`). Every job
  that talks to App Store Connect runs `asc_cli.py check`, and every run
  re-checks the checksum of the `asc` on `PATH` before handing it credentials;
  no environment variable can point the scripts at another executable.
- Credentials come only from the `APP_STORE_CONNECT_*` secrets, through asc's
  own environment variables; the keychain and asc config files are bypassed,
  and `ASC_STRICT_AUTH` rejects credentials mixed from several sources.
- Telemetry (`ASC_TELEMETRY_DISABLED`, `DO_NOT_TRACK`) and the skills update
  check are off.
- Checks that must never write run asc read-only (`ASC_READ_ONLY`): the version
  check before the build, the product plan, build numbering, the build wait, the
  whole `prepare` job on validation runs, and `export_listing.py`. asc refuses a
  write in that mode before sending it (exit 6).
- On failure the error keeps asc's exit code and drops the key and issuer IDs.
  asc itself retries a read that hits HTTP 429 or 503 (`ASC_MAX_RETRIES=5`).
  The build wait also retries asc's server-side exit codes (HTTP 5xx, 408, 429
  and timeouts) and network drops, which asc reports as a generic exit 1 with a
  connection error; auth, usage and not-found errors stop at once.
- asc refuses to upload a PNG that stores an alpha channel or transparency (PNG
  colour types 4 and 6, or a `tRNS` chunk), even when every pixel is opaque.
  Screenshots that store alpha go up as RGB copies (see [Screenshots](#screenshots));
  creative images must be RGB (see [Creative assets](#creative-assets)).

The binary itself is still exported and uploaded by `xcodebuild`. To move to a
newer asc, change `ASC_VERSION` and both `ASC_SHA256` values from that release's
`asc_<version>_checksums.txt`, then run the store tests.

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
change, screenshots and creative assets cannot) or waits for review (nothing can
change), the re-run only checks that App Store Connect already holds the
repository's values and stops on any difference. A re-run of a version already waiting for review
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
| `IOS_CREATE_PRODUCTS` | Create every iOS product in `store/catalog/products.json` that App Store Connect does not have yet (matched by product ID) and bring it to *Ready to Submit*, in `prepare`, before anything is built, so `IOS_SUBMIT_REVIEW` sends it to review in the same run. A read-only plan of every product runs first and must pass completely (fields, review screenshot, price point, territories), so a problem stops the run before any write. Existing products are never changed: their price, period, type and family sharing are compared and differences printed as warnings. One left in *Missing Metadata* by an interrupted run is finished, never created twice. Tag runs only; validation runs print the plan read-only (see [Creating products](#creating-products-ios_create_products)) |
| `IOS_UPLOAD_BUILD` | Run the iOS tests, then build, sign and upload the binary to App Store Connect and wait until it is processed |
| `IOS_UPLOAD_LISTING` | Name, subtitle, privacy URL, description, keywords, promotional text, marketing and support URLs for every locale in `store/metadata/ios/` |
| `IOS_UPLOAD_WHATS_NEW` | What's New (`store/metadata/ios/<locale>/whats_new.txt`, required for every locale; refused if it still repeats the live version's text) |
| `IOS_UPLOAD_SCREENSHOTS` | iPhone 6.7" (`APP_IPHONE_67`) and iPad 12.9" (`APP_IPAD_PRO_3GEN_129`) screenshots from `store/metadata/screenshots/ios/` |
| `IOS_UPLOAD_CREATIVE_ASSETS` | Product page header and search results images from `store/creative/ios/` (see below). A new image is uploaded to the Asset Library and sent to App Review in a submission of its own; once Apple approves it, a later run places it on the version. Never changes anything while the version is in a draft review submission or waits for review |
| `IOS_SUBMIT_REVIEW` | Select this run's build and submit the version **and** any Ready-to-Submit iOS products from `catalog/products.json` (including ones `IOS_CREATE_PRODUCTS` created earlier in the run) for review, together in one review submission. Requires `IOS_UPLOAD_BUILD` |
| `IOS_PUBLIC_RELEASE` | Release automatically after approval. Off = you release it by hand. Requires `IOS_SUBMIT_REVIEW` |
| `IOS_BUILD_POLL_SECONDS` | Poll interval, default 30 (10–600) |
| `IOS_BUILD_WAIT_MINUTES` | Total wait from upload start, default 120 (10–300) |

Listing, What's New, screenshot and creative asset uploads and review submission
need the App Store version to exist in App Store Connect in a state that can take
them (*Prepare for Submission* takes everything; *Ready for Review* takes text and
the submission, not screenshots or creative assets), with a localization for every
locale in `store/metadata/ios/`.
With `IOS_CREATE_VERSION` on, the tag run creates a missing version itself;
otherwise create it in App Store Connect before tagging. The run checks all of
this, read-only, before building. A version created by a run whose later steps
fail stays in *Prepare for Submission*, and the next run for the same tag reuses
it. A new run never submits a version already *Waiting for Review*: its new
build could not replace the one in review.

## Secrets

| Secret | Used for |
|--------|----------|
| `APP_STORE_CONNECT_API_KEY_ID` | Admin-role App Store Connect API key: build numbers, cloud-managed distribution signing, upload, build readiness, listing, creative assets, review |
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
upload, and a set that already matches is left alone. Sets are compared by
checksum, in order: App Store Connect keeps the file names a set was first
uploaded with (the live sets are `image1.png` to `image5.png`). A changed set is
replaced (`asc screenshots upload --replace`, given a folder holding exactly the
set's PNGs, which it uploads in file-name order, waiting for App Store Connect to
process each one), then read back until every screenshot is processed and the
set matches the repository.
With the switch on and no PNGs in a directory, the run fails with a clear
message.

asc refuses to upload a PNG with an alpha channel, even a fully opaque one, and
the ten committed mockups are RGBA. The store step never hands asc the committed
files: it writes each set to a temporary folder, where a PNG that stores alpha
becomes an RGB copy with its transparent pixels laid over black (Pillow, pinned
with hashes in `requirements-store.txt`; the committed mockups are fully opaque,
so their copies have the same pixels). The committed files are never changed.
Every file in that folder is checked for alpha once more before asc gets it. A
set matches App Store Connect when it holds either the committed files (as for
1.0) or those copies, so neither is uploaded again. Release jobs install Pillow
only when `IOS_UPLOAD_SCREENSHOTS` is on. Without it, a set with alpha that would
have to be uploaded is refused by the read-only check before the build (and by
the store step, which checks every set before replacing any). The dry run prints
a note for screenshots that have an alpha channel. For a set App Store Connect
holds as copies, a Pillow update that changes the copies' bytes makes the next
run with the switch on upload it again (and a rerun fail while the version is in
review).

## Creative assets

`IOS_UPLOAD_CREATIVE_ASSETS` puts the product page header and search results
images on the version. The repository has none yet, so the switch stays off; to
use it, add one image to each slot you want:

```
store/creative/ios/
  header/           # one product page header image: 3840×1646 PNG
  search-results/   # one search results image: 3:2 PNG or JPEG, 1920×1280 to 3840×2560
```

(Apple also accepts one 5244×2950 PNG for either slot.) A slot may stay empty,
but with the switch on at least one image is required. Sizes, formats and alpha
are checked with the other local inputs, so more than one image in a slot, an
image of the wrong size or format, or one with an alpha channel stops the run
before anything is built.

An image is matched in the Asset Library by file name and size, so give a changed
image a new file name. A reservation left by an interrupted upload
(`AWAITING_UPLOAD`) is ignored and the image is uploaded again. Images never take
the draft the version and its products chose. An unfinished draft holding only
these images is finished first, else an empty one, instead of starting another,
so a rerun leaves no extra drafts. Apple reviews each new image on its own
(PCM Notes already has an approved version), and a later run places it once it
is approved, swapping out an earlier placement; a
rejected image stops the run with Apple's reason. While the version is in a draft
review submission or waits for review, nothing is uploaded, sent to review or
placed: a new or not yet submitted image stops the run, but an image approved in
the meantime is not a mismatch, so a re-run passes and the image is placed once
the version can be edited. Apple's rules for these images: no prices, website
addresses, other platforms or Apple badges, and no transparency.

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
*Ready to Submit* (including any `IOS_CREATE_PRODUCTS` created earlier in the same
run) go to review with the version: their draft versions join the same review
submission as the app version (`inAppPurchaseVersions` review items,
via `asc review items-add`), after the build recheck, and everything is submitted
once. Apple's deprecated `inAppPurchaseSubmissions` endpoint, which sent a product
to review on its own, is not used. Approved products (all four today) are never
resubmitted, and products not in the catalog are ignored. A reused draft is
submitted only when it holds this version and nothing but versions of catalog
products; anything else stops the run. The draft that already holds this version
is reused; otherwise only a draft holding no app version is, so another version's
draft is never touched, and drafts holding creative images belong to those
images' own review. A *Ready for Review* version that no draft holds stops the
run before anything is written: the draft is checked before the listing text,
screenshots, images or build are touched, and the read-only version preflight
checks it too, before anything is built. A re-run that has no product to send
creates no draft.
[`validate_catalog.py`](../scripts/store/validate_catalog.py) keeps the catalog in
line with `TipProductID` in `TipStore.swift` and `ios/PCMNotes/PCMNotes.storekit`.
RevenueCat records the tips; there is nothing to sync, so there is no
RevenueCat switch.

Product groups and their App Store type: `subscriptions` (auto-renewable),
`consumables` and `tips` (`CONSUMABLE`), and the optional `non_consumables`
(`NON_CONSUMABLE`). Every item lists `"platforms": ["ios"]` (only items that list
`ios` are App Store products, and PCM Notes has no other platform), a `type`
(`consumable` or `non_consumable`) that matches its group, and a `reference_name`
(or, for a product the release creates, `ios.reference_name`; when both are set
they must match). `apple_id` is App Store Connect's id, added once the product
exists. `base_price_inr` records the four tips' prices (India is their base
territory); the release does not read it.

### Creating products (`IOS_CREATE_PRODUCTS`)

[`asc_products.py`](../scripts/store/asc_products.py) (the same file in every app
that shares this pipeline) matches every iOS product to App Store Connect by
product ID:

| In App Store Connect | What a tag run does |
|---|---|
| Missing | Creates it and brings it to *Ready to Submit* |
| *Missing Metadata*, with the catalog's reference name, type (or period) and family sharing | Finishes it: an interrupted run left it there. Only missing parts are added; a part that is already set and differs from the catalog (localization, price, availability, review note, or a failed or different review screenshot) stops the run in the plan, before any write. A screenshot that matches the file is kept even while Apple is still processing it |
| Anything else | Never changed. Price (United States), period, type and family sharing (when the catalog sets them) are compared, and differences are printed as warnings |

The four tips are approved and have no `ios` block or `suggested_price_usd`, so
the plan lists them as existing and compares only their type.

What a created product gets, all from the catalog:

| Part | Source |
|---|---|
| Reference name | `ios.reference_name` (up to 64 characters) |
| Type | the catalog group (above) |
| Subscription group and period | `ios.subscription_group` (a `reference_name` from the top-level `ios_subscription_groups`) and `period` (`P1W`, `P1M`, `P2M`, `P3M`, `P6M`, `P1Y`) |
| Base price | `suggested_price_usd` in the United States (`USA`); App Store Connect derives the other territories' prices from it (Apple's equalized prices), India's included. So unlike the four tips, whose base territory is India, a created product's base territory is the United States. The plan checks the price is a price point by asking a live product of the same kind; an app without one is checked against its own price grid, except a first subscription priced above that grid's top ($400; only subscriptions go higher), which App Store Connect checks while creating it (a warning says so) |
| Availability | `ios.territories`: `"all"` (the default) is every current territory plus new ones automatically; a list of alpha-3 codes (`["USA", "IND"]`) is exactly those |
| en-US display name and description | `ios.display_name` (2–30 characters), `ios.description` (up to 45) |
| App Review screenshot | `ios.review_screenshot`: a path under `store/` (keep them in `store/catalog/review/`), a PNG or JPEG named for its format, RGB without alpha, at an iPhone or iPad screenshot size (e.g. 1290×2796 or 2064×2752), and complete (a PNG is fully decoded; a JPEG must reach its end marker) |
| Review note | `ios.review_note` (subscriptions only, up to 4,000 characters) |
| Family sharing | `ios.family_sharing`, default `false`. It can never be turned off again, so it is only sent when the catalog says `true` (subscriptions and non-consumables only) |

A new subscription group also needs `display_name` in its `ios_subscription_groups`
entry (its en-US name); a group App Store Connect already names is never renamed.
Products that already exist need none of these fields; nothing is invented for them.
Comparing an existing product's price is only a warning, so an App Store Connect
error while reading it is reported and the release goes on.
`validate_catalog.py` checks every `ios` block offline (including the screenshot
file), and the plan checks, read-only, that each product to create has every
field, that its price is a price point in the United States, and that its
territories exist. Example (a fifth tip):

```json
"tips": [
  {
    "id": "com.apoorvdarshan.iitjee.tip.mega",
    "type": "consumable",
    "platforms": ["ios"],
    "suggested_price_usd": "9.99",
    "ios": {
      "reference_name": "PCM Notes Mega Tip",
      "display_name": "Mega Tip",
      "description": "A big optional tip for PCM Notes.",
      "review_screenshot": "store/catalog/review/tip-jar.png"
    }
  }
]
```

How it runs (`prepare` job, before anything is built): the plan (read-only)
checks every product first; any problem stops the run with nothing created.
Subscriptions go through asc's reconciling `asc subscriptions setup`: once
without the screenshot (group, localization, price, availability), then the
review note, then again with the screenshot, which completes it and verifies the
result. In-app purchases are added one part at a time (product, en-US
localization, price, availability, screenshot last). Because the screenshot is
required for *Ready to Submit* and always goes last, a product an interrupted run
left behind stays in *Missing Metadata*, and the next run finishes it. Each
product is read back until it is *Ready to Submit* (up to 2 minutes). A manual
validation run prints the plan and creates nothing.

What stays manual:

- **The app**: a new tip must also be in `TipProductID` (`TipStore.swift`) and
  `ios/PCMNotes/PCMNotes.storekit`, in the catalog's order
  (`validate_catalog.py` refuses the catalog otherwise), so it ships with the app
  version that sells it. The app loads tips by product ID through RevenueCat, not
  from an offering. When the script finishes it prints a reminder about
  RevenueCat offerings and `catalog/revenuecat.json`, which comes from Fud AI's
  copy of the script; PCM Notes has neither.
- An in-app purchase's review note: the pinned asc 5.15.0 has no command for it,
  so `ios.review_note` is refused on in-app purchases. Add one in App Store
  Connect when App Review needs it.
- Other locales, promotional images, introductory and promotional offers, offer
  codes, a subscription's level within its group, and a base territory other
  than the United States.
- Changing an existing product (price, name, availability); the release only
  warns about differences.
- A new subscription group's own review item, if App Store Connect asks for one:
  the review step attaches subscription and in-app purchase versions only.
- `apple_id`: add the new product's App Store Connect id to the catalog afterwards.

## Scripts

| Script | Role |
|--------|------|
| [`scripts/store/ios_release.py`](../scripts/store/ios_release.py) | Switches, export compliance (project and archive), read-only preflight, build number, wait for this run's build |
| [`scripts/store/asc_cli.py`](../scripts/store/asc_cli.py) | Installs the pinned, checksum-verified `asc` and runs it (credentials, read-only mode, telemetry off, redacted errors) |
| [`scripts/store/asc_products.py`](../scripts/store/asc_products.py) | `plan`: read-only check of the catalog products App Store Connect does not have (or has in *Missing Metadata*) and drift warnings for the rest; `create` (`CREATE_IOS_PRODUCTS`): the plan, then create or finish them, *Ready to Submit*. Its offline field checks also run in `validate_catalog.py`. Identical to Fud AI's copy; it reads the app's ids from `asc_release.py` |
| [`scripts/store/asc_release.py`](../scripts/store/asc_release.py) | Version creation (`--create-version`), listing, What's New, screenshots, header and search results images, build selection, review submission; every call through `asc` |
| [`scripts/store/export_listing.py`](../scripts/store/export_listing.py) | Read-only export of the live listing into `store/metadata/` (asc in read-only mode) |
| [`scripts/store/validate_catalog.py`](../scripts/store/validate_catalog.py) | Catalog ↔ app ↔ StoreKit configuration check, and the offline checks of the `ios` creation fields and review screenshots |
| [`scripts/store/print_gates.py`](../scripts/store/print_gates.py) | Prints the switches as the release reads them (`INVALID` for values that fail a run) |
| [`scripts/store/redact_logs.py`](../scripts/store/redact_logs.py) | Removes the App Store Connect key and issuer IDs from build logs before they are kept as an artifact |
| [`scripts/store/test_ios_release.py`](../scripts/store/test_ios_release.py) | Offline tests against a fake App Store Connect behind `asc`: disabled/invalid switches, tag mismatch, validation mode, export compliance (project and archived `Info.plist`), version creation (left alone when it exists, refused while another version is in flight or not newer), preflight, build wait timeout/failure/ambiguity/mismatch/old build/missing upload date, selection read-back and recheck before any review write, listing, What's New (strict only when its switch is on), screenshots (RGB copies of PNGs that store alpha, the committed files unchanged), creative assets, products and the items a draft may hold, product creation (each type, finishing an interrupted product, drift warnings, failed checks before any write, plan-only validation runs, new products reaching review, only iOS products submitted), idempotent reruns, the asc runner (credentials, read-only mode, redaction, pinned checksum), the read-only export, log redaction, release notes, the printed switches |
| [`scripts/release_notes.py`](../scripts/release_notes.py) | GitHub Release notes from `RELEASE_NOTES.md` (generated notes when the tag has no section or the file is missing) |

The **Store release checks** workflow installs and checks the pinned `asc`
(so a bad pin fails on the pull request), then runs the offline tests, the
catalog check and a dry run of the listing and screenshots on every pull request
that touches this tooling, and reruns the release's offline tests and
release-notes step on a copy of the checkout without `RELEASE_NOTES.md`, which
must pass because a missing file only means generated notes. Every iOS Release
run also runs the offline tests first, except the ones that check the
repository's listing and screenshot files (`STORE_TESTS_SKIP_REPOSITORY_FILES=1`);
the check that `RELEASE_NOTES.md` repeats no section runs only when the file
exists. The store scripts need only the standard library, except Pillow for the
screenshots' RGB copies (`requirements-store.txt`, installed with
`--require-hashes`): the Store release checks always install it and run the
flattening tests with `STORE_TESTS_NEED_PILLOW=1`, and release jobs install it
only when `IOS_UPLOAD_SCREENSHOTS` is on (elsewhere those tests are skipped).
Release jobs install the pinned `asc` only when they talk to App Store Connect,
and run the store scripts unbuffered (`python3 -u`) so the build wait's progress
shows live. Dependabot proposes monthly updates for the pinned GitHub Actions,
the Swift packages, the store tooling's Pillow, the fuzzers' Atheris and the
website's npm tooling; `asc` is pinned by hand in `asc_cli.py` (see above).

## Releasing

1. Bump `MARKETING_VERSION` in the Xcode project (every target) to the new version.
2. If the listing, What's New, screenshots or review will be uploaded, the X.Y.Z
   App Store version must exist. With `IOS_CREATE_VERSION` on, the tag run
   creates it; otherwise create it in App Store Connect first (it stays in
   *Prepare for Submission*). The run stops before building without it. Update
   `store/metadata/ios/<locale>/whats_new.txt`.
3. Optionally add one `## vX.Y.Z` section to `RELEASE_NOTES.md`.
4. New in-app purchase or subscription: add it to `store/catalog/products.json`
   with its `ios` fields and review screenshot (and, for a tip, to `TipProductID`
   and the StoreKit configuration), turn on `IOS_CREATE_PRODUCTS` (and
   `IOS_SUBMIT_REVIEW` to send it with the version). A validation run (step 5)
   prints what the tag would create.
5. Run **iOS Release → Run workflow** with `marketing_version` = the new version
   to validate signing and export (nothing is uploaded).
6. Turn on only the switches you want (start with `IOS_RELEASE_AUTOMATION` +
   `IOS_UPLOAD_BUILD`, plus `IOS_CREATE_VERSION` if the run should create the
   App Store version; leave `IOS_SUBMIT_REVIEW` and `IOS_PUBLIC_RELEASE` off until
   you intend to submit).
7. Push the tag: `git tag -a vX.Y.Z -m "PCM Notes X.Y.Z" && git push origin vX.Y.Z`.

## Dry run

```bash
python3 -m pip install --require-hashes -r requirements-store.txt  # Pillow, for the flattening tests
python3 -m unittest scripts/store/test_ios_release.py
python3 scripts/store/print_gates.py
python3 scripts/store/validate_catalog.py
# Read-only against App Store Connect (needs the APP_STORE_CONNECT_* variables and asc on PATH):
ASC_READ_ONLY=1 python3 scripts/store/asc_products.py plan
UPLOAD_LISTING=true UPLOAD_SCREENSHOTS=true SUBMIT_IOS_REVIEW=true \
  python3 scripts/store/asc_release.py --dry-run --build-id DRY-RUN --build-number 1
python3 scripts/release_notes.py v1.0.1
```
