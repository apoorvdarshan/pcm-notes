#!/usr/bin/env python3
"""Export the live App Store listing into store/metadata/ios/<locale>/ (read-only).

Every locale of the version on sale is written: name, subtitle and privacy URL from
the app information; description, keywords, promotional text, marketing/support
URLs and What's New from the version. Only GET requests are made. Use it to
re-sync the repository after editing the listing in App Store Connect by hand:

  APP_STORE_CONNECT_API_KEY_ID=… APP_STORE_CONNECT_ISSUER_ID=… \\
  APP_STORE_CONNECT_API_KEY_P8="$(cat AuthKey_….p8)" \\
    python3 scripts/store/export_listing.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asc_release  # noqa: E402

FIELDS = {
    **{field: filename for field, filename, _ in asc_release.APP_INFO_FIELDS},
    **{field: filename for field, filename, _ in asc_release.LISTING_FIELDS},
    asc_release.WHATS_NEW_FIELD[0]: asc_release.WHATS_NEW_FIELD[1],
}


def write(directory: Path, attributes: dict, fields: list[str]) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for field in fields:
        value = (attributes.get(field) or "").strip()
        path = directory / FIELDS[field]
        if value:
            path.write_text(value + "\n", encoding="utf-8")
            written.append(path.name)
        elif path.exists():
            path.unlink()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=asc_release.ROOT / "store" / "metadata")
    args = parser.parse_args()

    client = asc_release.client_from_environment()
    app_id = asc_release.find_app(client, asc_release.DEFAULT_BUNDLE_ID)
    live = asc_release.live_ios_version(client, app_id)
    if live is None:
        asc_release.fail("no iOS version is on the App Store yet")
    version = (live.get("attributes") or {}).get("versionString")
    infos = asc_release.iter_collection(client, f"/apps/{app_id}/appInfos")
    live_info = next(
        (info for info in infos if asc_release.app_info_state(info) not in asc_release.EDITABLE_APP_INFO_STATES),
        None,
    )
    if live_info is None:
        asc_release.fail("no live app information record")

    app_fields = [field for field, _, _ in asc_release.APP_INFO_FIELDS]
    version_fields = [field for field, _, _ in asc_release.LISTING_FIELDS] + [asc_release.WHATS_NEW_FIELD[0]]
    for loc in asc_release.iter_collection(client, f"/appInfos/{live_info['id']}/appInfoLocalizations"):
        attrs = loc.get("attributes") or {}
        names = write(args.out / "ios" / attrs["locale"], attrs, app_fields)
        print(f"{attrs['locale']}: app information → {', '.join(names) or 'nothing'}")
    for loc in asc_release.iter_collection(
        client, f"/appStoreVersions/{live['id']}/appStoreVersionLocalizations"
    ):
        attrs = loc.get("attributes") or {}
        names = write(args.out / "ios" / attrs["locale"], attrs, version_fields)
        print(f"{attrs['locale']}: version {version} → {', '.join(names) or 'nothing'}")


if __name__ == "__main__":
    main()
