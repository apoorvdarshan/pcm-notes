#!/bin/bash
# Capture real app screens from the unsigned debug build produced by iOS tests.
set -euo pipefail

app="${IOS_TEST_DERIVED_DATA:?}/Build/Products/Debug-iphonesimulator/PCMNotes.app"
output="${IOS_STORE_CAPTURES:?}"
bundle="com.apoorvdarshan.iitjee"
device=""
cleanup() {
  if [[ -n "$device" ]]; then
    xcrun simctl shutdown "$device" >/dev/null 2>&1 || true
    xcrun simctl delete "$device" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ ! -f "$app/PCMNotes" ]]; then
  echo "Missing simulator app: $app" >&2
  exit 1
fi

capture() {
  local name="$1" subject="$2" page="$3" layout="$4"
  SIMCTL_CHILD_PCM_CAPTURE_SUBJECT="$subject" \
    SIMCTL_CHILD_PCM_CAPTURE_PAGE="$page" \
    SIMCTL_CHILD_PCM_CAPTURE_LAYOUT="$layout" \
    xcrun simctl launch --terminate-running-process "$device" "$bundle" \
      --ui-testing --ui-testing-reset -AppleLanguages '(en)' -AppleLocale en_US
  # Wait for the reader's fullscreen presentation and its first PDFKit frame.
  sleep 4
  xcrun simctl io "$device" screenshot "$output/$family/$name.png"
}

for family in iphone ipad; do
  read -r runtime type < <(python3 - "$family" <<'PY'
import json, subprocess, sys

family = sys.argv[1]
preferred = {
    "iphone": ["iPhone 15 Pro Max", "iPhone 15 Plus", "iPhone 14 Pro Max",
               "iPhone 16 Pro Max", "iPhone 17 Pro Max", "iPhone 18 Pro Max"],
    "ipad": ["iPad Pro (12.9-inch) (6th generation)", "iPad Pro (12.9-inch) (5th generation)",
             "iPad Pro 13-inch (M5)", "iPad Pro 13-inch (M4)", "iPad Air 13-inch (M3)"],
}[family]
data = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "-j", "runtimes"]))
runtimes = sorted((r for r in data["runtimes"] if r["isAvailable"] and ".iOS-" in r["identifier"]),
                  key=lambda r: tuple(map(int, r["version"].split("."))), reverse=True)
for name in preferred:
    for runtime in runtimes:
        for device in runtime["supportedDeviceTypes"]:
            if device["name"] == name:
                print(runtime["identifier"], device["identifier"])
                sys.exit(0)
raise SystemExit(f"No compatible {family} simulator for App Store captures")
PY
)
  mkdir -p "$output/$family"
  device="$(xcrun simctl create "PCM Notes Store $family" "$type" "$runtime")"
  xcrun simctl bootstatus "$device" -b
  xcrun simctl install "$device" "$app"
  xcrun simctl ui "$device" appearance light
  xcrun simctl status_bar "$device" override --time '9:41' --dataNetwork wifi \
    --wifiMode active --wifiBars 3 --cellularMode active --cellularBars 4 --batteryLevel 100
  layout=continuous
  if [[ "$family" == ipad ]]; then layout=spread; fi
  capture 01-reader physics 2 "$layout"
  capture 02-chapters physics '' continuous
  capture 03-chemistry chemistry 3 continuous
  capture 04-mathematics mathematics 2 "$layout"
  cleanup
  device=""
done
