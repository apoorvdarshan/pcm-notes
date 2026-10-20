#!/usr/bin/env python3
"""App Store Connect listing, What's New, screenshots and submit-for-review (gated in CI).

The listing lives in the repository: store/metadata/ios/<locale>/*.txt holds every
App Store locale (exported from the live listing), and the screenshots are in
store/metadata/screenshots/ios/<display size>/. Each operation only runs when its switch
is on (see store/README.md); with every switch off this script does nothing.

Submitting for review covers the App Store version and any Ready-to-Submit
in-app purchases listed in store/catalog/products.json. They all join one draft
review submission as Review Submission Items (subscriptions through the
`subscriptionVersion` relationship, in-app purchases such as the tip jar through
`inAppPurchaseVersion`) only after this run's build is rechecked, and reach App
Review together with a single submit. Apple deprecated the standalone
`inAppPurchaseSubmissions` endpoint, which sent a product to review on its own.

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
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
ASC_BASE = "https://api.appstoreconnect.apple.com/v1"
ASC_V2_BASE = "https://api.appstoreconnect.apple.com/v2"
DEFAULT_BUNDLE_ID = "com.apoorvdarshan.iitjee"
# PCM Notes: JEE & NEET. Every call is pinned to this App Store Connect app.
APP_STORE_APP_ID = "6792598227"
PRIMARY_LOCALE = "en-US"
# App Store Connect per screenshot-set limit.
APP_SCREENSHOT_SET_MAX = 10
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
    but a rerun may confirm App Store Connect already matches, else None."""
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


class AscHttpError(SystemExit):
    """An App Store Connect HTTP error; exits like fail() unless a caller retries."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"error: {message}")
        self.status = status


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


class AscClient:
    # JWTs are minted for 20 minutes; refresh well before that during long waits.
    TOKEN_MAX_AGE_SECONDS = 600

    def __init__(
        self, token: str | None = None, *, token_factory: Any | None = None
    ) -> None:
        if token is None and token_factory is None:
            raise ValueError("AscClient needs a token or a token_factory")
        self._token_factory = token_factory
        self._token = token if token is not None else token_factory()
        self._token_minted = time.monotonic()

    def _authorization(self) -> str:
        if (
            self._token_factory is not None
            and time.monotonic() - self._token_minted > self.TOKEN_MAX_AGE_SECONDS
        ):
            self._token = self._token_factory()
            self._token_minted = time.monotonic()
        return f"Bearer {self._token}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
        accept: str = "application/json",
    ) -> dict[str, Any] | None:
        url = path if path.startswith("http") else f"{ASC_BASE}{path}"
        headers = {
            "Authorization": self._authorization(),
            "Accept": accept,
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type
        elif raw_body is not None:
            data = raw_body
            headers["Content-Type"] = content_type

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
                if not payload:
                    return None
                if accept != "application/json":
                    return {"raw": payload}
                return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            print(f"error: ASC {method} {path} → HTTP {exc.code}: {detail[:2000]}", file=sys.stderr)
            raise AscHttpError(exc.code, f"ASC {method} {path} → HTTP {exc.code}") from None

    def get(self, path: str) -> dict[str, Any]:
        result = self.request("GET", path)
        assert result is not None
        return result

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self.request("PATCH", path, body=body)
        assert result is not None
        return result

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", path, body=body)
        assert result is not None
        return result

    def upload_bytes(
        self, url: str, data: bytes, headers: dict[str, str], *, method: str = "PUT"
    ) -> None:
        req = urllib.request.Request(
            url, data=data, method=method.upper(), headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=300):
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            fail(f"ASC asset upload → HTTP {exc.code}: {detail[:1000]}")


def make_asc_token(key_id: str, issuer_id: str, private_key: str) -> str:
    import jwt

    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 1200,
        "aud": "appstoreconnect-v1",
    }
    headers = {"kid": key_id, "typ": "JWT"}
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


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


def client_from_environment() -> AscClient:
    """Client whose JWT is re-minted automatically during long-running steps."""
    key_id, issuer_id, key_p8 = load_credentials()
    return AscClient(token_factory=lambda: make_asc_token(key_id, issuer_id, key_p8))


def find_app(client: AscClient, bundle_id: str) -> str:
    """Return the App Store Connect id of exactly this app, or stop."""
    query = urllib.parse.urlencode({"filter[bundleId]": bundle_id})
    payload = client.get(f"/apps?{query}")
    matches = [
        row for row in payload.get("data") or []
        if (row.get("attributes") or {}).get("bundleId") == bundle_id
    ]
    if not matches:
        fail(f"no App Store Connect app for bundle id {bundle_id}")
    if len(matches) > 1:
        fail(f"ambiguous: {len(matches)} App Store Connect apps use bundle id {bundle_id}")
    app_id = matches[0]["id"]
    if bundle_id == DEFAULT_BUNDLE_ID and app_id != APP_STORE_APP_ID:
        fail(f"bundle id {bundle_id} belongs to app {app_id}, expected PCM Notes ({APP_STORE_APP_ID})")
    return app_id


def ios_app_store_versions(client: AscClient, app_id: str) -> list[dict[str, Any]]:
    # Prefer the app relationship listing — some ASC API keys cannot
    # GET_COLLECTION on /appStoreVersions with filter[app]=… (403).
    return iter_collection(
        client,
        f"/apps/{app_id}/appStoreVersions?"
        + urllib.parse.urlencode({"limit": "50", "filter[platform]": "IOS"}),
    )


def find_ios_app_store_version(
    client: AscClient, app_id: str, version_string: str
) -> dict[str, Any] | None:
    matches = [
        row for row in ios_app_store_versions(client, app_id)
        if (row.get("attributes") or {}).get("versionString") == version_string
    ]
    if len(matches) > 1:
        fail(
            f"ambiguous App Store versions for {version_string!r} on IOS — "
            f"got {len(matches)} records"
        )
    return matches[0] if matches else None


def get_ios_app_store_version(
    client: AscClient, app_id: str, version_string: str
) -> dict[str, Any]:
    version = find_ios_app_store_version(client, app_id, version_string)
    if version is None:
        fail(
            f"no IOS App Store version {version_string!r} for app {app_id} "
            "(turn on IOS_CREATE_VERSION, or create the version in App Store Connect first)"
        )
    return version


def live_ios_version(client: AscClient, app_id: str) -> dict[str, Any] | None:
    """The version currently on the App Store, if any."""
    for row in ios_app_store_versions(client, app_id):
        if (row.get("attributes") or {}).get("appStoreState") in LIVE_VERSION_STATES:
            return row
    return None


def find_localization(
    client: AscClient, collection_path: str, locale: str
) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"filter[locale]": locale, "limit": "1"})
    payload = client.get(f"{collection_path}?{query}")
    data = [
        row for row in payload.get("data") or []
        if (row.get("attributes") or {}).get("locale", locale) == locale
    ]
    return data[0] if data else None


def localization_for_locale(
    client: AscClient, collection_path: str, locale: str
) -> dict[str, Any]:
    localization = find_localization(client, collection_path, locale)
    if localization is None:
        fail(
            f"no localization {locale} at {collection_path} "
            "(add that language in App Store Connect first)"
        )
    return localization


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
# Files App Store Connect requires for every listed locale.
REQUIRED_LISTING_FILES = ("name.txt", "description.txt", "keywords.txt", "support_url.txt", "privacy_url.txt")
LOCALE_PATTERN = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,4})*")


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
    attrs = info.get("attributes") or {}
    return attrs.get("state") or attrs.get("appStoreState") or ""


def upload_app_info(
    client: AscClient,
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
    infos = iter_collection(client, f"/apps/{app_id}/appInfos")
    pending = [info for info in infos if app_info_state(info) not in SETTLED_APP_INFO_STATES]
    if len(pending) > 1:
        fail(f"ambiguous: {len(pending)} app information records for a new version")
    live = [info for info in infos if app_info_state(info) in LIVE_VERSION_STATES]
    target = (pending or live or infos or [None])[0]
    if target is None:
        fail("app has no appInfos")
    loc = find_localization(client, f"/appInfos/{target['id']}/appInfoLocalizations", locale)
    current = (loc or {}).get("attributes") or {}
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
    client.patch(
        f"/appInfoLocalizations/{loc['id']}",
        {"data": {"type": "appInfoLocalizations", "id": loc["id"], "attributes": changed}},
    )
    print(f"  updated app information ({locale}): {', '.join(changed)}")


def check_whats_new_is_new(
    client: AscClient, *, app_id: str, version_id: str, locale: str, text: str
) -> None:
    """Refuse to publish the live version's What's New again (a forgotten update)."""
    live = live_ios_version(client, app_id)
    if live is None or live.get("id") == version_id:
        return
    loc = find_localization(
        client, f"/appStoreVersions/{live['id']}/appStoreVersionLocalizations", locale
    )
    previous = ((loc or {}).get("attributes") or {}).get("whatsNew") or ""
    if previous.strip() and previous.strip() == text.strip():
        live_version = (live.get("attributes") or {}).get("versionString")
        fail(
            f"store/metadata/ios/{locale}/whats_new.txt still holds the What's New of live "
            f"version {live_version}; write this release's notes before uploading"
        )


def upload_listing(
    client: AscClient,
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
            client, app_id=app_id, locale=locale, metadata_dir=metadata_dir,
            frozen_state=frozen_state,
        )

    version_loc = localization_for_locale(
        client, f"/appStoreVersions/{version_id}/appStoreVersionLocalizations", locale
    )
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
            client, app_id=app_id, version_id=version_id, locale=locale,
            text=wanted["whatsNew"],
        )
    current = version_loc.get("attributes") or {}
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
    client.patch(
        f"/appStoreVersionLocalizations/{version_loc['id']}",
        {
            "data": {
                "type": "appStoreVersionLocalizations",
                "id": version_loc["id"],
                "attributes": changed,
            }
        },
    )
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


def screenshot_sets_for_localization(
    client: AscClient, version_loc_id: str
) -> list[dict[str, Any]]:
    payload = client.get(
        f"/appStoreVersionLocalizations/{version_loc_id}/appScreenshotSets"
    )
    return payload.get("data") or []


def find_screenshot_set(
    client: AscClient, version_loc_id: str, display_type: str
) -> dict[str, Any] | None:
    for item in screenshot_sets_for_localization(client, version_loc_id):
        if (item.get("attributes") or {}).get("screenshotDisplayType") == display_type:
            return item
    return None


def ensure_screenshot_set(
    client: AscClient, version_loc_id: str, display_type: str
) -> dict[str, Any]:
    existing = find_screenshot_set(client, version_loc_id, display_type)
    if existing is not None:
        return existing
    created = client.post(
        "/appScreenshotSets",
        {
            "data": {
                "type": "appScreenshotSets",
                "attributes": {"screenshotDisplayType": display_type},
                "relationships": {
                    "appStoreVersionLocalization": {
                        "data": {
                            "type": "appStoreVersionLocalizations",
                            "id": version_loc_id,
                        }
                    }
                },
            }
        },
    )
    return created["data"]


def list_screenshots(client: AscClient, screenshot_set_id: str) -> list[dict[str, Any]]:
    payload = client.get(f"/appScreenshotSets/{screenshot_set_id}/appScreenshots")
    return payload.get("data") or []


def list_screenshot_ids(client: AscClient, screenshot_set_id: str) -> list[str]:
    return [shot["id"] for shot in list_screenshots(client, screenshot_set_id)]


def delete_screenshots(client: AscClient, screenshot_ids: list[str]) -> None:
    for shot_id in screenshot_ids:
        client.request("DELETE", f"/appScreenshots/{shot_id}")


def upload_screenshot_file(
    client: AscClient, screenshot_set_id: str, image_path: Path
) -> None:
    raw = image_path.read_bytes()
    reserved = client.post(
        "/appScreenshots",
        {
            "data": {
                "type": "appScreenshots",
                "attributes": {
                    "fileName": image_path.name,
                    "fileSize": len(raw),
                },
                "relationships": {
                    "appScreenshotSet": {
                        "data": {"type": "appScreenshotSets", "id": screenshot_set_id}
                    }
                },
            }
        },
    )
    shot = reserved["data"]
    shot_id = shot["id"]
    upload_ops = (shot.get("attributes") or {}).get("uploadOperations") or []
    if not upload_ops:
        fail(f"ASC returned no uploadOperations for {image_path.name}")
    for op in upload_ops:
        offset = int(op.get("offset", 0))
        length = int(op["length"])
        chunk = raw[offset : offset + length]
        headers = {h["name"]: h["value"] for h in op.get("requestHeaders") or []}
        method = (op.get("method") or "PUT").upper()
        client.upload_bytes(op["url"], chunk, headers, method=method)
    client.patch(
        f"/appScreenshots/{shot_id}",
        {
            "data": {
                "type": "appScreenshots",
                "id": shot_id,
                "attributes": {
                    "uploaded": True,
                    "sourceFileChecksum": hashlib.md5(raw).hexdigest(),  # noqa: S324 - ASC uses MD5
                },
            }
        },
    )
    print(f"  uploaded screenshot {image_path.name}")


def wait_for_screenshot_processing(
    client: AscClient,
    screenshot_set_id: str,
    *,
    timeout_seconds: int = 600,
    poll_seconds: int = 10,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """App Store Connect processes screenshots after upload; review needs them COMPLETE."""
    deadline = now() + timeout_seconds
    while True:
        states = [
            ((shot.get("attributes") or {}).get("assetDeliveryState") or {}).get("state")
            for shot in list_screenshots(client, screenshot_set_id)
        ]
        if "FAILED" in states:
            fail(f"App Store Connect could not process a screenshot in set {screenshot_set_id}")
        if states and all(state == "COMPLETE" for state in states):
            return
        if now() > deadline:
            fail(f"screenshots in set {screenshot_set_id} were still processing after {timeout_seconds}s")
        sleep(poll_seconds)


def upload_screenshot_set(
    client: AscClient,
    version_loc_id: str,
    spec: ScreenshotSet,
    root: Path = ROOT,
    *,
    frozen_state: str | None = None,
) -> bool:
    """Replace one display type's screenshots. Returns False when already current.

    With `frozen_state` (screenshots can no longer change) nothing is written: a
    set that already matches passes, anything else stops."""
    pngs = screenshot_files(spec, root)
    if not pngs:
        fail(f"no PNG screenshots in {root / spec.directory}")
    if len(pngs) > APP_SCREENSHOT_SET_MAX:
        fail(f"too many PNGs ({len(pngs)}) for {spec.display_type} (max {APP_SCREENSHOT_SET_MAX})")

    if frozen_state:
        shot_set = find_screenshot_set(client, version_loc_id, spec.display_type)
    else:
        shot_set = ensure_screenshot_set(client, version_loc_id, spec.display_type)
    existing = list_screenshots(client, shot_set["id"]) if shot_set else []
    wanted = [hashlib.md5(path.read_bytes()).hexdigest() for path in pngs]  # noqa: S324
    live = [(shot.get("attributes") or {}).get("sourceFileChecksum") for shot in existing]
    if live == wanted:
        print(f"  {spec.display_type}: the {len(pngs)} screenshots already match")
        return False
    if frozen_state:
        fail(
            f"{spec.display_type} screenshots differ from App Store Connect, but the version "
            f"is already {frozen_state}, so its screenshots can no longer change"
        )
    set_id = shot_set["id"]

    # Old ASC ids still on the set; pop front when deleting for capacity or final cleanup.
    pending_old_ids = [shot["id"] for shot in existing]

    # Upload replacements one at a time. ASC rejects new reservations when the set is
    # at capacity, so delete one pending old screenshot before each upload when full.
    # If a mid-run upload fails, the live set may be mixed (some olds removed, some news
    # uploaded); fix the error and re-run, or restore screenshots in App Store Connect.
    for path in pngs:
        current_ids = list_screenshot_ids(client, set_id)
        if len(current_ids) >= APP_SCREENSHOT_SET_MAX:
            if not pending_old_ids:
                fail(
                    f"ASC screenshot set at capacity ({APP_SCREENSHOT_SET_MAX}) "
                    f"with no tracked old screenshots left to replace"
                )
            delete_screenshots(client, [pending_old_ids.pop(0)])
        upload_screenshot_file(client, set_id, path)

    if pending_old_ids:
        delete_screenshots(client, pending_old_ids)
        print(f"  removed {len(pending_old_ids)} previous {spec.display_type} screenshot(s)")
    wait_for_screenshot_processing(client, set_id)
    print(f"  {spec.display_type}: {len(pngs)} screenshots processed")
    return True


def upload_screenshots(
    client: AscClient,
    *,
    version_id: str,
    locale: str = PRIMARY_LOCALE,
    root: Path = ROOT,
    frozen_state: str | None = None,
) -> None:
    # Other languages fall back to the primary locale's screenshots.
    version_loc = localization_for_locale(
        client, f"/appStoreVersions/{version_id}/appStoreVersionLocalizations", locale
    )
    for spec in SCREENSHOT_SETS:
        upload_screenshot_set(client, version_loc["id"], spec, root, frozen_state=frozen_state)


def iter_collection(client: AscClient, path: str) -> list[dict[str, Any]]:
    """Return every record in an ASC collection, following links.next."""
    rows: list[dict[str, Any]] = []
    while path:
        payload = client.get(path)
        rows.extend(payload.get("data") or [])
        next_url = (payload.get("links") or {}).get("next")
        if not next_url:
            break
        path = next_url
    return rows


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
    ) -> None:
        self.submission_id = submission_id
        self.submitted = submitted
        self.added_items = added_items
        # True when the draft holds intended products (attached now or in an
        # earlier run), so a catch-up retry can still submit the draft.
        self.has_products = has_products


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


def find_draft_submission(client: AscClient, app_id: str) -> str | None:
    """The app's READY_FOR_REVIEW draft review submission, if there is one."""
    existing = client.get(
        f"/apps/{app_id}/reviewSubmissions?"
        + urllib.parse.urlencode({"filter[state]": "READY_FOR_REVIEW", "limit": "5"})
    )
    for row in existing.get("data") or []:
        return row["id"]
    return None


def get_or_create_draft_submission(client: AscClient, app_id: str) -> str:
    """Return the app's READY_FOR_REVIEW draft review submission, creating one."""
    existing = find_draft_submission(client, app_id)
    if existing is not None:
        return existing
    submission = client.post(
        "/reviewSubmissions",
        {
            "data": {
                "type": "reviewSubmissions",
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
    )
    return submission["data"]["id"]


def draft_item_relationships(client: AscClient, submission_id: str) -> list[dict[str, Any]]:
    """Return the draft's items with their version relationships populated.

    App Store Connect omits `relationships` from Review Submission Items unless
    they are requested with `include`, so both relationship readers must ask for
    the version relationships explicitly.
    """
    return iter_collection(
        client,
        f"/reviewSubmissions/{submission_id}/items"
        "?limit=50&include=subscriptionVersion,appStoreVersion,inAppPurchaseVersion",
    )


def draft_item_version_ids(client: AscClient, submission_id: str) -> set[str]:
    """Return the subscription/in-app purchase/app version ids already in a draft."""
    ids: set[str] = set()
    for item in draft_item_relationships(client, submission_id):
        relationships = item.get("relationships") or {}
        for name in ("subscriptionVersion", "appStoreVersion", "inAppPurchaseVersion"):
            related = (relationships.get(name) or {}).get("data") or {}
            if related.get("id"):
                ids.add(related["id"])
    return ids


def draft_holds_app_version(client: AscClient, submission_id: str) -> bool:
    """Return True if any item in the draft references an app store version."""
    for item in draft_item_relationships(client, submission_id):
        related = (
            ((item.get("relationships") or {}).get("appStoreVersion") or {}).get("data")
            or {}
        )
        if related.get("id"):
            return True
    return False


def submit_product_draft(client: AscClient, submission_id: str) -> None:
    """Submit a draft review submission.

    Used on the catch-up path when the app version is already under review and a
    late Ready-to-Submit product was attached to a new draft: the draft is
    submitted on its own so the product actually reaches review. Callers must
    ensure the draft holds only products intended for this release.
    """
    client.request(
        "PATCH",
        f"/reviewSubmissions/{submission_id}",
        body={
            "data": {
                "type": "reviewSubmissions",
                "id": submission_id,
                "attributes": {"submitted": True},
            }
        },
    )
    print(f"  submitted product review submission {submission_id}")


def current_subscription_version(
    client: AscClient, subscription_id: str
) -> str | None:
    """Return the subscription's current in-flight version id, if any.

    App Store Connect allows only one in-flight version per subscription, so the
    highest-numbered version in a submittable state is the one to attach.
    """
    versions = iter_collection(client, f"/subscriptions/{subscription_id}/versions")
    candidates = [v for v in versions if (v.get("attributes") or {}).get("version")]
    if not candidates:
        return versions[0]["id"] if versions else None
    return max(candidates, key=lambda v: v["attributes"]["version"])["id"]


def current_in_app_purchase_version(client: AscClient, iap_id: str) -> str | None:
    """Return the in-app purchase's draft version to submit, if any.

    App Store Connect keeps at most one version of an in-app purchase in flight;
    approved and replaced versions are never submitted again."""
    versions = iter_collection(client, f"{ASC_V2_BASE}/inAppPurchases/{iap_id}/versions?limit=200")
    drafts = [
        v for v in versions
        if (v.get("attributes") or {}).get("state") in SUBMITTABLE_IAP_VERSION_STATES
    ]
    if not drafts:
        return None
    return max(drafts, key=lambda v: (v.get("attributes") or {}).get("version") or 0)["id"]


def submit_in_app_purchases(
    client: AscClient,
    app_id: str,
    *,
    intended_ids: set[str] | None = None,
    submission_id: str | None = None,
) -> ProductSubmitResult:
    """Attach Ready-to-Submit subscriptions / IAPs to the draft review submission.

    Both join the draft as Review Submission Items: subscriptions through the
    `subscriptionVersion` relationship (not `subscription`, which the API
    rejects), in-app purchases such as the PCM Notes tip jar through
    `inAppPurchaseVersion`. Nothing reaches App Review until the draft is
    submitted, so the caller rechecks the build first and submits once.

    Only products listed in `store/catalog/products.json` are considered, so an
    unrelated Ready-to-Submit product is not dragged into this release. Approved
    products are never resubmitted. Without a `submission_id`, an existing draft
    is reused and a new one is created only when a product has to be attached.
    """
    if intended_ids is None:
        intended_ids = intended_product_ids()

    if not intended_ids:
        print("  catalog has no products to submit")
        return ProductSubmitResult(submission_id or "", 0, False)

    if submission_id is None:
        submission_id = find_draft_submission(client, app_id)

    # Idempotency: a retry reuses the same draft, so skip anything already
    # attached instead of posting a duplicate that App Store Connect rejects.
    already_attached = draft_item_version_ids(client, submission_id) if submission_id else set()

    submitted = 0
    added_items = False
    has_products = False
    failures: list[str] = []

    def attach(relationship: str, resource_type: str, version_id: str) -> None:
        nonlocal submission_id
        if submission_id is None:
            submission_id = get_or_create_draft_submission(client, app_id)
        client.post(
            "/reviewSubmissionItems",
            {
                "data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {
                            "data": {"type": "reviewSubmissions", "id": submission_id}
                        },
                        relationship: {"data": {"type": resource_type, "id": version_id}},
                    },
                }
            },
        )

    for group in iter_collection(
        client, f"/apps/{app_id}/subscriptionGroups?limit=200"
    ):
        for sub in iter_collection(
            client, f"/subscriptionGroups/{group['id']}/subscriptions?limit=200"
        ):
            attrs = sub.get("attributes") or {}
            pid = attrs.get("productId") or ""
            if pid not in intended_ids:
                continue
            if attrs.get("state") not in SUBMITTABLE_PRODUCT_STATES:
                continue
            name = attrs.get("name") or sub["id"]
            version_id = current_subscription_version(client, sub["id"])
            if not version_id:
                failures.append(f"subscription {name} ({pid}): no version to submit")
                continue
            if version_id in already_attached:
                submitted += 1
                has_products = True
                print(f"  subscription {name}: already attached (idempotent)")
                continue
            try:
                attach("subscriptionVersion", "subscriptionVersions", version_id)
            except SystemExit:
                failures.append(f"subscription {name} ({pid})")
                continue
            submitted += 1
            added_items = True
            has_products = True
            print(f"  subscription {name}: attached to review submission")

    for iap in iter_collection(client, f"/apps/{app_id}/inAppPurchasesV2?limit=200"):
        attrs = iap.get("attributes") or {}
        pid = attrs.get("productId") or ""
        if pid not in intended_ids:
            continue
        if attrs.get("state") != "READY_TO_SUBMIT":
            continue
        name = attrs.get("name") or iap["id"]
        version_id = current_in_app_purchase_version(client, iap["id"])
        if not version_id:
            failures.append(f"in-app purchase {name} ({pid}): no version to submit")
            continue
        if version_id in already_attached:
            submitted += 1
            has_products = True
            print(f"  in-app purchase {name}: already attached (idempotent)")
            continue
        try:
            attach("inAppPurchaseVersion", "inAppPurchaseVersions", version_id)
        except SystemExit:
            failures.append(f"in-app purchase {name} ({pid})")
            continue
        submitted += 1
        added_items = True
        has_products = True
        print(f"  in-app purchase {name}: attached to review submission")

    print(f"  in-app purchases / subscriptions in the review submission: {submitted}")
    if failures:
        raise ProductSubmitError(
            "these required products were not added to App Review: "
            + "; ".join(failures)
            + ". Resolve them in App Store Connect and rerun the release."
        )
    return ProductSubmitResult(submission_id or "", submitted, added_items, has_products)


def submit_for_review(
    client: AscClient,
    app_id: str,
    version_id: str,
    version_state: str = "",
    *,
    submission_id: str | None = None,
) -> None:
    # Reuse a draft submission when one already exists (an earlier attempt can
    # leave a READY_FOR_REVIEW submission that was never submitted).
    if submission_id is None:
        submission_id = get_or_create_draft_submission(client, app_id)

    items_versions = draft_item_version_ids(client, submission_id)
    # READY_FOR_REVIEW means the version is already held by this draft submission.
    already_added = version_state == "READY_FOR_REVIEW" or version_id in items_versions
    if not already_added:
        client.post(
            "/reviewSubmissionItems",
            {
                "data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {
                            "data": {"type": "reviewSubmissions", "id": submission_id}
                        },
                        "appStoreVersion": {
                            "data": {"type": "appStoreVersions", "id": version_id}
                        },
                    },
                }
            },
        )

    # Submitting is a PATCH with submitted=true; there is no /submit action.
    client.request(
        "PATCH",
        f"/reviewSubmissions/{submission_id}",
        body={
            "data": {
                "type": "reviewSubmissions",
                "id": submission_id,
                "attributes": {"submitted": True},
            }
        },
    )
    print(
        f"  submitted App Store version {version_id} for review "
        f"(submission {submission_id})"
    )


RELEASE_TYPE_PUBLIC = "AFTER_APPROVAL"
RELEASE_TYPE_MANUAL = "MANUAL"


def verify_build(
    client: AscClient, build_id: str, version_string: str, build_number: str
) -> None:
    """Re-read the build and refuse anything but this run's processed build."""
    payload = client.get(
        f"/builds/{build_id}?"
        + urllib.parse.urlencode(
            {
                "include": "preReleaseVersion",
                "fields[builds]": "version,processingState,expired,preReleaseVersion",
                "fields[preReleaseVersions]": "version,platform",
            }
        )
    )
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    pre = next(
        (row for row in payload.get("included") or [] if row.get("type") == "preReleaseVersions"),
        {},
    )
    pre_attrs = pre.get("attributes") or {}
    problems = []
    if data.get("id") != build_id:
        problems.append(f"id {data.get('id')!r}")
    if attrs.get("version") != build_number:
        problems.append(f"build number {attrs.get('version')!r}")
    if attrs.get("processingState") != "VALID":
        problems.append(f"processing state {attrs.get('processingState')!r}")
    if attrs.get("expired"):
        problems.append("expired")
    if pre_attrs.get("version") != version_string or pre_attrs.get("platform") != "IOS":
        problems.append(f"version {pre_attrs.get('platform')} {pre_attrs.get('version')!r}")
    if problems:
        fail(
            f"build {build_id} is not {version_string} ({build_number}), VALID and unexpired: "
            + ", ".join(problems)
        )


def attached_build_id(client: AscClient, version_id: str) -> str | None:
    payload = client.get(f"/appStoreVersions/{version_id}/build")
    return (payload.get("data") or {}).get("id")


def confirm_selected_build(client: AscClient, version_id: str, build_id: str) -> None:
    current = attached_build_id(client, version_id)
    if current != build_id:
        fail(
            f"App Store version {version_id} has build {current or 'none'} selected, "
            f"expected {build_id}; not submitting"
        )


def select_build(
    client: AscClient,
    version_id: str,
    build_id: str,
    version_string: str,
    build_number: str,
) -> None:
    """Explicitly select this run's build for the version, then read it back.

    A rerun that finds this build already selected changes nothing."""
    verify_build(client, build_id, version_string, build_number)
    if attached_build_id(client, version_id) == build_id:
        print(f"  build {build_number} ({build_id}) is already selected for version {version_string}")
        return
    client.request(
        "PATCH",
        f"/appStoreVersions/{version_id}/relationships/build",
        body={"data": {"type": "builds", "id": build_id}},
    )
    confirm_selected_build(client, version_id, build_id)
    print(f"  selected build {build_number} ({build_id}) for version {version_string}")


def set_release_type(
    client: AscClient, version_id: str, *, public_release: bool, current: str | None = None
) -> None:
    release_type = RELEASE_TYPE_PUBLIC if public_release else RELEASE_TYPE_MANUAL
    if current == release_type:
        print(f"  release type is already {release_type}")
        return
    client.patch(
        f"/appStoreVersions/{version_id}",
        {
            "data": {
                "type": "appStoreVersions",
                "id": version_id,
                "attributes": {"releaseType": release_type},
            }
        },
    )
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
    root: Path = ROOT,
) -> list[str]:
    """Check the repository's listing before any API call. Returns the locales."""
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
    return locales


def version_key(version_string: str) -> tuple[int, ...] | None:
    """1.0.1 -> (1, 0, 1), padded to three parts; None when it is not numeric."""
    if not re.fullmatch(r"[0-9]+(\.[0-9]+){0,2}", version_string or ""):
        return None
    parts = [int(part) for part in version_string.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def ensure_ios_app_store_version(
    client: AscClient, *, bundle_id: str, expected_app_id: str, version_string: str
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
    app_id = find_app(client, bundle_id)
    if app_id != expected_app_id:
        fail(f"{bundle_id} resolved to App Store Connect app {app_id}, expected {expected_app_id}")
    versions = ios_app_store_versions(client, app_id)
    existing = [
        row for row in versions if (row.get("attributes") or {}).get("versionString") == version_string
    ]
    if len(existing) > 1:
        fail(f"ambiguous App Store versions for {version_string!r} on IOS — got {len(existing)} records")
    if existing:
        state = (existing[0].get("attributes") or {}).get("appStoreState") or ""
        print(f"App Store version {version_string} already exists ({state}); nothing to create")
        return
    problems: list[str] = []
    for row in versions:
        attrs = row.get("attributes") or {}
        other = attrs.get("versionString") or ""
        state = attrs.get("appStoreState") or ""
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
    client.post(
        "/appStoreVersions",
        {
            "data": {
                "type": "appStoreVersions",
                "attributes": {"platform": "IOS", "versionString": version_string},
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
    )
    created = [
        row for row in ios_app_store_versions(client, app_id)
        if (row.get("attributes") or {}).get("versionString") == version_string
    ]
    if len(created) != 1:
        fail(
            f"created App Store version {version_string}, but reading it back found "
            f"{len(created)} records; check it in App Store Connect before rerunning"
        )
    state = (created[0].get("attributes") or {}).get("appStoreState") or ""
    print(f"created App Store version {version_string} ({state or 'state not reported yet'})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        default=ROOT / "store" / "metadata",
        help="Listing tree: ios/<locale>/*.txt",
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
        help="Validate flags and local files only — no ASC HTTP calls",
    )
    parser.add_argument(
        "--create-version",
        action="store_true",
        help="Create the App Store version for --version when it is missing (CREATE_IOS_VERSION)",
    )
    args = parser.parse_args()

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
            client_from_environment(),
            bundle_id=args.bundle_id,
            expected_app_id=APP_STORE_APP_ID,
            version_string=args.version,
        )
        return

    do_listing = truthy("UPLOAD_LISTING")
    do_whats_new = truthy("UPLOAD_WHATS_NEW")
    do_screenshots = truthy("UPLOAD_SCREENSHOTS")
    do_submit = truthy("SUBMIT_IOS_REVIEW")
    do_public = truthy("PUBLIC_RELEASE")

    if not (do_listing or do_whats_new or do_screenshots or do_submit):
        print(
            "ASC release skipped — UPLOAD_LISTING / UPLOAD_WHATS_NEW / "
            "UPLOAD_SCREENSHOTS / SUBMIT_IOS_REVIEW are off"
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
    )

    if args.dry_run:
        print("ASC dry-run — local inputs OK; no API calls")
        print(
            f"  bundle={args.bundle_id} locales={','.join(locales) or '-'} listing={do_listing} "
            f"whats_new={do_whats_new} screenshots={do_screenshots} submit={do_submit} "
            f"public_release={do_public}"
        )
        return

    client = client_from_environment()

    if not args.version:
        fail("--version is required for live ASC calls (pass marketing version from tag)")

    app_id = find_app(client, args.bundle_id)
    version = get_ios_app_store_version(client, app_id, args.version)
    version_id = version["id"]
    version_attrs = version.get("attributes") or {}
    state = version_attrs.get("appStoreState") or ""
    print(
        f"ASC app={app_id} version={args.version} id={version_id} state={state}"
    )
    do_text = do_listing or do_whats_new
    # EDIT uploads what differs; VERIFY (a rerun that finds the version already in
    # review, or screenshots already in a draft) only compares, read-only.
    text_access = metadata_access(state) if do_text else None
    shot_access = metadata_access(state, screenshots=True) if do_screenshots else None
    blocked = [
        what for what, on, access in (
            ("listing/What's New", do_text, text_access),
            ("screenshots", do_screenshots, shot_access),
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
                    client,
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
            upload_screenshots(client, version_id=version_id, frozen_state=shots_frozen)

    if state == "WAITING_FOR_REVIEW":
        # A rerun after a successful submit ("Re-run failed jobs"): nothing on the
        # version can change any more. Accept it only when review holds exactly
        # this run's build and App Store Connect already has the repository's
        # listing, What's New and screenshots; then catch up on late products.
        if do_submit:
            confirm_selected_build(client, version_id, args.build_id)
        sync_metadata()
        if do_submit:
            print(
                "ASC submit skipped — version already WAITING_FOR_REVIEW (idempotent)"
            )
            try:
                result = submit_in_app_purchases(client, app_id)
            except ProductSubmitError as exc:
                fail(str(exc))
            # Attaching a product to a fresh draft does not submit it. If the
            # version is already under review, the draft holding the late
            # products must be submitted on its own or they never reach review.
            # `has_products` also covers a retry where an earlier run attached
            # the products but failed to submit their draft. Only submit a
            # product-only draft: refuse if it holds an app version (e.g.
            # another release's) so unrelated items never ship.
            if result.has_products and result.submission_id:
                if draft_holds_app_version(client, result.submission_id):
                    fail(
                        "refusing to submit a draft that contains an app "
                        "version; remove it in App Store Connect and rerun"
                    )
                submit_product_draft(client, result.submission_id)
        print("ASC release step finished")
        return

    if do_submit and state not in SUBMITTABLE_VERSION_STATES:
        fail(
            f"version {args.version!r} cannot be submitted for review (state={state})"
        )

    sync_metadata()

    if do_submit:
        print("selecting this run's build…")
        select_build(client, version_id, args.build_id, args.version, args.build_number)
        set_release_type(
            client, version_id, public_release=do_public, current=version_attrs.get("releaseType")
        )
        # Recheck before the review submission is touched: the selection must
        # still be this run's processed build. Products and the version are only
        # attached to the draft after this, and nothing reaches App Review until
        # the single submit below.
        verify_build(client, args.build_id, args.version, args.build_number)
        confirm_selected_build(client, version_id, args.build_id)
        print("adding in-app purchases / subscriptions to the review submission…")
        submission_id = get_or_create_draft_submission(client, app_id)
        try:
            result = submit_in_app_purchases(
                client, app_id, submission_id=submission_id
            )
        except ProductSubmitError as exc:
            # A required product must ship with the version, so a failed one
            # blocks the whole submission rather than letting the version enter
            # review without it.
            fail(f"{exc} Not submitting the app version.")
        print("submitting for App Store review…")
        submit_for_review(
            client, app_id, version_id, state, submission_id=submission_id
        )
        print(f"  {result.submitted} product(s) went out with the version")

    print("ASC release step finished")


if __name__ == "__main__":
    main()
