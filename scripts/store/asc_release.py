#!/usr/bin/env python3
"""App Store Connect listing, What's New, screenshots, creative assets and submit-for-review (gated in CI).

Every App Store Connect call goes through the pinned App Store Connect CLI
(`asc`, see asc_cli.py); this script decides what may change and checks the
results.

The listing lives in the repository: store/metadata/ios/<locale>/*.txt holds every
App Store locale (exported from the live listing), and the screenshots are in
store/metadata/screenshots/ios/<display size>/. Each operation only runs when its switch
is on (see store/README.md); with every switch off this script does nothing.

Submitting for review covers the App Store version and any Ready-to-Submit
in-app purchases listed in store/catalog/products.json. They all join one draft
review submission as Review Submission Items (subscriptions through their
`subscriptionVersions`, in-app purchases such as the tip jar through their
`inAppPurchaseVersions`) only after this run's build is rechecked, and reach App
Review together with a single submit. A reused draft is submitted only when it
holds nothing but this version and versions of catalog products.

Reruns are idempotent: only fields that differ are sent, and once the version is
in a draft review submission (READY_FOR_REVIEW) or waiting for review, whatever
can no longer change is only compared with the repository; a difference stops
the run.

`--create-version` (IOS_CREATE_VERSION) runs first on a release run, before the
read-only preflight: it adds the App Store version for the tag when App Store
Connect does not have it yet, and leaves an existing one alone. It refuses when
another version is still on its way to the store or the tag's version is not
newer than every existing one."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asc_cli  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE_ID = "com.apoorvdarshan.iitjee"
# PCM Notes: JEE & NEET, the App Store Connect app record for DEFAULT_BUNDLE_ID.
# Every run checks that this record carries exactly that bundle id before
# touching anything.
ASC_APP_ID = "6792598227"
PLATFORM = "IOS"
PRIMARY_LOCALE = "en-US"
# App Store Connect per screenshot-set limit.
APP_SCREENSHOT_SET_MAX = 10
# Product page creative assets: the folder under --creative-dir, the placement
# type, and a label for logs. Each folder holds at most one image.
CREATIVE_SLOTS = (
    ("header", "PRODUCT_PAGE_HEADER_ASSET", "product page header"),
    ("search-results", "APP_STORE_SEARCH_RESULTS_ASSET", "search results"),
)
CREATIVE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
# Library image states: still being reviewed, not sent to review yet, or
# finished without approval.
CREATIVE_PENDING_STATES = {"WAITING_FOR_REVIEW", "IN_REVIEW", "ACCEPTED"}
CREATIVE_UNSUBMITTED_STATES = {"PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW", "COMPLETE", "UPLOAD_COMPLETE"}
CREATIVE_DEAD_STATES = {"REJECTED", "FAILED", "ARCHIVED"}
EDITABLE_VERSION_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "DEVELOPER_REJECTED",
    "REJECTED",
    "METADATA_REJECTED",
    "INVALID_BINARY",
}
# READY_FOR_REVIEW means the version already sits in a draft review submission
# (for example after a failed submit). It can still be submitted, and its text
# (listing, What's New, app information) can still change; its screenshots cannot.
SUBMITTABLE_VERSION_STATES = EDITABLE_VERSION_STATES | {"READY_FOR_REVIEW"}
TEXT_EDITABLE_VERSION_STATES = EDITABLE_VERSION_STATES | {"READY_FOR_REVIEW"}
SCREENSHOT_EDITABLE_VERSION_STATES = EDITABLE_VERSION_STATES
# A rerun ("Re-run failed jobs") can find the version further along than the
# first attempt left it. In these states it only checks, read-only, that App
# Store Connect already holds the repository's values, and stops on a difference.
TEXT_FROZEN_VERSION_STATES = {"WAITING_FOR_REVIEW"}
SCREENSHOT_FROZEN_VERSION_STATES = {"READY_FOR_REVIEW", "WAITING_FOR_REVIEW"}
# App information (name, subtitle, privacy URL) gets an editable copy while a new
# version is being prepared; the live copy cannot change.
EDITABLE_APP_INFO_STATES = TEXT_EDITABLE_VERSION_STATES
LIVE_VERSION_STATES = {"READY_FOR_SALE", "READY_FOR_DISTRIBUTION"}
# App information records that belong to the live (or a replaced) version.
SETTLED_APP_INFO_STATES = LIVE_VERSION_STATES | {"REPLACED_WITH_NEW_INFO", "REPLACED_WITH_NEW_VERSION"}
# Versions in these states are finished. Any other state means the version is
# still on its way to the store, and App Store Connect allows only one such
# version per platform, so no new version is created next to it.
FINISHED_VERSION_STATES = {
    "READY_FOR_SALE",
    "REPLACED_WITH_NEW_VERSION",
    "REMOVED_FROM_SALE",
    "DEVELOPER_REMOVED_FROM_SALE",
}
EDIT, VERIFY = "edit", "verify"


def metadata_access(state: str, *, screenshots: bool = False) -> str | None:
    """EDIT when a version in `state` takes the change, VERIFY when it is frozen
    but a rerun may confirm App Store Connect already matches, else None.

    Screenshots and creative assets (screenshots=True) lock as soon as the
    version is in a draft review submission; text stays editable there."""
    editable = SCREENSHOT_EDITABLE_VERSION_STATES if screenshots else TEXT_EDITABLE_VERSION_STATES
    frozen = SCREENSHOT_FROZEN_VERSION_STATES if screenshots else TEXT_FROZEN_VERSION_STATES
    if state in editable:
        return EDIT
    if state in frozen:
        return VERIFY
    return None


def same_text(current: object, wanted: str) -> bool:
    """Compare stored text with the repository's, ignoring line endings and outer whitespace."""
    def normalize(value: object) -> str:
        return str(value or "").replace("\r\n", "\n").strip()

    return normalize(current) == normalize(wanted)


class ScreenshotSet(NamedTuple):
    display_type: str
    directory: str  # relative to the repository root
    sizes: frozenset[tuple[int, int]]

    @property
    def device_type(self) -> str:
        """asc's --device-type for the set: APP_IPHONE_67 -> IPHONE_67."""
        return self.display_type.removeprefix("APP_")


# These mockups are byte-identical to the screenshots live on the App Store for 1.0,
# so they are uploaded as-is.
SCREENSHOT_SETS = (
    ScreenshotSet(
        "APP_IPHONE_67",
        "store/metadata/screenshots/ios/6.7",
        frozenset({(1290, 2796), (2796, 1290), (1284, 2778), (2778, 1284)}),
    ),
    ScreenshotSet(
        "APP_IPAD_PRO_3GEN_129",
        "store/metadata/screenshots/ios/12.9",
        frozenset({(2048, 2732), (2732, 2048)}),
    ),
)


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def data(payload: Any) -> Any:
    return (payload or {}).get("data") if isinstance(payload, dict) else None


def rows(payload: Any) -> list[dict[str, Any]]:
    value = data(payload)
    return value if isinstance(value, list) else []


def attrs(row: dict[str, Any] | None) -> dict[str, Any]:
    return (row or {}).get("attributes") or {}


def related_id(row: dict[str, Any], name: str) -> str | None:
    return (((row.get("relationships") or {}).get(name) or {}).get("data") or {}).get("id")


def asc_value(text: str) -> str:
    """A flag value for asc, which reads a leading @ as @file:/@env: (escaped as @@)."""
    return "@" + text if text.startswith("@") else text


def load_credentials() -> tuple[str, str, str]:
    key_id = os.environ.get("APP_STORE_CONNECT_API_KEY_ID", "").strip()
    issuer_id = os.environ.get("APP_STORE_CONNECT_ISSUER_ID", "").strip()
    key_p8 = os.environ.get("APP_STORE_CONNECT_API_KEY_P8", "").strip()
    if not key_id or not issuer_id or not key_p8:
        fail(
            "missing ASC credentials — set APP_STORE_CONNECT_API_KEY_ID, "
            "APP_STORE_CONNECT_ISSUER_ID, APP_STORE_CONNECT_API_KEY_P8"
        )
    key_p8 = key_p8.replace("\\n", "\n")
    return key_id, issuer_id, key_p8


def asc_from_environment(*, read_only: bool = False) -> asc_cli.Asc:
    """The asc runner for this job; read_only=True makes asc refuse every write."""
    load_credentials()
    return asc_cli.Asc(read_only=read_only)


def call(asc: asc_cli.Asc, *args: str) -> Any:
    """Run asc and stop the release with a readable error when it fails."""
    try:
        return asc.run(*args)
    except asc_cli.AscError as exc:
        fail(str(exc))


def find_app(asc: asc_cli.Asc, bundle_id: str, app_id: str = ASC_APP_ID) -> str:
    """The pinned App Store Connect app (PCM Notes), after checking it carries exactly bundle_id."""
    record = data(call(asc, "apps", "view", "--id", app_id)) or {}
    if record.get("id") != app_id:
        fail(f"App Store Connect returned app {record.get('id')!r}, expected PCM Notes ({app_id})")
    found = attrs(record).get("bundleId")
    if found != bundle_id:
        fail(f"App Store Connect app {app_id} has bundle id {found!r}, expected PCM Notes ({bundle_id})")
    return app_id


def ios_app_store_versions(asc: asc_cli.Asc, app_id: str) -> list[dict[str, Any]]:
    return rows(call(asc, "versions", "list", "--app", app_id, "--platform", PLATFORM, "--paginate"))


def find_ios_app_store_version(
    asc: asc_cli.Asc, app_id: str, version_string: str
) -> dict[str, Any] | None:
    matches = [
        row for row in ios_app_store_versions(asc, app_id)
        if attrs(row).get("versionString") == version_string
    ]
    if len(matches) > 1:
        fail(
            f"ambiguous App Store versions for {version_string!r} on IOS — "
            f"got {len(matches)} records"
        )
    return matches[0] if matches else None


def get_ios_app_store_version(
    asc: asc_cli.Asc, app_id: str, version_string: str
) -> dict[str, Any]:
    version = find_ios_app_store_version(asc, app_id, version_string)
    if version is None:
        fail(
            f"no IOS App Store version {version_string!r} for app {app_id} "
            "(turn on IOS_CREATE_VERSION, or create the version in App Store Connect first)"
        )
    return version


def live_ios_version(asc: asc_cli.Asc, app_id: str) -> dict[str, Any] | None:
    """The version currently on the App Store, if any."""
    for row in ios_app_store_versions(asc, app_id):
        if attrs(row).get("appStoreState") in LIVE_VERSION_STATES:
            return row
    return None


def matching_locale(records: list[dict[str, Any]], locale: str) -> dict[str, Any] | None:
    found = [row for row in records if attrs(row).get("locale", locale) == locale]
    return found[0] if found else None


def find_version_localization(asc: asc_cli.Asc, version_id: str, locale: str) -> dict[str, Any] | None:
    payload = call(asc, "localizations", "list", "--version", version_id, "--locale", locale)
    return matching_locale(rows(payload), locale)


def version_localization(asc: asc_cli.Asc, version_id: str, locale: str) -> dict[str, Any]:
    localization = find_version_localization(asc, version_id, locale)
    if localization is None:
        fail(
            f"App Store version {version_id} has no {locale} localization "
            "(add that language in App Store Connect first)"
        )
    return localization


def app_infos(asc: asc_cli.Asc, app_id: str) -> list[dict[str, Any]]:
    return rows(call(asc, "apps", "info", "list", "--app", app_id))


def find_app_info_localization(
    asc: asc_cli.Asc, app_id: str, app_info_id: str, locale: str
) -> dict[str, Any] | None:
    payload = call(
        asc, "localizations", "list", "--type", "app-info", "--app", app_id, "--app-info", app_info_id,
        "--locale", locale,
    )
    return matching_locale(rows(payload), locale)


# (App Store Connect attribute, file name, character limit or None)
APP_INFO_FIELDS = (
    ("name", "name.txt", 30),
    ("subtitle", "subtitle.txt", 30),
    ("privacyPolicyUrl", "privacy_url.txt", None),
)
LISTING_FIELDS = (
    ("description", "description.txt", 4000),
    ("keywords", "keywords.txt", 100),
    ("promotionalText", "promotional_text.txt", 170),
    ("marketingUrl", "marketing_url.txt", None),
    ("supportUrl", "support_url.txt", None),
)
WHATS_NEW_FIELD = ("whatsNew", "whats_new.txt", 4000)
# The `asc localizations update` flag for each attribute.
ASC_FLAGS = {
    "name": "--name",
    "subtitle": "--subtitle",
    "privacyPolicyUrl": "--privacy-policy-url",
    "description": "--description",
    "keywords": "--keywords",
    "promotionalText": "--promotional-text",
    "marketingUrl": "--marketing-url",
    "supportUrl": "--support-url",
    "whatsNew": "--whats-new",
}
# Files App Store Connect requires for every listed locale.
REQUIRED_LISTING_FILES = ("name.txt", "description.txt", "keywords.txt", "support_url.txt", "privacy_url.txt")
LOCALE_PATTERN = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,4})*")


def update_args(changed: dict[str, str]) -> list[str]:
    """`asc localizations update` flags for the changed attributes."""
    args: list[str] = []
    for field, value in changed.items():
        args += [ASC_FLAGS[field], asc_value(value)]
    return args


def listing_locales(metadata_dir: Path) -> list[str]:
    """Every App Store locale kept in the repository (one directory each)."""
    root = metadata_dir / "ios"
    if not root.is_dir():
        return []
    return sorted(
        path.name for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


def app_info_state(info: dict[str, Any]) -> str:
    return attrs(info).get("state") or attrs(info).get("appStoreState") or ""


def upload_app_info(
    asc: asc_cli.Asc,
    *,
    app_id: str,
    locale: str,
    metadata_dir: Path,
    frozen_state: str | None = None,
) -> None:
    """Name, subtitle and privacy URL live on the app information, not the version.

    While a new version is being prepared, App Store Connect keeps a copy of the
    app information for it next to the live one; only fields that differ from that
    copy are sent. When the copy cannot change (no new version, or `frozen_state`
    for a version already submitted), differing values stop the run."""
    loc_dir = metadata_dir / "ios" / locale
    wanted = {
        field: value
        for field, filename, _ in APP_INFO_FIELDS
        if (value := read_text(loc_dir / filename))
    }
    if not wanted:
        return
    infos = app_infos(asc, app_id)
    pending = [info for info in infos if app_info_state(info) not in SETTLED_APP_INFO_STATES]
    if len(pending) > 1:
        fail(f"ambiguous: {len(pending)} app information records for a new version")
    live = [info for info in infos if app_info_state(info) in LIVE_VERSION_STATES]
    target = (pending or live or infos or [None])[0]
    if target is None:
        fail("app has no appInfos")
    loc = find_app_info_localization(asc, app_id, target["id"], locale)
    current = attrs(loc)
    changed = {field: value for field, value in wanted.items() if not same_text(current.get(field), value)}
    if not changed:
        print(f"  app information ({locale}) already matches the repository")
        return
    if frozen_state:
        fail(
            f"{', '.join(sorted(changed))} ({locale}) differ from App Store Connect, but the "
            f"version is already {frozen_state}, so its app information can no longer change"
        )
    if app_info_state(target) not in EDITABLE_APP_INFO_STATES or loc is None:
        fail(
            f"{', '.join(sorted(changed))} ({locale}) differ from App Store Connect, but no "
            "editable app information exists for this locale; create the new version (and "
            "language) in App Store Connect first"
        )
    call(asc, "localizations", "update", "--type", "app-info", "--id", loc["id"], *update_args(changed))
    print(f"  updated app information ({locale}): {', '.join(changed)}")


def check_whats_new_is_new(
    asc: asc_cli.Asc, *, app_id: str, version_id: str, locale: str, text: str
) -> None:
    """Refuse to publish the live version's What's New again (a forgotten update)."""
    live = live_ios_version(asc, app_id)
    if live is None or live.get("id") == version_id:
        return
    loc = find_version_localization(asc, live["id"], locale)
    previous = attrs(loc).get("whatsNew") or ""
    if previous.strip() and previous.strip() == text.strip():
        live_version = attrs(live).get("versionString")
        fail(
            f"store/metadata/ios/{locale}/whats_new.txt still holds the What's New of live "
            f"version {live_version}; write this release's notes before uploading"
        )


def upload_listing(
    asc: asc_cli.Asc,
    *,
    app_id: str,
    version_id: str,
    locale: str,
    metadata_dir: Path,
    include_listing: bool = True,
    include_whats_new: bool = False,
    frozen_state: str | None = None,
) -> None:
    """Update one locale. Listing text and What's New are separate gates.

    Only fields that differ from App Store Connect are sent. With `frozen_state`
    (the version is already submitted) nothing is sent: any difference stops."""
    loc_dir = metadata_dir / "ios" / locale
    if include_listing:
        upload_app_info(
            asc, app_id=app_id, locale=locale, metadata_dir=metadata_dir,
            frozen_state=frozen_state,
        )

    version_loc = version_localization(asc, version_id, locale)
    fields = (LISTING_FIELDS if include_listing else ()) + (
        (WHATS_NEW_FIELD,) if include_whats_new else ()
    )
    wanted: dict[str, str] = {}
    for field, filename, _ in fields:
        value = read_text(loc_dir / filename)
        if value:
            wanted[field] = value
    if include_whats_new and "whatsNew" in wanted:
        check_whats_new_is_new(
            asc, app_id=app_id, version_id=version_id, locale=locale,
            text=wanted["whatsNew"],
        )
    current = attrs(version_loc)
    changed = {field: value for field, value in wanted.items() if not same_text(current.get(field), value)}
    if not changed:
        if wanted:
            print(f"  version localization ({locale}) already matches the repository")
        return
    if frozen_state:
        fail(
            f"{', '.join(sorted(changed))} ({locale}) differ from App Store Connect, but the "
            f"version is already {frozen_state}, so they can no longer change"
        )
    call(asc, "localizations", "update", "--id", version_loc["id"], *update_args(changed))
    print(f"  updated version localization ({locale}): {', '.join(changed)}")


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        fail(f"{path} is not a PNG file")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def screenshot_files(spec: ScreenshotSet, root: Path = ROOT) -> list[Path]:
    return sorted((root / spec.directory).glob("*.png"))


def validate_screenshots(root: Path = ROOT) -> None:
    for spec in SCREENSHOT_SETS:
        directory = root / spec.directory
        files = screenshot_files(spec, root)
        if not files:
            fail(
                f"screenshot upload is on, but {directory} has no PNG files for "
                f"{spec.display_type}"
            )
        if len(files) > APP_SCREENSHOT_SET_MAX:
            fail(f"{len(files)} PNGs in {directory}; App Store Connect allows {APP_SCREENSHOT_SET_MAX}")
        for path in files:
            size = png_size(path)
            if size not in spec.sizes:
                accepted = ", ".join(f"{w}x{h}" for w, h in sorted(spec.sizes))
                fail(f"{path} is {size[0]}x{size[1]}; {spec.display_type} accepts {accepted}")


def file_checksum(raw: bytes) -> str:
    """The MD5 App Store Connect records as a screenshot's sourceFileChecksum."""
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()


def live_screenshot_sets(
    asc: asc_cli.Asc, *, app_id: str, version_id: str, locale: str
) -> dict[str, list[dict[str, Any]]]:
    """Display type -> the screenshots App Store Connect holds in that set, in order."""
    payload = call(
        asc, "screenshots", "list", "--app", app_id, "--version-id", version_id, "--locale", locale,
        "--platform", PLATFORM,
    )
    return {
        attrs(entry.get("set")).get("screenshotDisplayType"): list(entry.get("screenshots") or [])
        for entry in (payload or {}).get("sets") or []
    }


def screenshot_checksums(shots: Iterable[dict[str, Any]]) -> list[Any]:
    return [attrs(shot).get("sourceFileChecksum") for shot in shots]


def wait_for_screenshot_processing(
    asc: asc_cli.Asc,
    *,
    app_id: str,
    version_id: str,
    locale: str,
    display_type: str,
    timeout_seconds: int = 600,
    poll_seconds: int = 10,
    now: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[dict[str, Any]]:
    """App Store Connect processes screenshots after upload; review needs them COMPLETE.

    asc already waits for each upload; this reads the set back until every
    screenshot in it is COMPLETE and returns them, in order."""
    now = now or time.time
    sleep = sleep or time.sleep
    deadline = now() + timeout_seconds
    while True:
        shots = live_screenshot_sets(asc, app_id=app_id, version_id=version_id, locale=locale).get(display_type) or []
        states = [(attrs(shot).get("assetDeliveryState") or {}).get("state") for shot in shots]
        if "FAILED" in states:
            fail(f"App Store Connect could not process a {display_type} screenshot")
        if states and all(state == "COMPLETE" for state in states):
            return shots
        if now() > deadline:
            fail(f"{display_type} screenshots were still processing after {timeout_seconds}s")
        sleep(poll_seconds)


def upload_screenshot_set(
    asc: asc_cli.Asc,
    *,
    app_id: str,
    version_id: str,
    locale: str,
    spec: ScreenshotSet,
    live: dict[str, list[dict[str, Any]]],
    root: Path = ROOT,
    frozen_state: str | None = None,
) -> bool:
    """Replace one display type's screenshots. Returns False when already current.

    Sets are compared by checksum, in file-name order (App Store Connect keeps
    the file names a set was first uploaded with). With `frozen_state`
    (screenshots can no longer change) nothing is written: a set that already
    matches passes, anything else stops."""
    pngs = screenshot_files(spec, root)
    if not pngs:
        fail(f"no PNG screenshots in {root / spec.directory}")
    if len(pngs) > APP_SCREENSHOT_SET_MAX:
        fail(f"too many PNGs ({len(pngs)}) for {spec.display_type} (max {APP_SCREENSHOT_SET_MAX})")

    wanted = [file_checksum(path.read_bytes()) for path in pngs]
    if screenshot_checksums(live.get(spec.display_type) or []) == wanted:
        print(f"  {spec.display_type}: the {len(pngs)} screenshots already match")
        return False
    if frozen_state:
        fail(
            f"{spec.display_type} screenshots differ from App Store Connect, but the version "
            f"is already {frozen_state}, so its screenshots can no longer change"
        )
    # asc uploads every file in --path, so it gets a folder holding exactly
    # these PNGs (same names, so the same order). --replace deletes the set's
    # screenshots before uploading; asc uploads the files in name order and
    # waits for App Store Connect to process each one. If an upload fails
    # part-way, fix the error and re-run: the set is replaced again.
    with tempfile.TemporaryDirectory(prefix="asc-screenshots-") as staging:
        for path in pngs:
            shutil.copyfile(path, Path(staging) / path.name)
        call(
            asc,
            "screenshots", "upload", "--app", app_id, "--version-id", version_id, "--locale", locale,
            "--path", staging, "--device-type", spec.device_type, "--replace", "--confirm",
        )
    shots = wait_for_screenshot_processing(
        asc, app_id=app_id, version_id=version_id, locale=locale, display_type=spec.display_type
    )
    if screenshot_checksums(shots) != wanted:
        fail(f"after uploading, App Store Connect's {spec.display_type} set does not match the repository")
    print(f"  {spec.display_type}: {len(pngs)} screenshots processed")
    return True


def upload_screenshots(
    asc: asc_cli.Asc,
    *,
    app_id: str,
    version_id: str,
    locale: str = PRIMARY_LOCALE,
    root: Path = ROOT,
    frozen_state: str | None = None,
) -> None:
    # Other languages fall back to the primary locale's screenshots.
    version_localization(asc, version_id, locale)
    live = live_screenshot_sets(asc, app_id=app_id, version_id=version_id, locale=locale)
    for spec in SCREENSHOT_SETS:
        upload_screenshot_set(
            asc, app_id=app_id, version_id=version_id, locale=locale, spec=spec, live=live, root=root,
            frozen_state=frozen_state,
        )


def creative_sources(creative_dir: Path) -> list[tuple[str, str, Path]]:
    """(placement type, label, image) for every creative slot that has an image."""
    found = []
    for folder, placement_type, label in CREATIVE_SLOTS:
        slot_dir = creative_dir / folder
        images = sorted(
            p for p in slot_dir.glob("*") if p.is_file() and p.suffix.lower() in CREATIVE_EXTENSIONS
        ) if slot_dir.is_dir() else []
        if len(images) > 1:
            fail(f"the {label} slot takes one image; {slot_dir} has {len(images)}")
        if images:
            found.append((placement_type, label, images[0]))
    if not found:
        fail(f"creative asset upload is on, but {creative_dir} has no header or search results image")
    return found


def library_images(asc: asc_cli.Asc, library_id: str) -> list[dict[str, Any]]:
    return rows(
        call(asc, "asset-library", "images", "list", "--library-id", library_id, "--category", "CREATIVE_ASSETS", "--paginate")
    )


def library_image_for(images: list[dict[str, Any]], path: Path) -> dict[str, Any] | None:
    """The newest usable library image uploaded from this exact file (name and size)."""
    size = path.stat().st_size
    matches = [
        image
        for image in images
        if attrs(image).get("fileName") == path.name
        and attrs(image).get("fileSize") == size
        and attrs(image).get("state") not in CREATIVE_DEAD_STATES
    ]
    rejected = [
        image
        for image in images
        if attrs(image).get("fileName") == path.name
        and attrs(image).get("fileSize") == size
        and attrs(image).get("state") == "REJECTED"
    ]
    if not matches and rejected:
        fail(
            f"App Review rejected {path.name}; replace the image (a new file name) and re-run. "
            f"Details: {json.dumps(attrs(rejected[0]).get('stateDetails'))}"
        )
    return max(matches, key=lambda image: attrs(image).get("createdDate") or "") if matches else None


def sync_creative_assets(
    asc: asc_cli.Asc,
    *,
    app_id: str,
    version_id: str,
    locale: str,
    creative_dir: Path,
    editable: bool,
) -> dict[str, str]:
    """Put the repository's header and search results images on the version.

    An image is uploaded to the Asset Library once (matched by file name and size)
    and reviewed on its own. Only approved images are placed, because a version
    that is already approved only takes approved assets; one that is still in
    review or not yet submitted is placed on a later run. Nothing is uploaded,
    sent to review or placed while the version is locked (editable=False): the
    step then passes only when nothing needs to change. Returns placement type ->
    outcome.
    """
    sources = creative_sources(creative_dir)
    library_id = (data(call(asc, "asset-library", "view", "--app", app_id)) or {}).get("id")
    if not library_id:
        fail(f"App Store Connect returned no Asset Library for app {app_id}")
    images = library_images(asc, library_id)
    version_loc = version_localization(asc, version_id, locale)
    outcomes: dict[str, str] = {}
    to_review: list[tuple[str, dict[str, Any]]] = []
    for placement_type, label, path in sources:
        image = library_image_for(images, path)
        if image is None:
            if not editable:
                fail(f"cannot upload the {label} image while the version waits for review")
            # asc waits for processing and prints a receipt (imageId, ready, state).
            receipt = call(asc, "asset-library", "images", "upload", "--library-id", library_id, "--file", str(path))
            if not receipt.get("imageId") or not receipt.get("ready"):
                fail(f"Apple did not finish processing the {label} image {path.name}: {json.dumps(receipt)}")
            image = {"id": receipt["imageId"], "attributes": {"fileName": path.name, "state": receipt.get("state")}}
            print(f"  uploaded {path.name} to the Asset Library ({image['id']})")
        state = attrs(image).get("state") or ""
        if state in CREATIVE_UNSUBMITTED_STATES or not state:
            if not editable:
                fail(
                    f"cannot send the {label} image {path.name} to App Review while the version waits for "
                    "review; re-run once the version can be edited, or submit it in App Store Connect"
                )
            to_review.append((label, image))
            outcomes[placement_type] = "submitted for review"
            continue
        if state in CREATIVE_PENDING_STATES:
            outcomes[placement_type] = f"in App Review ({state}); placed on a later run"
            print(f"  {label}: {path.name} is {state}; it is placed once App Review approves it")
            continue
        if state != "APPROVED":
            fail(f"the {label} image {path.name} is {state}; resolve it in App Store Connect")
        current = rows(
            call(
                asc, "localizations", "placements", "list", "--localization-id", version_loc["id"],
                "--placement-type", placement_type, "--include", "image",
            )
        )
        if any(related_id(row, "image") == image["id"] for row in current):
            outcomes[placement_type] = "already placed"
            print(f"  {label}: {path.name} is already on the product page")
            continue
        if not editable:
            fail(f"cannot change the {label} while the version waits for review")
        if current:
            call(
                asc, "localizations", "placements", "swap", "--localization-id", version_loc["id"],
                "--image-id", image["id"], "--placement-type", placement_type, "--placement-id", current[0]["id"],
                "--confirm",
            )
        else:
            call(
                asc, "localizations", "placements", "create", "--localization-id", version_loc["id"],
                "--image-id", image["id"], "--placement-type", placement_type, "--placement-group", "DEFAULT_PROFILE",
            )
        outcomes[placement_type] = "placed"
        print(f"  {label}: placed {path.name} on the product page")
    if to_review:
        submit_creative_review(asc, app_id, to_review)
    return outcomes


def submit_creative_review(asc: asc_cli.Asc, app_id: str, images: list[tuple[str, dict[str, Any]]]) -> str:
    """Send not-yet-reviewed creative images to App Review in a submission of their own.

    A draft an earlier attempt left behind holding only some of these images
    (or nothing yet) is finished instead of starting another one.
    """
    wanted = {image["id"] for _, image in images}
    submission_id = None
    for draft in draft_submission_ids(asc, app_id):
        held = {related_id(item, "appAssetLibraryImage") for item in submission_items(asc, draft)}
        if held <= wanted:
            submission_id = draft
            print(f"  finishing creative review submission {draft} from an earlier attempt")
            break
    if submission_id is None:
        submission = data(call(asc, "review", "submissions-create", "--app", app_id, "--platform", PLATFORM)) or {}
        submission_id = submission.get("id")
    if not submission_id:
        fail("asc did not report the new review submission's id")
    for label, image in images:
        call(
            asc, "review", "items-add", "--submission", submission_id, "--item-type", "appAssetLibraryImages",
            "--item-id", image["id"], "--if-exists", "skip",
        )
        print(f"  {label}: {attrs(image).get('fileName')} added to creative review submission {submission_id}")
    items = submission_items(asc, submission_id)
    unexpected = [item["id"] for item in items if related_id(item, "appAssetLibraryImage") not in wanted]
    if unexpected or len(items) != len(images):
        fail(f"review submission {submission_id} holds unexpected items {unexpected}; not submitting it")
    call(asc, "review", "submissions-submit", "--id", submission_id, "--confirm")
    print(f"  submitted {len(images)} creative image(s) for App Review ({submission_id})")
    return submission_id


PRODUCT_GROUPS = ("subscriptions", "consumables", "tips")


def intended_product_ids(catalog_path: Path | None = None) -> set[str]:
    """Product IDs the release is allowed to submit, from the canonical catalog."""
    catalog = json.loads(
        (catalog_path or ROOT / "store" / "catalog" / "products.json").read_text(encoding="utf-8")
    )
    ids: set[str] = set()
    for group in PRODUCT_GROUPS:
        for item in catalog.get(group) or []:
            pid = item.get("id")
            if isinstance(pid, str) and pid:
                ids.add(pid)
    return ids


class ProductSubmitError(Exception):
    """A required product could not be added to App Review."""


class ProductSubmitResult:
    """Outcome of attaching products to a review submission."""

    def __init__(
        self,
        submission_id: str,
        submitted: int,
        added_items: bool,
        has_products: bool = False,
        allowed_versions: set[str] | None = None,
    ) -> None:
        self.submission_id = submission_id
        self.submitted = submitted
        self.added_items = added_items
        # True when the draft holds intended products (attached now or in an
        # earlier run), so a catch-up retry can still submit the draft.
        self.has_products = has_products
        # Every version of every catalog product: the only product items a
        # draft may hold when it is submitted.
        self.allowed_versions = allowed_versions or set()


# States that mean the product can still be (re)attached to a review.
SUBMITTABLE_PRODUCT_STATES = {
    "READY_TO_SUBMIT",
    "DEVELOPER_REJECTED",
    "READY_FOR_REVIEW",
}
# In-app purchase version states that can still join a review submission
# (READY_FOR_REVIEW: already in a draft, e.g. this run's on a rerun).
SUBMITTABLE_IAP_VERSION_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "READY_FOR_REVIEW",
    "DEVELOPER_REJECTED",
    "REJECTED",
}
# Review submission item relationships, requested explicitly: App Store Connect
# leaves them out of the items unless they are included.
ITEM_RELATIONSHIPS = ("appStoreVersion", "subscriptionVersion", "inAppPurchaseVersion", "appAssetLibraryImage")


def draft_submission_ids(asc: asc_cli.Asc, app_id: str) -> list[str]:
    """Every READY_FOR_REVIEW (draft) review submission of the app."""
    payload = call(
        asc, "review", "submissions-list", "--app", app_id, "--platform", PLATFORM, "--state", "READY_FOR_REVIEW",
        "--paginate",
    )
    return [row["id"] for row in rows(payload)]


def submission_items(asc: asc_cli.Asc, submission_id: str) -> list[dict[str, Any]]:
    """The submission's items with their related ids."""
    return rows(
        call(
            asc, "review", "items-list", "--submission", submission_id, "--include", ",".join(ITEM_RELATIONSHIPS),
            "--paginate",
        )
    )


def find_draft_submission(asc: asc_cli.Asc, app_id: str) -> str | None:
    """The app's draft review submission for the version and products, if there is one.

    A draft holding creative images belongs to their own review and is skipped."""
    for submission_id in draft_submission_ids(asc, app_id):
        if not any(related_id(item, "appAssetLibraryImage") for item in submission_items(asc, submission_id)):
            return submission_id
    return None


def get_or_create_draft_submission(asc: asc_cli.Asc, app_id: str) -> str:
    """Return the app's draft review submission for the version and products, creating one."""
    existing = find_draft_submission(asc, app_id)
    if existing is not None:
        return existing
    created = data(call(asc, "review", "submissions-create", "--app", app_id, "--platform", PLATFORM)) or {}
    if not created.get("id"):
        fail("asc did not report the new review submission's id")
    return created["id"]


def draft_item_version_ids(asc: asc_cli.Asc, submission_id: str) -> set[str]:
    """Return the subscription/in-app purchase/app version ids already in a draft."""
    ids: set[str] = set()
    for item in submission_items(asc, submission_id):
        for name in ("subscriptionVersion", "appStoreVersion", "inAppPurchaseVersion"):
            found = related_id(item, name)
            if found:
                ids.add(found)
    return ids


def draft_holds_app_version(asc: asc_cli.Asc, submission_id: str) -> bool:
    """Return True if any item in the draft references an app store version."""
    return any(related_id(item, "appStoreVersion") for item in submission_items(asc, submission_id))


def check_draft_items(
    asc: asc_cli.Asc, submission_id: str, *, version_id: str | None, allowed_products: set[str]
) -> None:
    """Refuse to submit a draft holding anything but this version and catalog products.

    A reused draft can hold items added by hand or by another tool; they must
    not ride along into App Review.
    """
    unexpected = []
    for item in submission_items(asc, submission_id):
        app_version = related_id(item, "appStoreVersion")
        product = related_id(item, "subscriptionVersion") or related_id(item, "inAppPurchaseVersion")
        if app_version and app_version == version_id:
            continue
        if product and not app_version and product in allowed_products:
            continue
        unexpected.append(item.get("id"))
    if unexpected:
        fail(
            f"review submission {submission_id} holds items this release did not choose "
            f"({', '.join(map(str, unexpected))}); remove them in App Store Connect and rerun"
        )


def submit_product_draft(asc: asc_cli.Asc, submission_id: str) -> None:
    """Submit a draft review submission.

    Used on the catch-up path when the app version is already under review and a
    late Ready-to-Submit product was attached to a new draft: the draft is
    submitted on its own so the product actually reaches review. Callers must
    ensure the draft holds only products intended for this release.
    """
    call(asc, "review", "submissions-submit", "--id", submission_id, "--confirm")
    print(f"  submitted product review submission {submission_id}")


def subscription_versions(asc: asc_cli.Asc, subscription_id: str) -> list[dict[str, Any]]:
    return rows(call(asc, "subscriptions", "versions", "list", "--subscription-id", subscription_id, "--paginate"))


def current_subscription_version(versions: list[dict[str, Any]]) -> str | None:
    """Return the subscription's current in-flight version id, if any.

    App Store Connect allows only one in-flight version per subscription, so the
    highest-numbered version is the one to attach.
    """
    candidates = [v for v in versions if attrs(v).get("version")]
    if not candidates:
        return versions[0]["id"] if versions else None
    return max(candidates, key=lambda v: v["attributes"]["version"])["id"]


def in_app_purchase_versions(asc: asc_cli.Asc, iap_id: str) -> list[dict[str, Any]]:
    return rows(call(asc, "iap", "versions", "list", "--iap-id", iap_id, "--paginate"))


def current_in_app_purchase_version(versions: list[dict[str, Any]]) -> str | None:
    """Return the in-app purchase's draft version to submit, if any.

    App Store Connect keeps at most one version of an in-app purchase in flight;
    approved and replaced versions are never submitted again."""
    drafts = [v for v in versions if attrs(v).get("state") in SUBMITTABLE_IAP_VERSION_STATES]
    if not drafts:
        return None
    return max(drafts, key=lambda v: attrs(v).get("version") or 0)["id"]


def submit_in_app_purchases(
    asc: asc_cli.Asc,
    app_id: str,
    *,
    intended_ids: set[str] | None = None,
    submission_id: str | None = None,
) -> ProductSubmitResult:
    """Attach Ready-to-Submit subscriptions / IAPs to the draft review submission.

    Both join the draft as Review Submission Items through their current
    version: subscriptions as `subscriptionVersions`, in-app purchases such as
    the PCM Notes tip jar as `inAppPurchaseVersions`. Nothing reaches App Review
    until the draft is submitted, so the caller rechecks the build first and
    submits once.

    Only products listed in `store/catalog/products.json` are considered, so an
    unrelated Ready-to-Submit product is not dragged into this release. Approved
    products are never resubmitted. Without a `submission_id`, an existing draft
    is reused and a new one is created only when a product has to be attached.
    The versions of every catalog product (in any state) are returned as
    `allowed_versions`, the only product items the draft may hold when submitted.
    """
    if intended_ids is None:
        intended_ids = intended_product_ids()

    if not intended_ids:
        print("  catalog has no products to submit")
        return ProductSubmitResult(submission_id or "", 0, False)

    if submission_id is None:
        submission_id = find_draft_submission(asc, app_id)

    # Idempotency: a retry reuses the same draft, so skip anything already
    # attached instead of adding it again.
    already_attached = draft_item_version_ids(asc, submission_id) if submission_id else set()

    submitted = 0
    added_items = False
    has_products = False
    failures: list[str] = []
    allowed: set[str] = set()

    def attach(item_type: str, version_id: str) -> None:
        nonlocal submission_id
        if submission_id is None:
            submission_id = get_or_create_draft_submission(asc, app_id)
        asc.run(
            "review", "items-add", "--submission", submission_id, "--item-type", item_type,
            "--item-id", version_id, "--if-exists", "skip",
        )

    candidates: list[tuple[str, str, str, str, str | None]] = []
    for sub in rows(call(asc, "subscriptions", "list", "--app", app_id, "--paginate")):
        a = attrs(sub)
        pid = a.get("productId") or ""
        if pid not in intended_ids:
            continue
        versions = subscription_versions(asc, sub["id"])
        allowed |= {v["id"] for v in versions}
        if a.get("state") not in SUBMITTABLE_PRODUCT_STATES:
            continue
        candidates.append(
            ("subscription", "subscriptionVersions", a.get("name") or sub["id"], pid, current_subscription_version(versions))
        )
    for iap in rows(call(asc, "iap", "list", "--app", app_id, "--paginate")):
        a = attrs(iap)
        pid = a.get("productId") or ""
        if pid not in intended_ids:
            continue
        versions = in_app_purchase_versions(asc, iap["id"])
        allowed |= {v["id"] for v in versions}
        if a.get("state") != "READY_TO_SUBMIT":
            continue
        candidates.append(
            ("in-app purchase", "inAppPurchaseVersions", a.get("name") or iap["id"], pid,
             current_in_app_purchase_version(versions))
        )

    for kind, item_type, name, pid, version_id in candidates:
        if not version_id:
            failures.append(f"{kind} {name} ({pid}): no version to submit")
            continue
        if version_id in already_attached:
            submitted += 1
            has_products = True
            print(f"  {kind} {name}: already attached (idempotent)")
            continue
        try:
            attach(item_type, version_id)
        except asc_cli.AscError as exc:
            failures.append(f"{kind} {name} ({pid}): {exc}")
            continue
        submitted += 1
        added_items = True
        has_products = True
        print(f"  {kind} {name}: attached to review submission")

    print(f"  in-app purchases / subscriptions in the review submission: {submitted}")
    if failures:
        raise ProductSubmitError(
            "these required products were not added to App Review: "
            + "; ".join(failures)
            + ". Resolve them in App Store Connect and rerun the release."
        )
    return ProductSubmitResult(submission_id or "", submitted, added_items, has_products, allowed)


def submit_for_review(
    asc: asc_cli.Asc,
    app_id: str,
    version_id: str,
    version_state: str = "",
    *,
    submission_id: str | None = None,
    allowed_products: Iterable[str] = (),
) -> None:
    # Reuse a draft submission when one already exists (an earlier attempt can
    # leave a READY_FOR_REVIEW submission that was never submitted).
    if submission_id is None:
        submission_id = get_or_create_draft_submission(asc, app_id)

    items_versions = draft_item_version_ids(asc, submission_id)
    # READY_FOR_REVIEW means the version is already held by this draft submission.
    already_added = version_state == "READY_FOR_REVIEW" or version_id in items_versions
    if not already_added:
        call(
            asc, "review", "items-add", "--submission", submission_id, "--item-type", "appStoreVersions",
            "--item-id", version_id, "--if-exists", "skip",
        )
    # Only this version and versions of catalog products go to App Review.
    check_draft_items(asc, submission_id, version_id=version_id, allowed_products=set(allowed_products))
    call(asc, "review", "submissions-submit", "--id", submission_id, "--confirm")
    print(
        f"  submitted App Store version {version_id} for review "
        f"(submission {submission_id})"
    )


RELEASE_TYPE_PUBLIC = "AFTER_APPROVAL"
RELEASE_TYPE_MANUAL = "MANUAL"


def verify_build(
    asc: asc_cli.Asc, build_id: str, version_string: str, build_number: str
) -> None:
    """Re-read the build and refuse anything but this run's processed build."""
    payload = call(
        asc, "builds", "list", "--app", ASC_APP_ID, "--build-number", build_number, "--version", version_string,
        "--platform", PLATFORM, "--processing-state", "all", "--include", "preReleaseVersion",
    )
    included = {(row.get("type"), row.get("id")): row for row in (payload or {}).get("included") or []}
    build = next((row for row in rows(payload) if row.get("id") == build_id), None)
    problems = []
    if build is None:
        problems.append("not listed for this app, version and build number")
    else:
        a = attrs(build)
        pre = attrs(included.get(("preReleaseVersions", related_id(build, "preReleaseVersion"))))
        if a.get("version") != build_number:
            problems.append(f"build number {a.get('version')!r}")
        if a.get("processingState") != "VALID":
            problems.append(f"processing state {a.get('processingState')!r}")
        if a.get("expired"):
            problems.append("expired")
        if pre.get("version") != version_string or pre.get("platform") != PLATFORM:
            problems.append(f"version {pre.get('platform')} {pre.get('version')!r}")
    if problems:
        fail(
            f"build {build_id} is not {version_string} ({build_number}), VALID and unexpired: "
            + ", ".join(problems)
        )


def attached_build_id(asc: asc_cli.Asc, version_id: str) -> str | None:
    payload = call(asc, "versions", "view", "--version-id", version_id, "--include", "build")
    return related_id(data(payload) or {}, "build")


def confirm_selected_build(asc: asc_cli.Asc, version_id: str, build_id: str) -> None:
    current = attached_build_id(asc, version_id)
    if current != build_id:
        fail(
            f"App Store version {version_id} has build {current or 'none'} selected, "
            f"expected {build_id}; not submitting"
        )


def select_build(
    asc: asc_cli.Asc,
    version_id: str,
    build_id: str,
    version_string: str,
    build_number: str,
) -> None:
    """Explicitly select this run's build for the version, then read it back.

    A rerun that finds this build already selected changes nothing."""
    verify_build(asc, build_id, version_string, build_number)
    if attached_build_id(asc, version_id) == build_id:
        print(f"  build {build_number} ({build_id}) is already selected for version {version_string}")
        return
    call(asc, "versions", "attach-build", "--version-id", version_id, "--build-id", build_id)
    confirm_selected_build(asc, version_id, build_id)
    print(f"  selected build {build_number} ({build_id}) for version {version_string}")


def set_release_type(
    asc: asc_cli.Asc, version_id: str, *, public_release: bool, current: str | None = None
) -> None:
    release_type = RELEASE_TYPE_PUBLIC if public_release else RELEASE_TYPE_MANUAL
    if current == release_type:
        print(f"  release type is already {release_type}")
        return
    call(asc, "versions", "update", "--version-id", version_id, "--release-type", release_type)
    print(
        "  release type: "
        + ("release automatically after approval" if public_release else "manual release after approval")
    )


def validate_local_inputs(
    *,
    upload_listing: bool,
    upload_screenshots: bool,
    metadata_dir: Path,
    upload_whats_new: bool = False,
    upload_creative: bool = False,
    creative_dir: Path | None = None,
    root: Path = ROOT,
) -> list[str]:
    """Check the repository's listing before any App Store Connect call. Returns the locales."""
    locales = listing_locales(metadata_dir)
    if (upload_listing or upload_whats_new) and not locales:
        fail(f"no App Store locales in {metadata_dir / 'ios'}")
    if (upload_listing or upload_whats_new) and PRIMARY_LOCALE not in locales:
        fail(f"{metadata_dir / 'ios' / PRIMARY_LOCALE} is missing (the primary App Store locale)")
    for locale in locales if (upload_listing or upload_whats_new) else ():
        if not LOCALE_PATTERN.fullmatch(locale):
            fail(f"{metadata_dir / 'ios' / locale} is not an App Store locale directory")
        loc_dir = metadata_dir / "ios" / locale
        fields: tuple[tuple[str, str, int | None], ...] = ()
        if upload_listing:
            for filename in REQUIRED_LISTING_FILES:
                if not read_text(loc_dir / filename):
                    fail(f"listing upload is on, but {loc_dir / filename} is missing or empty")
            fields += APP_INFO_FIELDS + LISTING_FIELDS
        if upload_whats_new:
            if not read_text(loc_dir / WHATS_NEW_FIELD[1]):
                fail(f"What's New upload is on, but {loc_dir / WHATS_NEW_FIELD[1]} is missing or empty")
            fields += (WHATS_NEW_FIELD,)
        for field, filename, limit in fields:
            value = read_text(loc_dir / filename) or ""
            if limit is not None and len(value) > limit:
                fail(f"{loc_dir / filename} is {len(value)} characters; App Store Connect allows {limit}")
            if field.endswith("Url") and value and not value.startswith("https://"):
                fail(f"{loc_dir / filename} must be an https:// URL")
    if upload_screenshots:
        validate_screenshots(root)
    if upload_creative:
        creative_sources(creative_dir or root / "store" / "creative" / "ios")
    return locales


def version_key(version_string: str) -> tuple[int, ...] | None:
    """1.0.1 -> (1, 0, 1), padded to three parts; None when it is not numeric."""
    if not re.fullmatch(r"[0-9]+(\.[0-9]+){0,2}", version_string or ""):
        return None
    parts = [int(part) for part in version_string.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def ensure_ios_app_store_version(
    asc: asc_cli.Asc, *, bundle_id: str, expected_app_id: str, version_string: str
) -> None:
    """Create the App Store version for this release when it does not exist yet.

    An existing version with this number is left as it is (the preflight then
    checks its state). Nothing is created when another version is still on its
    way to the store, or when this number is not newer than every existing
    version; the created version is read back before the run continues.
    """
    wanted = version_key(version_string)
    if wanted is None:
        fail(f"version {version_string!r} must look like 1.1 or 1.0.1")
    app_id = find_app(asc, bundle_id, expected_app_id)
    if app_id != expected_app_id:
        fail(f"{bundle_id} resolved to App Store Connect app {app_id}, expected {expected_app_id}")
    versions = ios_app_store_versions(asc, app_id)
    existing = [row for row in versions if attrs(row).get("versionString") == version_string]
    if len(existing) > 1:
        fail(f"ambiguous App Store versions for {version_string!r} on IOS — got {len(existing)} records")
    if existing:
        state = attrs(existing[0]).get("appStoreState") or ""
        print(f"App Store version {version_string} already exists ({state}); nothing to create")
        return
    problems: list[str] = []
    for row in versions:
        other = attrs(row).get("versionString") or ""
        state = attrs(row).get("appStoreState") or ""
        if state not in FINISHED_VERSION_STATES:
            problems.append(
                f"version {other!r} is still {state or 'in an unknown state'}; release or remove it "
                f"in App Store Connect before {version_string} can be created"
            )
        other_key = version_key(other)
        if other_key is not None and other_key >= wanted:
            problems.append(f"version {version_string} is not newer than the existing version {other}")
    if problems:
        fail(
            f"not creating App Store version {version_string} (nothing was changed):\n  "
            + "\n  ".join(problems)
        )
    call(asc, "versions", "create", "--app", app_id, "--version", version_string, "--platform", PLATFORM)
    created = [
        row for row in ios_app_store_versions(asc, app_id)
        if attrs(row).get("versionString") == version_string
    ]
    if len(created) != 1:
        fail(
            f"created App Store version {version_string}, but reading it back found "
            f"{len(created)} records; check it in App Store Connect before rerunning"
        )
    state = attrs(created[0]).get("appStoreState") or ""
    print(f"created App Store version {version_string} ({state or 'state not reported yet'})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        default=ROOT / "store" / "metadata",
        help="Listing tree: ios/<locale>/*.txt",
    )
    parser.add_argument(
        "--creative-dir",
        type=Path,
        default=ROOT / "store" / "creative" / "ios",
        help="Product page header and search results images: " + ", ".join(s[0] for s in CREATIVE_SLOTS),
    )
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument(
        "--version",
        help="Marketing version from the release tag (e.g. 1.0.1 for tag v1.0.1)",
    )
    parser.add_argument(
        "--build-id",
        help="App Store Connect id of the build uploaded by this run (required to submit)",
    )
    parser.add_argument(
        "--build-number",
        help="CFBundleVersion of the build uploaded by this run (required to submit)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate flags and local files only — no asc calls",
    )
    parser.add_argument(
        "--create-version",
        action="store_true",
        help="Create the App Store version for --version when it is missing (CREATE_IOS_VERSION)",
    )
    args = parser.parse_args()

    # find_app() checks the pinned app's bundle id, so only this app is accepted.
    if args.bundle_id != DEFAULT_BUNDLE_ID:
        fail(f"this publisher only supports the PCM Notes iOS bundle ({DEFAULT_BUNDLE_ID})")

    if args.create_version:
        if not truthy("CREATE_IOS_VERSION"):
            print("App Store version creation skipped — CREATE_IOS_VERSION is off")
            return
        if not args.version:
            fail("--create-version needs --version (the marketing version from the tag)")
        if version_key(args.version) is None:
            fail(f"version {args.version!r} must look like 1.1 or 1.0.1")
        if args.dry_run:
            print(f"ASC dry-run — would create App Store version {args.version} if it is missing")
            return
        ensure_ios_app_store_version(
            asc_from_environment(),
            bundle_id=args.bundle_id,
            expected_app_id=ASC_APP_ID,
            version_string=args.version,
        )
        return

    do_listing = truthy("UPLOAD_LISTING")
    do_whats_new = truthy("UPLOAD_WHATS_NEW")
    do_screenshots = truthy("UPLOAD_SCREENSHOTS")
    do_creative = truthy("UPLOAD_CREATIVE_ASSETS")
    do_submit = truthy("SUBMIT_IOS_REVIEW")
    do_public = truthy("PUBLIC_RELEASE")

    if not (do_listing or do_whats_new or do_screenshots or do_creative or do_submit):
        print(
            "ASC release skipped — UPLOAD_LISTING / UPLOAD_WHATS_NEW / UPLOAD_SCREENSHOTS / "
            "UPLOAD_CREATIVE_ASSETS / SUBMIT_IOS_REVIEW are off"
        )
        return
    if do_public and not do_submit:
        fail("PUBLIC_RELEASE only applies when SUBMIT_IOS_REVIEW is on")
    if do_submit and not (args.build_id and args.build_number):
        fail(
            "submitting needs --build-id and --build-number for the build uploaded by "
            "this run; an older build is never substituted"
        )

    locales = validate_local_inputs(
        upload_listing=do_listing,
        upload_screenshots=do_screenshots,
        metadata_dir=args.metadata_dir,
        upload_whats_new=do_whats_new,
        upload_creative=do_creative,
        creative_dir=args.creative_dir,
    )

    if args.dry_run:
        print("ASC dry-run — local inputs OK; no asc calls")
        print(
            f"  bundle={args.bundle_id} locales={','.join(locales) or '-'} listing={do_listing} "
            f"whats_new={do_whats_new} screenshots={do_screenshots} creative={do_creative} "
            f"submit={do_submit} public_release={do_public}"
        )
        return

    asc = asc_from_environment()

    if not args.version:
        fail("--version is required for live ASC calls (pass marketing version from tag)")

    app_id = find_app(asc, args.bundle_id)
    version = get_ios_app_store_version(asc, app_id, args.version)
    version_id = version["id"]
    version_attrs = attrs(version)
    state = version_attrs.get("appStoreState") or ""
    print(
        f"ASC app={app_id} version={args.version} id={version_id} state={state}"
    )
    do_text = do_listing or do_whats_new
    # EDIT uploads what differs; VERIFY (a rerun that finds the version already in
    # review, or screenshots already in a draft) only compares, read-only. The
    # header and search results images follow the screenshots.
    text_access = metadata_access(state) if do_text else None
    shot_access = metadata_access(state, screenshots=True) if do_screenshots else None
    creative_access = metadata_access(state, screenshots=True) if do_creative else None
    blocked = [
        what for what, on, access in (
            ("listing/What's New", do_text, text_access),
            ("screenshots", do_screenshots, shot_access),
            ("creative assets", do_creative, creative_access),
        )
        if on and access is None
    ]
    if blocked:
        fail(
            f"version {args.version!r} is not editable for {' and '.join(blocked)} "
            f"(state={state}); expected one of: {', '.join(sorted(EDITABLE_VERSION_STATES))}"
        )
    text_frozen = state if text_access == VERIFY else None
    shots_frozen = state if shot_access == VERIFY else None

    def sync_metadata() -> None:
        if do_text:
            parts = [name for name, on in (("listing", do_listing), ("What's New", do_whats_new)) if on]
            verb = "checking" if text_frozen else "uploading"
            for locale in locales:
                print(f"{verb} ASC {' + '.join(parts)} ({locale})…")
                upload_listing(
                    asc,
                    app_id=app_id,
                    version_id=version_id,
                    locale=locale,
                    metadata_dir=args.metadata_dir,
                    include_listing=do_listing,
                    include_whats_new=do_whats_new,
                    frozen_state=text_frozen,
                )
        if do_screenshots:
            verb = "checking" if shots_frozen else "uploading"
            print(f"{verb} ASC screenshots ({PRIMARY_LOCALE})…")
            upload_screenshots(asc, app_id=app_id, version_id=version_id, frozen_state=shots_frozen)
        if do_creative:
            verb = "checking" if creative_access == VERIFY else "syncing"
            print(f"{verb} ASC product page header and search results images ({PRIMARY_LOCALE})…")
            sync_creative_assets(
                asc, app_id=app_id, version_id=version_id, locale=PRIMARY_LOCALE, creative_dir=args.creative_dir,
                editable=creative_access == EDIT,
            )

    if state == "WAITING_FOR_REVIEW":
        # A rerun after a successful submit ("Re-run failed jobs"): nothing on the
        # version can change any more. Accept it only when review holds exactly
        # this run's build and App Store Connect already has the repository's
        # listing, What's New, screenshots and creative assets; then catch up on
        # late products.
        if do_submit:
            confirm_selected_build(asc, version_id, args.build_id)
        sync_metadata()
        if do_submit:
            print(
                "ASC submit skipped — version already WAITING_FOR_REVIEW (idempotent)"
            )
            try:
                result = submit_in_app_purchases(asc, app_id)
            except ProductSubmitError as exc:
                fail(str(exc))
            # Attaching a product to a fresh draft does not submit it. If the
            # version is already under review, the draft holding the late
            # products must be submitted on its own or they never reach review.
            # `has_products` also covers a retry where an earlier run attached
            # the products but failed to submit their draft. Only submit a
            # product-only draft: refuse if it holds an app version (e.g.
            # another release's) or anything else this release did not choose.
            if result.has_products and result.submission_id:
                if draft_holds_app_version(asc, result.submission_id):
                    fail(
                        "refusing to submit a draft that contains an app "
                        "version; remove it in App Store Connect and rerun"
                    )
                check_draft_items(
                    asc, result.submission_id, version_id=None, allowed_products=result.allowed_versions
                )
                submit_product_draft(asc, result.submission_id)
        print("ASC release step finished")
        return

    if do_submit and state not in SUBMITTABLE_VERSION_STATES:
        fail(
            f"version {args.version!r} cannot be submitted for review (state={state})"
        )

    sync_metadata()

    if do_submit:
        print("selecting this run's build…")
        select_build(asc, version_id, args.build_id, args.version, args.build_number)
        set_release_type(
            asc, version_id, public_release=do_public, current=version_attrs.get("releaseType")
        )
        # Recheck before the review submission is touched: the selection must
        # still be this run's processed build. Products and the version are only
        # attached to the draft after this, and nothing reaches App Review until
        # the single submit below.
        verify_build(asc, args.build_id, args.version, args.build_number)
        confirm_selected_build(asc, version_id, args.build_id)
        print("adding in-app purchases / subscriptions to the review submission…")
        submission_id = get_or_create_draft_submission(asc, app_id)
        try:
            result = submit_in_app_purchases(
                asc, app_id, submission_id=submission_id
            )
        except ProductSubmitError as exc:
            # A required product must ship with the version, so a failed one
            # blocks the whole submission rather than letting the version enter
            # review without it.
            fail(f"{exc} Not submitting the app version.")
        print("submitting for App Store review…")
        submit_for_review(
            asc, app_id, version_id, state, submission_id=submission_id,
            allowed_products=result.allowed_versions,
        )
        print(f"  {result.submitted} product(s) went out with the version")

    print("ASC release step finished")


if __name__ == "__main__":
    main()
