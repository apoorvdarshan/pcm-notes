# iOS testing

The **iOS tests** GitHub Actions workflow builds the Debug app once and runs the
`PCMNotesTests` unit tests and `PCMNotesUITests` UI tests on iOS simulators: the unit
tests and the iPhone UI tests on an iPhone, and the iPad-only reader tests
(`iPadReaderUITests`) on an iPad, so every test runs exactly once. It runs for
pull requests and pushes to `main` that change the app (`ios/`), the note images it
bundles (`physics/`, `chemistry/`, `mathematics/`) or the test tooling, and by
hand. The iOS Release workflow also runs it before uploading a build. It never
archives, signs, uploads or calls App Store Connect.

## Run locally

On a Mac with Xcode 26.6 or newer and an iOS 17.6 or newer Simulator runtime:

```sh
bash scripts/test-ios.sh
```

The script creates its own iPhone and iPad simulators and deletes them afterwards;
existing simulators and devices are never touched. Builds and package checkouts
are reused under
`~/Library/Developer/Xcode/DerivedData/PCMNotes-simulator-tests`, and results go to a
new `TestResults` subdirectory (printed at the end). Override
`IOS_TEST_DERIVED_DATA`, `IOS_TEST_PACKAGES` or `IOS_TEST_RESULTS` when needed; an
existing result bundle is never overwritten. The run fails when any test fails, a
run executes zero tests (unit or UI), or a test is skipped.

GitHub runs Xcode 27 on the `xcode-27` runner, caches the pinned Swift packages
(keyed on `Package.resolved`, resolved with
`-onlyUsePackageVersionsFromResolvedFile`) and checks out only the app and the note
images, not the website or marketing images. Tests run serially, without retries.
A newer push to a pull request cancels its older run; `main` runs are never
cancelled. On failure, the `.xcresult` bundles (with UI test screenshots) and logs
are kept as an artifact for seven days.

## What is covered

- **Unit tests** (Swift Testing): every chapter's bundled PDF exists with the page
  count the catalog shows, the bundle holds exactly the catalog's chapters and the
  generated manifest matches it; chapter search by title and number; resume pages
  and progress text; reader paging in continuous and two-page spread modes,
  including the counter, spoken position and slider; the tip store's catalog
  states, every purchase outcome, the in-flight guard and RevenueCat error mapping;
  app identity, privacy manifest, appearance and legal pages; the test launch
  controls.
- **UI tests** (XCTest): launch into the Physics index; the five tabs and a tab bar
  that stays expanded while scrolling; each subject's chapter index; search by
  title, number and no match; opening and closing the reader, paging, jumping with
  the slider, and resuming at the saved page after a relaunch; appearance choices
  and their persistence; support contacts, tip prices, a tip purchase and the
  offline tip-price retry; About's archive summary, version and legal pages; on
  iPad, the two-page spread, paging by pairs and remembering the layout.

## App Store captures

The manual **iOS tests** workflow can also retain real iPhone and iPad PNG
captures as the `ios-store-captures` artifact. Turn on **Also capture the iPhone
and iPad App Store scenes** when dispatching the workflow. It uses the same
unsigned Debug build as the tests and never uploads to the App Store.

## Test mode

UI tests launch the app with `--ui-testing` (and usually `--ui-testing-reset`).
Only in Debug simulator builds (`TestEnvironment.swift`), this starts from empty
reading progress and preferences and replaces StoreKit/RevenueCat with a local tip
store that has fixed prices and never charges (`--ui-testing-tips=` and
`--ui-testing-tip-outcome=` choose its behavior). When XCTest hosts the app for
unit tests, RevenueCat is skipped the same way. Release builds and devices always
use RevenueCat, so nothing here can reset data or bypass the App Store for users.

Not covered: real StoreKit purchases (use the `PCMNotes.storekit` configuration in
Xcode or a sandbox account), PDF rendering quality, sharing, and older iOS
versions; those need manual or device testing.
