#!/bin/bash
# Unsigned Debug simulator tests for PCM Notes. Never archives, signs or uploads.
#
#   1. builds the app and both test bundles once,
#   2. runs the unit tests and every UI test except the iPad-only ones on an iPhone,
#   3. runs the iPad-only UI tests (iPadReaderUITests) on an iPad,
#   4. fails if either run failed, ran no tests, or skipped a test.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

derived_data="${IOS_TEST_DERIVED_DATA:-$HOME/Library/Developer/Xcode/DerivedData/PCMNotes-simulator-tests}"
packages="${IOS_TEST_PACKAGES:-$derived_data/SourcePackages}"
results="${IOS_TEST_RESULTS:-$derived_data/TestResults/$(date -u +%Y%m%dT%H%M%SZ)-$$}"
ipad_only="PCMNotesUITests/iPadReaderUITests"
mkdir -p "$results"
for bundle in Build iPhone iPad; do
  if [[ -e "$results/$bundle.xcresult" ]]; then
    echo 'Choose an unused IOS_TEST_RESULTS directory; existing results are preserved.' >&2
    exit 1
  fi
done

xcodebuild -version

# The app bundles PDFs generated from these note images (BuildTools/GenerateOfflineNotes.swift).
for subject in physics chemistry mathematics; do
  if ! compgen -G "$subject/*/*.jpg" > /dev/null; then
    echo "Missing $subject/ note images: the app bundles every chapter as a PDF built from them." >&2
    exit 1
  fi
done

# Create task-owned simulators: never erase or reset a developer's existing ones.
iphone_id=""
ipad_id=""
cleanup() {
  # Only the simulators created by this invocation are removed.
  for id in $iphone_id $ipad_id; do
    xcrun simctl shutdown "$id" >/dev/null 2>&1 || true
    xcrun simctl delete "$id" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

read -r runtime iphone_type ipad_type < <(python3 - <<'PY'
import json, subprocess
data = json.loads(subprocess.check_output(['xcrun', 'simctl', 'list', '--json']))
def version(runtime):
    return tuple(int(part) for part in runtime['version'].split('.'))
runtimes = [r for r in data['runtimes'] if r.get('isAvailable') and
            '.iOS-' in r['identifier'] and version(r) >= (17, 6)]
if not runtimes:
    raise SystemExit('Install an iOS 17.6 or newer Simulator runtime in Xcode.')
iphone_names = ['iPhone 17 Pro', 'iPhone 16 Pro', 'iPhone 17']
ipad_names = ['iPad Pro 13-inch (M5)', 'iPad Pro 13-inch (M4)', 'iPad Air 13-inch (M3)']
def offers(runtime, names):
    return any(d['name'] in names for d in runtime.get('supportedDeviceTypes', []))
# A device-specific runtime can be newer than the general one (iOS 27.1 ships only the
# iPhone Duo, a 386x678 pt screen), so take the newest runtime that offers the preferred
# iPhone and iPad instead of the newest runtime overall.
runtime = max((r for r in runtimes if offers(r, iphone_names) and offers(r, ipad_names)),
              key=version, default=max(runtimes, key=version))
types = runtime.get('supportedDeviceTypes', [])
def pick(family, preferred):
    devices = [d for d in types if d.get('productFamily') == family]
    if not devices:
        raise SystemExit(f'The iOS {runtime["version"]} runtime has no {family} simulator.')
    by_name = {d['name']: d for d in devices}
    return next((by_name[name] for name in preferred if name in by_name), devices[0])
iphone = pick('iPhone', iphone_names)
ipad = pick('iPad', ipad_names)
print(runtime['identifier'], iphone['identifier'], ipad['identifier'])
PY
) || true
if [[ -z "${ipad_type:-}" ]]; then
  echo 'Could not choose iPhone and iPad simulators.' >&2
  exit 1
fi
iphone_id="$(xcrun simctl create 'PCM Notes CI iPhone' "$iphone_type" "$runtime")"
ipad_id="$(xcrun simctl create 'PCM Notes CI iPad' "$ipad_type" "$runtime")"
echo "Simulators: iPhone $iphone_type ($iphone_id), iPad $ipad_type ($ipad_id) on $runtime"

common=(
  -project ios/PCMNotes/PCMNotes.xcodeproj
  -scheme PCMNotes
  -configuration Debug
  -derivedDataPath "$derived_data"
  -clonedSourcePackagesDirPath "$packages"
  -onlyUsePackageVersionsFromResolvedFile
  -enableCodeCoverage YES
)
# -collect-test-diagnostics never: otherwise xcodebuild can spend 10 minutes after
# the tests finish timing out while collecting simulator diagnostics. Failure
# screenshots and logs are still kept in the result bundles.
run_tests=(
  -destination-timeout 120
  -parallel-testing-enabled NO
  -maximum-concurrent-test-simulator-destinations 1
  -collect-test-diagnostics never
)

# Both simulators share the iOS Simulator SDK and architecture, so one build serves both.
# Each simulator boots just before its tests, and only one runs at a time: booting
# one while building or testing on the other made the CI job 2-3 minutes slower, and
# with the iPhone still booted the iPad's first UI test intermittently timed out
# launching the app (both seen on CI in October 2026). `bootstatus -b` boots the
# simulator and waits until it has finished booting, so no launch races the boot.
xcodebuild build-for-testing "${common[@]}" \
  -destination "platform=iOS Simulator,id=$iphone_id" \
  -resultBundlePath "$results/Build.xcresult" \
  CODE_SIGNING_ALLOWED=NO \
  2>&1 | tee "$results/build.log"

status=0
xcrun simctl bootstatus "$iphone_id" -b
xcodebuild test-without-building "${common[@]}" "${run_tests[@]}" \
  -destination "platform=iOS Simulator,id=$iphone_id" \
  -skip-testing:"$ipad_only" \
  -resultBundlePath "$results/iPhone.xcresult" \
  2>&1 | tee "$results/iphone.log" || status=1

xcrun simctl shutdown "$iphone_id" || true
xcrun simctl bootstatus "$ipad_id" -b
xcodebuild test-without-building "${common[@]}" "${run_tests[@]}" \
  -destination "platform=iOS Simulator,id=$ipad_id" \
  -only-testing:"$ipad_only" \
  -resultBundlePath "$results/iPad.xcresult" \
  2>&1 | tee "$results/ipad.log" || status=1

python3 scripts/ios_test_summary.py "$results/iPhone.xcresult" --require-unit --require-ui || status=1
python3 scripts/ios_test_summary.py "$results/iPad.xcresult" --require-ui || status=1
# Line coverage of the app from the iPhone run (unit tests plus most UI tests).
# Informational only: it never changes the result.
echo "App code coverage (iPhone run):"
xcrun xccov view --report --only-targets "$results/iPhone.xcresult" || true
echo "Results: $results"
exit "$status"
