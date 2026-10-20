#!/usr/bin/env python3
"""App Store Connect listing, What's New, screenshots, creative assets and submit-for-review (gated in CI).

The listing lives in the repository: store/metadata/ios/<locale>/*.txt holds every
App Store locale (exported from the live listing), and the screenshots are in
store/metadata/screenshots/ios/<display size>/ (optionally iphone-duo/, and watch/
for the Apple Watch sets, picked by frame size). Each operation only runs when its
switch is on (see store/README.md); with every switch off this script does nothing.

Screenshots are uploaded as copies made in a temporary directory. App Store Connect
refuses a PNG that stores an alpha channel, even when every pixel is opaque, so a
PNG that does goes up as an RGB copy: transparent pixels laid over black, colour
profile kept (Pillow, requirements-store.txt). The committed files are only read,
never rewritten.

Product page header and search results images (UPLOAD_CREATIVE_ASSETS) live in
the app's Asset Library. A new image is uploaded once, sent to App Review in a
review submission of its own (never the version's draft), and placed on the
version's localization once Apple approves it.

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
import io
import json
import os
import re
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
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
# Product page creative assets: the folder under --creative-dir, the placement
# type, a label for logs, and the formats Apple takes. Each folder holds at most
# one image; a slot without an image is left alone.
HEADER_PLACEMENT = "PRODUCT_PAGE_HEADER_ASSET"
SEARCH_PLACEMENT = "APP_STORE_SEARCH_RESULTS_ASSET"
CREATIVE_SLOTS = (
    ("header", HEADER_PLACEMENT, "product page header", ("png",)),
    ("search-results", SEARCH_PLACEMENT, "search results", ("png", "jpeg")),
)
CREATIVE_EXTENSIONS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}
# Apple's image specs: the header is a 3840x1646 PNG; search results images are
# 3:2 PNG or JPEG from 1920x1280 to 3840x2560.
HEADER_SIZE = (3840, 1646)
SEARCH_RESULTS_WIDTHS = (1920, 3840)
CREATIVE_PLACEMENT_GROUP = "DEFAULT_PROFILE"
DEFAULT_CREATIVE_DIR = ROOT / "store" / "creative" / "ios"
# Asset Library image states (AppAssetLibraryAssetState). AWAITING_UPLOAD is the
# reservation an interrupted upload leaves behind: like a failed or archived
# image it is ignored, and the image is uploaded again.
CREATIVE_DEAD_STATES = {"AWAITING_UPLOAD", "FAILED", "ARCHIVED", "REJECTED"}
# Uploaded and still being processed; App Review takes it once it is ready.
CREATIVE_PROCESSING_STATES = {"UPLOAD_COMPLETE", "COMPLETE"}
# Processed, not yet submitted (READY_FOR_REVIEW: in a draft review submission).
CREATIVE_READY_STATES = {"PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW"}
CREATIVE_PENDING_STATES = {"WAITING_FOR_REVIEW", "IN_REVIEW", "ACCEPTED"}
# Processing a freshly uploaded image usually takes seconds.
CREATIVE_PROCESSING_TIMEOUT_SECONDS = 600
CREATIVE_POLL_SECONDS = 5
# A review submission item's relationships, requested explicitly (App Store
# Connect leaves them out otherwise).
DRAFT_ITEM_INCLUDE = "subscriptionVersion,appStoreVersion,inAppPurchaseVersion,appAssetLibraryImage"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# PNG chunks that describe colour, copied from a screenshot to its RGB copy
# (Pillow writes an ICC profile, iCCP, itself).
PNG_COLOUR_CHUNKS = (b"sRGB", b"gAMA", b"cHRM", b"cICP")
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
# Optional sets, uploaded with the ones above when their folder holds PNGs; without
# any, App Store Connect's set is left alone. APP_IPHONE_DUO is missing from Apple's
# OpenAPI spec, but App Store Connect accepts it; one set holds both the outer
# (1398x2034) and the inner (2007x2853) screen sizes.
OPTIONAL_SCREENSHOT_SETS = (
    ScreenshotSet(
        "APP_IPHONE_DUO",
        "store/metadata/screenshots/ios/iphone-duo",
        frozenset({(1398, 2034), (2007, 2853)}),
    ),
)
# Apple Watch screenshots, in this folder (relative to the repository root). The
# sets are optional: without PNGs here the live Watch sets are left alone. Each
# frame's size picks its display type (Apple's screenshot specifications), so one
# folder can fill more than one Watch set.
WATCH_FOLDER = "store/metadata/screenshots/ios/watch"
WATCH_DISPLAY_TYPES = {
    (416, 496): "APP_WATCH_SERIES_10",  # Series 10 and 11
    (422, 514): "APP_WATCH_ULTRA",  # Ultra 3
    (410, 502): "APP_WATCH_ULTRA",  # Ultra and Ultra 2
    (396, 484): "APP_WATCH_SERIES_7",  # Series 7 to 9
    (368, 448): "APP_WATCH_SERIES_4",  # Series 4 to 6 and SE
    (312, 390): "APP_WATCH_SERIES_3",
}
WATCH_LABELS = {
    "APP_WATCH_SERIES_10": "Apple Watch Series 10",
    "APP_WATCH_ULTRA": "Apple Watch Ultra",
    "APP_WATCH_SERIES_7": "Apple Watch Series 7",
    "APP_WATCH_SERIES_4": "Apple Watch Series 4",
    "APP_WATCH_SERIES_3": "Apple Watch Series 3",
}


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


def attrs(row: dict[str, Any] | None) -> dict[str, Any]:
    return (row or {}).get("attributes") or {}


def related_id(row: dict[str, Any], name: str) -> str | None:
    """The id a JSON:API row's `name` relationship points at, if it was included."""
    related = (((row.get("relationships") or {}).get(name) or {}).get("data")) or {}
    return related.get("id") if isinstance(related, dict) else None


def png_chunks(raw: bytes, *, strict: bool = True) -> list[tuple[bytes, bytes]]:
    """(type, data) of every chunk of a PNG, up to IEND.

    Raises ValueError when the bytes are not a PNG with an IHDR header. Strict
    (the default) also requires a well-formed file: every chunk complete with a
    matching CRC, image data that decompresses to the rows the header describes,
    and a final IEND, so a truncated or corrupt file fails before anything is
    uploaded. Lenient reading (the colour chunks copied to an RGB copy) stops at
    the first chunk that runs past the end of the file.
    """
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    chunks: list[tuple[bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(raw):
        length = int.from_bytes(raw[offset : offset + 4], "big")
        kind = raw[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + (4 if strict else 0) > len(raw):
            if strict or not chunks:
                raise ValueError(f"truncated {kind.decode('latin-1')!r} chunk")
            break
        data = raw[start:end]
        if strict and zlib.crc32(kind + data) != int.from_bytes(raw[end : end + 4], "big"):
            raise ValueError(f"the {kind.decode('latin-1')!r} chunk fails its CRC check")
        chunks.append((kind, data))
        offset = end + 4  # data, then the CRC
        if kind == b"IEND":
            break
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) < 13:
        raise ValueError("no IHDR header")
    if strict:
        if chunks[-1][0] != b"IEND":
            raise ValueError("no IEND chunk (the file is truncated)")
        check_png_image_data(chunks)
    return chunks


# Samples per pixel for each PNG colour type.
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def check_png_image_data(chunks: list[tuple[bytes, bytes]]) -> None:
    """Raises ValueError unless the IDAT data is one complete zlib stream of the header's size.

    Decompresses in bounded steps; the size is checked for non-interlaced images
    (Pillow writes those), an interlaced image only needs a complete stream.
    """
    header = chunks[0][1]
    width, height = png_header_size(chunks)
    bit_depth, colour_type, interlace = header[8], header[9], header[12]
    if colour_type not in PNG_CHANNELS:
        raise ValueError(f"unknown PNG colour type {colour_type}")
    idat = [data for kind, data in chunks if kind == b"IDAT"]
    if not idat:
        raise ValueError("no image data (IDAT)")
    inflater = zlib.decompressobj()
    size = 0
    try:
        for data in idat:
            pending = data
            while pending:
                size += len(inflater.decompress(pending, 1 << 20))
                pending = inflater.unconsumed_tail
        size += len(inflater.flush())
    except zlib.error as exc:
        raise ValueError(f"the image data does not decompress ({exc})") from None
    if not inflater.eof:
        raise ValueError("the image data ends early (the file is truncated)")
    if interlace == 0:
        expected = height * (1 + (width * PNG_CHANNELS[colour_type] * bit_depth + 7) // 8)
        if size != expected:
            raise ValueError(
                f"the image data holds {size} bytes, not the {expected} a {width}x{height} image needs"
            )


def png_header_size(chunks: list[tuple[bytes, bytes]]) -> tuple[int, int]:
    """(width, height) from the IHDR chunk png_chunks() returned first."""
    header = chunks[0][1]
    return int.from_bytes(header[0:4], "big"), int.from_bytes(header[4:8], "big")


def png_stores_alpha(chunks: list[tuple[bytes, bytes]]) -> bool:
    """True when a PNG stores alpha: grey or RGB with alpha (colour type 4 or 6), or a tRNS chunk.

    App Store Connect refuses such files even when every pixel is opaque.
    """
    colour_type = chunks[0][1][9]
    return colour_type in (4, 6) or any(kind == b"tRNS" for kind, _ in chunks)


def jpeg_size(raw: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's start-of-frame segment, or None when there is none."""
    if raw[:3] != b"\xff\xd8\xff":
        return None
    offset = 2
    while offset + 9 < len(raw):
        if raw[offset] != 0xFF:
            offset += 1
            continue
        marker = raw[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        # SOF0-SOF15, except DHT (C4), JPG (C8) and DAC (CC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(raw[offset + 5 : offset + 7], "big")
            width = int.from_bytes(raw[offset + 7 : offset + 9], "big")
            return width, height
        offset += 2 + int.from_bytes(raw[offset + 2 : offset + 4], "big")
    return None


def staged_png(path: Path, label: str) -> tuple[int, int]:
    """The size of a screenshot's upload copy; fails unless it is a PNG without alpha.

    The last guard before upload: the copies (screenshot_upload_copies) are RGB,
    so a file that still stores alpha was not flattened.
    """
    try:
        chunks = png_chunks(path.read_bytes())
    except ValueError as exc:
        fail(f"the {label} screenshot {path.name} is not a readable PNG ({exc})")
    if png_stores_alpha(chunks):
        fail(
            f"the {label} screenshot {path.name} stores an alpha channel (PNG colour type 4 or 6, "
            "or a tRNS chunk), which App Store Connect refuses; its upload copy must be RGB "
            "(screenshot_upload_copies flattens PNGs that store alpha)"
        )
    return png_header_size(chunks)


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


def watch_screenshot_sets(root: Path = ROOT) -> list[tuple[ScreenshotSet, list[Path]]]:
    """The Apple Watch frames grouped by the display type their size belongs to, in file name order."""
    watch_dir = root / WATCH_FOLDER
    groups: dict[str, list[Path]] = {}
    for path in sorted(watch_dir.glob("*.png")) if watch_dir.is_dir() else []:
        size = png_size(path)
        display_type = WATCH_DISPLAY_TYPES.get(size)
        if display_type is None:
            accepted = ", ".join(f"{w}x{h}" for w, h in WATCH_DISPLAY_TYPES)
            fail(
                f"the Apple Watch screenshot {path.name} is {size[0]}x{size[1]}, which is not an "
                f"Apple Watch screenshot size ({accepted})"
            )
        groups.setdefault(display_type, []).append(path)
    return [
        (
            ScreenshotSet(
                display_type,
                WATCH_FOLDER,
                frozenset(size for size, kind in WATCH_DISPLAY_TYPES.items() if kind == display_type),
            ),
            pngs,
        )
        for display_type, pngs in groups.items()
    ]


def screenshot_sets(root: Path = ROOT) -> list[tuple[ScreenshotSet, list[Path]]]:
    """(set, PNGs) for every set to upload, in upload order, Apple Watch sets last.

    Fails when an iPhone or iPad set has no PNGs, when a set is over App Store
    Connect's limit, when a PNG is not one of its set's sizes (an Apple Watch
    frame: not an Apple Watch screenshot size), and when a file is not a
    complete, well-formed PNG. An optional set without PNGs (iPhone Duo, Apple
    Watch) is left out, so App Store Connect's set for it is left alone. The
    PNGs may store alpha: their upload copies are RGB (screenshot_upload_copies).
    """
    sets: list[tuple[ScreenshotSet, list[Path]]] = []
    for spec in SCREENSHOT_SETS:
        files = screenshot_files(spec, root)
        if not files:
            fail(
                f"screenshot upload is on, but {root / spec.directory} has no PNG files for "
                f"{spec.display_type}"
            )
        sets.append((spec, files))
    for spec in OPTIONAL_SCREENSHOT_SETS:
        files = screenshot_files(spec, root)
        if files:
            sets.append((spec, files))
    # watch_screenshot_sets checks each Watch frame's size while it groups them.
    sets.extend(watch_screenshot_sets(root))
    for spec, files in sets:
        if len(files) > APP_SCREENSHOT_SET_MAX:
            fail(
                f"{len(files)} PNGs for {spec.display_type} in {root / spec.directory}; "
                f"App Store Connect allows {APP_SCREENSHOT_SET_MAX}"
            )
        for path in files:
            size = png_size(path)
            if size not in spec.sizes:
                accepted = ", ".join(f"{w}x{h}" for w, h in sorted(spec.sizes))
                fail(f"{path} is {size[0]}x{size[1]}; {spec.display_type} accepts {accepted}")
            try:
                png_chunks(path.read_bytes())
            except ValueError as exc:
                fail(f"{path} is not a complete, well-formed PNG ({exc})")
    return sets


def validate_screenshots(root: Path = ROOT) -> None:
    screenshot_sets(root)


def try_import_pillow():
    try:
        from PIL import Image  # type: ignore[import-untyped]

        return Image
    except ImportError:
        return None


def opaque_rgb(Image, img):
    """The image as RGB, with transparent pixels laid over black."""
    rgba = img.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, (0, 0, 0))
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def colour_chunks(raw: bytes) -> list[tuple[bytes, bytes]]:
    """The PNG's colour chunks other than its ICC profile (Pillow keeps that one)."""
    try:
        chunks = png_chunks(raw, strict=False)
    except ValueError:
        return []
    return [(kind, data) for kind, data in chunks if kind in PNG_COLOUR_CHUNKS]


def prepare_asc_png(Image, path: Path) -> bytes:
    """An RGB PNG of the screenshot, without alpha, colour profile kept.

    Committed screenshots already have an accepted size (screenshot_sets checks
    it), so nothing is resized: the frame is flattened once.
    """
    from PIL import PngImagePlugin  # type: ignore[import-untyped]

    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as img:
        icc_profile = img.info.get("icc_profile")
        out_img = opaque_rgb(Image, img)
    info = PngImagePlugin.PngInfo()
    for kind, data in colour_chunks(raw):
        info.add(kind, data)
    out = io.BytesIO()
    options = {"icc_profile": icc_profile} if icc_profile else {}
    out_img.save(out, format="PNG", pnginfo=info, **options)
    return out.getvalue()


PILLOW_HINT = (
    "install Pillow (pip install --require-hashes -r requirements-store.txt) so the upload can "
    "flatten it into an RGB copy, or re-export it as RGB"
)


def screenshot_upload_copies(spec: ScreenshotSet, pngs: list[Path], staging: Path) -> list[Path]:
    """The files uploaded for these screenshots: copies in `staging`, in order, without alpha.

    A PNG that stores alpha (colour type 4 or 6, or a tRNS chunk) becomes an RGB
    copy (prepare_asc_png); any other PNG is copied as it is. The committed files
    are only read. Every copy then passes the last guard (staged_png).
    """
    Image = None
    copies = []
    for path in pngs:
        raw = path.read_bytes()
        try:
            alpha = png_stores_alpha(png_chunks(raw))
        except ValueError as exc:
            fail(f"{path} is not a complete, well-formed PNG ({exc})")
        if alpha:
            Image = Image or try_import_pillow()
            if Image is None:
                fail(
                    f"the {spec.display_type} screenshot {path.name} stores an alpha channel, which "
                    f"App Store Connect refuses; {PILLOW_HINT}"
                )
            try:
                raw = prepare_asc_png(Image, path)
            except (OSError, SyntaxError, ValueError) as exc:
                fail(f"cannot flatten {path} into an RGB copy ({exc})")
        copy = staging / path.name
        copy.write_bytes(raw)
        staged_png(copy, spec.display_type)
        copies.append(copy)
    return copies


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


def file_checksum(raw: bytes) -> str:
    """The MD5 App Store Connect records as a screenshot's sourceFileChecksum."""
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()


def send_upload_operations(
    client: AscClient, reserved: dict[str, Any], raw: bytes, name: str
) -> None:
    """Send a file's bytes the way the reservation's uploadOperations ask for."""
    upload_ops = attrs(reserved).get("uploadOperations") or []
    if not upload_ops:
        fail(f"ASC returned no uploadOperations for {name}")
    for op in upload_ops:
        offset = int(op.get("offset", 0))
        length = int(op["length"])
        chunk = raw[offset : offset + length]
        headers = {h["name"]: h["value"] for h in op.get("requestHeaders") or []}
        method = (op.get("method") or "PUT").upper()
        client.upload_bytes(op["url"], chunk, headers, method=method)


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
    send_upload_operations(client, shot, raw, image_path.name)
    # The checksum lets a later run see that App Store Connect already has this file.
    client.patch(
        f"/appScreenshots/{shot_id}",
        {
            "data": {
                "type": "appScreenshots",
                "id": shot_id,
                "attributes": {"uploaded": True, "sourceFileChecksum": file_checksum(raw)},
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


def plan_screenshot_set(
    client: AscClient,
    version_loc_id: str,
    spec: ScreenshotSet,
    pngs: list[Path],
    staging: Path,
    *,
    frozen_state: str | None = None,
) -> list[Path] | None:
    """Read-only: the upload copies for one set, or None when App Store Connect already holds it.

    App Store Connect holds the set when its screenshots' checksums, in order, are
    those of the committed files (what earlier releases uploaded, alpha and all)
    or of their upload copies, so neither is uploaded again. With `frozen_state`
    (screenshots can no longer change) a set that differs stops the run.
    """
    if not pngs:
        fail(f"no PNG screenshots for {spec.display_type} in {spec.directory}")
    if len(pngs) > APP_SCREENSHOT_SET_MAX:
        fail(f"too many PNGs ({len(pngs)}) for {spec.display_type} (max {APP_SCREENSHOT_SET_MAX})")
    shot_set = find_screenshot_set(client, version_loc_id, spec.display_type)
    existing = list_screenshots(client, shot_set["id"]) if shot_set else []
    live = [attrs(shot).get("sourceFileChecksum") for shot in existing]
    if live == [file_checksum(path.read_bytes()) for path in pngs]:
        print(f"  {spec.display_type}: the {len(pngs)} screenshots already match")
        return None
    copies = screenshot_upload_copies(spec, pngs, staging)
    if live == [file_checksum(copy.read_bytes()) for copy in copies]:
        print(f"  {spec.display_type}: the {len(pngs)} screenshots already match (as RGB copies)")
        return None
    if frozen_state:
        fail(
            f"{spec.display_type} screenshots differ from App Store Connect, but the version "
            f"is already {frozen_state}, so its screenshots can no longer change"
        )
    return copies


def replace_screenshot_set(
    client: AscClient, version_loc_id: str, spec: ScreenshotSet, copies: list[Path]
) -> None:
    """Replace one display type's screenshots with these files, in order, and wait for processing."""
    set_id = ensure_screenshot_set(client, version_loc_id, spec.display_type)["id"]

    # Old ASC ids still on the set; pop front when deleting for capacity or final cleanup.
    pending_old_ids = list_screenshot_ids(client, set_id)

    # Upload replacements one at a time. ASC rejects new reservations when the set is
    # at capacity, so delete one pending old screenshot before each upload when full.
    # If a mid-run upload fails, the live set may be mixed (some olds removed, some news
    # uploaded); fix the error and re-run, or restore screenshots in App Store Connect.
    for path in copies:
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
    print(f"  {spec.display_type}: {len(copies)} screenshots processed")


def upload_screenshot_set(
    client: AscClient,
    version_loc_id: str,
    spec: ScreenshotSet,
    root: Path = ROOT,
    *,
    frozen_state: str | None = None,
    pngs: list[Path] | None = None,
) -> bool:
    """Replace one display type's screenshots. Returns False when already current.

    The files go up as copies made in a temporary directory (RGB where the
    committed PNG stores alpha). With `frozen_state` (screenshots can no longer
    change) nothing is written: a set that already matches passes, anything else
    stops."""
    pngs = screenshot_files(spec, root) if pngs is None else pngs
    with tempfile.TemporaryDirectory(prefix="asc-screenshots-") as staging:
        copies = plan_screenshot_set(
            client, version_loc_id, spec, pngs, Path(staging), frozen_state=frozen_state
        )
        if copies is None:
            return False
        replace_screenshot_set(client, version_loc_id, spec, copies)
    return True


def upload_screenshots(
    client: AscClient,
    *,
    version_id: str,
    locale: str = PRIMARY_LOCALE,
    root: Path = ROOT,
    frozen_state: str | None = None,
) -> None:
    """Replace every set App Store Connect does not hold yet (see plan_screenshot_set).

    Every set is compared, and its upload copies made and checked, before any
    screenshot is written. Optional sets without PNGs are left alone.
    """
    sets = screenshot_sets(root)
    present = {spec.display_type for spec, _ in sets}
    for spec in OPTIONAL_SCREENSHOT_SETS:
        if spec.display_type not in present:
            print(
                f"  no {spec.display_type} screenshots in {spec.directory}; "
                "that set in App Store Connect is left alone"
            )
    if not present & set(WATCH_LABELS):
        print(
            f"  no Apple Watch screenshots in {WATCH_FOLDER}; "
            "the Watch sets in App Store Connect are left alone"
        )
    # Other languages fall back to the primary locale's screenshots.
    version_loc = localization_for_locale(
        client, f"/appStoreVersions/{version_id}/appStoreVersionLocalizations", locale
    )
    with tempfile.TemporaryDirectory(prefix="asc-screenshots-") as staging:
        replacements = []
        for spec, pngs in sets:
            set_staging = Path(staging) / spec.display_type
            set_staging.mkdir()
            copies = plan_screenshot_set(
                client, version_loc["id"], spec, pngs, set_staging, frozen_state=frozen_state
            )
            if copies is not None:
                replacements.append((spec, copies))
        for spec, copies in replacements:
            replace_screenshot_set(client, version_loc["id"], spec, copies)


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


def creative_problem(placement_type: str, path: Path, formats: tuple[str, ...]) -> str | None:
    """Why App Store Connect would refuse this image for the slot, or None when it fits."""
    raw = path.read_bytes()
    kind = "png" if raw.startswith(PNG_SIGNATURE) else "jpeg" if raw[:3] == b"\xff\xd8\xff" else None
    if kind is None:
        return "is not a PNG or JPEG file"
    if CREATIVE_EXTENSIONS[path.suffix.lower()] != kind:
        return f"holds {kind.upper()} data but is named {path.suffix}"
    if kind not in formats:
        return f"is a {kind.upper()}; this slot takes {' or '.join(f.upper() for f in formats)}"
    if kind == "png":
        try:
            chunks = png_chunks(raw)
        except ValueError as exc:
            return f"is not a readable PNG ({exc})"
        if png_stores_alpha(chunks):
            return (
                "stores an alpha channel (PNG colour type 4 or 6, or a tRNS chunk), which App Store "
                "Connect refuses; re-export it as RGB"
            )
        width, height = png_header_size(chunks)
    else:
        size = jpeg_size(raw)
        if size is None:
            return "is not a readable JPEG"
        width, height = size
    if placement_type == HEADER_PLACEMENT:
        if (width, height) != HEADER_SIZE:
            return f"is {width}x{height}; the header must be exactly {HEADER_SIZE[0]}x{HEADER_SIZE[1]}"
        return None
    low, high = SEARCH_RESULTS_WIDTHS
    if not (low <= width <= high and width * 2 == height * 3):
        return (
            f"is {width}x{height}; search results images are 3:2, from {low}x{low * 2 // 3} "
            f"to {high}x{high * 2 // 3}"
        )
    return None


def creative_sources(creative_dir: Path) -> list[tuple[str, str, Path]]:
    """(placement type, label, image) for every creative slot that has an image.

    Checks each image's format, size and alpha first, so a file App Store
    Connect would refuse stops the run before any call. Fails when no slot has
    an image.
    """
    found = []
    for folder, placement_type, label, formats in CREATIVE_SLOTS:
        slot_dir = creative_dir / folder
        images = sorted(
            p
            for p in (slot_dir.iterdir() if slot_dir.is_dir() else [])
            if p.is_file() and p.suffix.lower() in CREATIVE_EXTENSIONS
        )
        if len(images) > 1:
            fail(f"the {label} slot takes one image; {slot_dir} has {len(images)}")
        if not images:
            print(f"  no {label} image in {slot_dir}; that slot is left alone")
            continue
        problem = creative_problem(placement_type, images[0], formats)
        if problem:
            fail(f"the {label} image {images[0].name} {problem}")
        found.append((placement_type, label, images[0]))
    if not found:
        fail(f"creative asset upload is on, but {creative_dir} has no header or search results image")
    return found


def asset_library_id(client: AscClient, app_id: str) -> str:
    library_id = (client.get(f"/apps/{app_id}/assetLibrary").get("data") or {}).get("id")
    if not library_id:
        fail(f"App Store Connect returned no Asset Library for app {app_id}")
    return library_id


def library_images(client: AscClient, library_id: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"filter[category]": "CREATIVE_ASSETS", "limit": "200"})
    return iter_collection(client, f"/appAssetLibraries/{library_id}/images?{query}")


def library_image_for(images: list[dict[str, Any]], path: Path) -> dict[str, Any] | None:
    """The newest usable library image uploaded from this file (same name and size).

    Reservations an interrupted upload left (AWAITING_UPLOAD), failed and
    archived images do not count, so the file is uploaded again. A file whose
    only upload App Review rejected stops the run.
    """
    size = path.stat().st_size
    same = [
        image
        for image in images
        if attrs(image).get("fileName") == path.name and attrs(image).get("fileSize") == size
    ]
    usable = [image for image in same if attrs(image).get("state") not in CREATIVE_DEAD_STATES]
    rejected = [image for image in same if attrs(image).get("state") == "REJECTED"]
    if not usable and rejected:
        fail(
            f"App Review rejected {path.name}; replace the image (with a new file name) and re-run. "
            f"Details: {json.dumps(attrs(rejected[0]).get('stateDetails'))}"
        )
    return max(usable, key=lambda image: attrs(image).get("createdDate") or "") if usable else None


def wait_for_library_image(client: AscClient, image_id: str, name: str) -> dict[str, Any]:
    """Poll an uploaded image until Apple has processed it; fails on FAILED or timeout."""
    deadline = time.monotonic() + CREATIVE_PROCESSING_TIMEOUT_SECONDS
    while True:
        image = client.get(f"/appAssetLibraryImages/{image_id}").get("data") or {}
        state = attrs(image).get("state") or ""
        asset = attrs(image).get("imageAsset") or {}
        if state == "FAILED":
            fail(
                f"Apple could not process {name} ({image_id}): "
                f"{json.dumps(attrs(image).get('stateDetails'))}"
            )
        if state in CREATIVE_READY_STATES and asset.get("width") and asset.get("height"):
            return image
        if state in CREATIVE_PENDING_STATES or state == "APPROVED":
            return image
        if time.monotonic() >= deadline:
            fail(f"Apple did not finish processing {name} ({image_id}) in time; it is {state or 'in an unknown state'}")
        print(f"  waiting for Apple to process {name} ({state or 'unknown state'})")
        time.sleep(CREATIVE_POLL_SECONDS)


def upload_library_image(client: AscClient, library_id: str, path: Path) -> dict[str, Any]:
    """Reserve, upload and commit one creative image, then wait until Apple has processed it."""
    raw = path.read_bytes()
    reserved = client.post(
        "/appAssetLibraryImages",
        {
            "data": {
                "type": "appAssetLibraryImages",
                "attributes": {
                    "fileName": path.name,
                    "fileSize": len(raw),
                    "category": "CREATIVE_ASSETS",
                },
                "relationships": {
                    "assetLibrary": {"data": {"type": "appAssetLibraries", "id": library_id}}
                },
            }
        },
    )["data"]
    image_id = reserved["id"]
    send_upload_operations(client, reserved, raw, path.name)
    client.patch(
        f"/appAssetLibraryImages/{image_id}",
        {
            "data": {
                "type": "appAssetLibraryImages",
                "id": image_id,
                "attributes": {"uploaded": True},
            }
        },
    )
    print(f"  uploaded {path.name} to the Asset Library ({image_id})")
    return wait_for_library_image(client, image_id, path.name)


class CreativeStep:
    """What one creative slot needs, decided before anything is written.

    action: upload (then review), review, pending (in App Review), place,
    placed, or locked (approved, but the version cannot be edited).
    """

    def __init__(
        self,
        placement_type: str,
        label: str,
        path: Path,
        image: dict[str, Any] | None,
        action: str,
        swap_out: dict[str, Any] | None = None,
    ) -> None:
        self.placement_type = placement_type
        self.label = label
        self.path = path
        self.image = image
        self.action = action
        # The localization's placement in this slot that holds another image.
        self.swap_out = swap_out


class CreativePlan:
    def __init__(self, library_id: str, localization_id: str, steps: list[CreativeStep]) -> None:
        self.library_id = library_id
        self.localization_id = localization_id
        self.steps = steps


def image_placement_ids(client: AscClient, image_id: str) -> set[str]:
    return {
        row["id"]
        for row in iter_collection(client, f"/appAssetLibraryImages/{image_id}/relationships/placements?limit=50")
    }


def slot_placements(client: AscClient, localization_id: str, placement_type: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"filter[placementType]": placement_type, "limit": "50"})
    rows = iter_collection(client, f"/appStoreVersionLocalizations/{localization_id}/placements?{query}")
    return [row for row in rows if attrs(row).get("placementType", placement_type) == placement_type]


def plan_creative_assets(
    client: AscClient,
    *,
    app_id: str,
    version_id: str,
    locale: str,
    creative_dir: Path,
    editable: bool,
) -> CreativePlan:
    """Read-only: decide what each header / search results image needs.

    Fails before anything is written when an image App Review rejected has not
    been replaced, when a locked version (in a review submission) would need an
    upload or a review submission, or when an image is in a state the release
    cannot resolve.
    """
    sources = creative_sources(creative_dir)
    library_id = asset_library_id(client, app_id)
    images = library_images(client, library_id)
    localization_id = localization_for_locale(
        client, f"/appStoreVersions/{version_id}/appStoreVersionLocalizations", locale
    )["id"]
    steps: list[CreativeStep] = []
    for placement_type, label, path in sources:
        image = library_image_for(images, path)
        state = attrs(image).get("state") or ""
        if image is None or state in CREATIVE_PROCESSING_STATES | CREATIVE_READY_STATES:
            if not editable:
                fail(
                    f"cannot send the {label} image {path.name} to App Review while the version is in "
                    "a review submission; re-run once the version can be edited"
                )
            steps.append(CreativeStep(placement_type, label, path, image, "review" if image else "upload"))
            continue
        if state in CREATIVE_PENDING_STATES:
            print(f"  {label}: {path.name} is {state}; it is placed once App Review approves it")
            steps.append(CreativeStep(placement_type, label, path, image, "pending"))
            continue
        if state != "APPROVED":
            fail(f"the {label} image {path.name} is {state or 'in an unknown state'}; resolve it in App Store Connect")
        # Placed when one of the image's placements is this slot on this localization.
        placed = image_placement_ids(client, image["id"])
        slot = slot_placements(client, localization_id, placement_type)
        if any(row["id"] in placed and attrs(row).get("state") != "FAILED" for row in slot):
            print(f"  {label}: {path.name} is already on the {locale} product page")
            steps.append(CreativeStep(placement_type, label, path, image, "placed"))
            continue
        if not editable:
            # Approval can land between a submit and a re-run; the image is
            # placed by a later run, once the version can be edited.
            print(f"  {label}: {path.name} is approved; it is placed once the version can be edited")
            steps.append(CreativeStep(placement_type, label, path, image, "locked"))
            continue
        if len(slot) > 1:
            fail(
                f"the {locale} {label} slot holds {len(slot)} placements "
                f"({', '.join(row['id'] for row in slot)}); leave one in App Store Connect and re-run"
            )
        steps.append(CreativeStep(placement_type, label, path, image, "place", slot[0] if slot else None))
    return CreativePlan(library_id, localization_id, steps)


def place_creative_image(client: AscClient, localization_id: str, step: CreativeStep) -> None:
    """Put an approved image in its slot; replaces the slot's other image in the same request."""
    assert step.image is not None
    group = attrs(step.swap_out).get("placementGroup") or CREATIVE_PLACEMENT_GROUP
    relationships: dict[str, Any] = {
        "image": {"data": {"type": "appAssetLibraryImages", "id": step.image["id"]}},
        "appStoreVersionLocalization": {
            "data": {"type": "appStoreVersionLocalizations", "id": localization_id}
        },
    }
    if step.swap_out is not None:
        relationships["placementToSwapOut"] = {
            "data": {"type": "appAssetLibraryPlacements", "id": step.swap_out["id"]}
        }
    client.post(
        "/appAssetLibraryPlacements",
        {
            "data": {
                "type": "appAssetLibraryPlacements",
                "attributes": {"placementType": step.placement_type, "placementGroup": group},
                "relationships": relationships,
            }
        },
    )
    replaced = f", replacing placement {step.swap_out['id']}" if step.swap_out else ""
    print(f"  {step.label}: placed {step.path.name} on the product page{replaced}")


def apply_creative_plan(
    client: AscClient, plan: CreativePlan, *, app_id: str, reserved_draft: str | None = None
) -> dict[str, str]:
    """Upload, review and place what plan_creative_assets decided. Returns placement type -> outcome."""
    outcomes: dict[str, str] = {}
    to_review: list[tuple[str, dict[str, Any]]] = []
    for step in plan.steps:
        if step.action == "upload":
            step.image = upload_library_image(client, plan.library_id, step.path)
        elif step.action == "review" and step.image is not None and (
            attrs(step.image).get("state") in CREATIVE_PROCESSING_STATES
        ):
            # An earlier run uploaded it; Apple may still be processing it.
            step.image = wait_for_library_image(client, step.image["id"], step.path.name)
        if step.action in {"upload", "review"}:
            assert step.image is not None
            to_review.append((step.label, step.image))
            outcomes[step.placement_type] = "submitted for review"
    if to_review:
        submit_creative_review(client, app_id, to_review, reserved_draft=reserved_draft)
    for step in plan.steps:
        if step.action == "place":
            place_creative_image(client, plan.localization_id, step)
            outcomes[step.placement_type] = "placed"
        elif step.action in {"pending", "placed", "locked"}:
            outcomes[step.placement_type] = step.action
    return outcomes


def submit_creative_review(
    client: AscClient,
    app_id: str,
    images: list[tuple[str, dict[str, Any]]],
    *,
    reserved_draft: str | None = None,
) -> str:
    """Send not-yet-reviewed creative images to App Review in a submission of their own.

    A draft an earlier attempt left holding only some of these images is
    finished first, else an empty draft, and a new one is created only when
    neither exists, so re-runs never pile up drafts. The draft this run keeps
    for the version and its products (reserved_draft) is never taken. Nothing is
    submitted unless the draft then holds exactly these images.
    """
    wanted = {image["id"] for _, image in images}
    partial = empty = None
    for draft in draft_submission_ids(client, app_id):
        if draft == reserved_draft:
            continue
        items = draft_item_relationships(client, draft)
        # An item that is not an image adds None, which is never wanted.
        held = {related_id(item, "appAssetLibraryImage") for item in items}
        if not items:
            empty = empty or draft
        elif held <= wanted:
            partial = draft
            break
    submission_id = partial or empty
    if submission_id:
        print(f"  finishing review submission {submission_id} from an earlier attempt")
    else:
        submission_id = create_draft_submission(client, app_id)
    held = {related_id(item, "appAssetLibraryImage") for item in draft_item_relationships(client, submission_id)}
    for label, image in images:
        if image["id"] in held:
            print(f"  {label}: {attrs(image).get('fileName')} is already in review submission {submission_id}")
            continue
        client.post(
            "/reviewSubmissionItems",
            {
                "data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {
                            "data": {"type": "reviewSubmissions", "id": submission_id}
                        },
                        "appAssetLibraryImage": {
                            "data": {"type": "appAssetLibraryImages", "id": image["id"]}
                        },
                    },
                }
            },
        )
        print(f"  {label}: {attrs(image).get('fileName')} added to review submission {submission_id}")
    items = draft_item_relationships(client, submission_id)
    held_ids = [related_id(item, "appAssetLibraryImage") for item in items]
    if len(items) != len(wanted) or set(held_ids) != wanted:
        unexpected = [item.get("id") for item in items if related_id(item, "appAssetLibraryImage") not in wanted]
        fail(
            f"review submission {submission_id} does not hold exactly the header and search results "
            f"images (unexpected items: {unexpected or 'none'}); not submitting it"
        )
    submit_product_draft(client, submission_id, what="creative image")
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


def draft_submission_ids(client: AscClient, app_id: str) -> list[str]:
    """The app's READY_FOR_REVIEW (draft, not yet submitted) review submissions."""
    return [
        row["id"]
        for row in iter_collection(
            client,
            f"/apps/{app_id}/reviewSubmissions?"
            + urllib.parse.urlencode({"filter[state]": "READY_FOR_REVIEW", "limit": "5"}),
        )
    ]


def find_draft_submission(
    client: AscClient, app_id: str, version_id: str | None = None
) -> str | None:
    """The draft to use for the App Store version and its products, if there is one.

    The draft already holding version_id wins; otherwise the first draft holding
    no app version, so another version's draft is never touched. A draft holding
    header or search results images belongs to their own review and is skipped,
    so those images never go out with the version.
    """
    drafts = []
    for submission_id in draft_submission_ids(client, app_id):
        items = draft_item_relationships(client, submission_id)
        if any(related_id(item, "appAssetLibraryImage") for item in items):
            continue
        versions = {related_id(item, "appStoreVersion") for item in items} - {None, ""}
        drafts.append((submission_id, versions))
    if version_id:
        for submission_id, versions in drafts:
            if version_id in versions:
                return submission_id
    for submission_id, versions in drafts:
        if not versions:
            return submission_id
    return None


def create_draft_submission(client: AscClient, app_id: str) -> str:
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


def get_or_create_draft_submission(client: AscClient, app_id: str) -> str:
    """The app's draft review submission for versions and products (see find_draft_submission), creating one."""
    return find_draft_submission(client, app_id) or create_draft_submission(client, app_id)


def version_draft(
    client: AscClient, app_id: str, version_id: str, version_state: str
) -> str | None:
    """Read-only: pick and check the draft the version and its products go out in.

    None means there is none yet (one is created at submit). A READY_FOR_REVIEW
    version already sits in a draft; when no usable draft holds it, this stops
    the run instead of submitting another draft. The preflight runs it before
    the build, and the store step again before its first write.
    """
    draft = find_draft_submission(client, app_id, version_id)
    if version_state == "READY_FOR_REVIEW" and (
        draft is None or version_id not in draft_item_version_ids(client, draft)
    ):
        fail(
            f"App Store version {version_id} is READY_FOR_REVIEW, but no draft review submission "
            "without header or search results images holds it; remove the version from its "
            "submission in App Store Connect and re-run"
        )
    return draft


def reserve_version_draft(
    client: AscClient, app_id: str, version_id: str, version_state: str
) -> str | None:
    """Pick and check the version's draft before anything is written, and reserve it.

    The draft is kept for the version and its products: creative images never take it.
    """
    draft = version_draft(client, app_id, version_id, version_state)
    if draft:
        print(f"  review submission {draft} is kept for the version and its products")
    return draft


def draft_item_relationships(client: AscClient, submission_id: str) -> list[dict[str, Any]]:
    """Return the draft's items with their relationships populated.

    App Store Connect omits `relationships` from Review Submission Items unless
    they are requested with `include`, so every relationship the readers look
    at (versions, products and Asset Library images) is asked for explicitly.
    """
    return iter_collection(
        client,
        f"/reviewSubmissions/{submission_id}/items?limit=50&include={DRAFT_ITEM_INCLUDE}",
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


def submit_product_draft(client: AscClient, submission_id: str, *, what: str = "product") -> None:
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
    print(f"  submitted {what} review submission {submission_id}")


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
    upload_creative: bool = False,
    creative_dir: Path | None = None,
) -> list[str]:
    """Check the repository's listing, screenshots and creative images before any API call. Returns the locales."""
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
        creative_sources(creative_dir or DEFAULT_CREATIVE_DIR)
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
    parser.add_argument(
        "--creative-dir",
        type=Path,
        default=DEFAULT_CREATIVE_DIR,
        help="Product page header and search results images, one folder per slot: "
        + ", ".join(folder for folder, _, _, _ in CREATIVE_SLOTS),
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
        print("ASC dry-run — local inputs OK; no API calls")
        print(
            f"  bundle={args.bundle_id} locales={','.join(locales) or '-'} listing={do_listing} "
            f"whats_new={do_whats_new} screenshots={do_screenshots} creative={do_creative} "
            f"submit={do_submit} public_release={do_public}"
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
    # review, or screenshots already in a draft) only compares, read-only. The
    # header and search results images are locked like the screenshots once the
    # version is in a review submission.
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
    waiting = state == "WAITING_FOR_REVIEW"
    if do_submit and not waiting and state not in SUBMITTABLE_VERSION_STATES:
        fail(
            f"version {args.version!r} cannot be submitted for review (state={state})"
        )
    text_frozen = state if text_access == VERIFY else None
    shots_frozen = state if shot_access == VERIFY else None
    # Pick and check the version's draft before any write. It is reserved for
    # the version and its products, so header and search results images never
    # go out in it.
    reserved_draft: str | None = None
    if do_submit and not waiting:
        reserved_draft = reserve_version_draft(client, app_id, version_id, state)
    # Read-only too, so an image the version cannot take stops the run before any write.
    creative_plan: CreativePlan | None = None
    if do_creative:
        print("checking ASC product page header and search results images…")
        creative_plan = plan_creative_assets(
            client,
            app_id=app_id,
            version_id=version_id,
            locale=PRIMARY_LOCALE,
            creative_dir=args.creative_dir,
            editable=creative_access == EDIT,
        )

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
        if creative_plan is not None:
            # Nothing is uploaded, reviewed or placed while the version is locked:
            # the plan above already stopped the run if an image needed that.
            print("syncing ASC product page header and search results images…")
            apply_creative_plan(client, creative_plan, app_id=app_id, reserved_draft=reserved_draft)

    if waiting:
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
        submission_id = reserved_draft or create_draft_submission(client, app_id)
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
