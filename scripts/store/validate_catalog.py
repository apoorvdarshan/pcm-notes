#!/usr/bin/env python3
"""Check store/catalog/products.json against the app and its StoreKit configuration.

The catalog decides which App Store products a release may submit for review
(scripts/store/asc_release.py) and, with IOS_CREATE_PRODUCTS, which ones it
creates (scripts/store/asc_products.py). It must list exactly the tip products
the app sells (TipProductID in TipStore.swift) and the local StoreKit
configuration tests, with matching types. Also checks the App Store creation
fields (`ios` blocks, `ios_subscription_groups`) offline, including each review
screenshot file. Read-only; never calls a store.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asc_products  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PRODUCTS = ROOT / "store" / "catalog" / "products.json"
TIP_STORE = ROOT / "ios" / "PCMNotes" / "PCMNotes" / "TipStore.swift"
STOREKIT = ROOT / "ios" / "PCMNotes" / "PCMNotes.storekit"
BUNDLE_ID = "com.apoorvdarshan.iitjee"
# products.json type -> StoreKit configuration "type"
STOREKIT_TYPES = {"consumable": "Consumable", "non_consumable": "NonConsumable"}
# The App Store type asc_products.py gives each group -> the products.json type it needs.
CATALOG_TYPES = {"CONSUMABLE": "consumable", "NON_CONSUMABLE": "non_consumable"}


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def app_tip_ids(source: str) -> list[str]:
    match = re.search(r"enum TipProductID\b[^{]*\{(.*?)\n\}", source, flags=re.DOTALL)
    if not match:
        fail(f"could not find enum TipProductID in {TIP_STORE}")
    ids = re.findall(r'case\s+\w+\s*=\s*"([^"]+)"', match.group(1))
    if not ids:
        fail(f"enum TipProductID in {TIP_STORE} lists no product ids")
    return ids


def check(catalog: dict[str, Any], *, tip_source: str | None = None,
          storekit: dict[str, Any] | None = None, root: Path = ROOT) -> list[str]:
    """Validate the catalog; returns every product id. Fails on any mismatch."""
    if catalog.get("bundle_id") != BUNDLE_ID:
        fail(f"products.json bundle_id must be {BUNDLE_ID}")
    for group in asc_products.REQUIRED_GROUPS:
        if not isinstance(catalog.get(group), list):
            fail(f"products.json {group!r} must be a list")
    seen: list[str] = []
    for group, (_, iap_type) in asc_products.PRODUCT_GROUPS.items():
        items = catalog.get(group, [])
        if not isinstance(items, list):
            fail(f"products.json {group!r} must be a list")
        for item in items:
            pid = item.get("id")
            if not isinstance(pid, str) or not pid.startswith(f"{BUNDLE_ID}."):
                fail(f"{group}: product id {pid!r} must start with {BUNDLE_ID}.")
            # PCM Notes is iOS only; a product without `ios` would be neither
            # created nor submitted by a release.
            if item.get("platforms") != ["ios"]:
                fail(f"{pid}: platforms must be [\"ios\"]")
            # A product the release has yet to create has no App Store Connect id.
            if "apple_id" in item and not re.fullmatch(r"[0-9]+", str(item["apple_id"])):
                fail(f"{pid}: apple_id must be the numeric App Store Connect id")
            ios = item.get("ios") if isinstance(item.get("ios"), dict) else {}
            name, ios_name = item.get("reference_name"), ios.get("reference_name")
            if not name and not ios_name:
                fail(f"{pid}: reference_name (or ios.reference_name) is required")
            if name and ios_name and name != ios_name:
                fail(f"{pid}: reference_name {name!r} differs from ios.reference_name {ios_name!r}")
            if group != "subscriptions":
                if item.get("type") not in STOREKIT_TYPES:
                    fail(f"{pid}: type must be one of {', '.join(STOREKIT_TYPES)}")
                if item["type"] != CATALOG_TYPES[iap_type]:
                    fail(f"{pid}: {group} are {CATALOG_TYPES[iap_type]} on the App Store, but type is {item['type']}")
            seen.append(pid)
    if len(seen) != len(set(seen)):
        fail("duplicate product ids in products.json")
    problems = asc_products.catalog_problems(catalog, root)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        raise SystemExit(1)

    tips = [item["id"] for item in catalog["tips"]]
    app_ids = app_tip_ids(tip_source if tip_source is not None else TIP_STORE.read_text(encoding="utf-8"))
    if tips != app_ids:
        fail(f"products.json tips {tips} do not match TipProductID in TipStore.swift {app_ids}")

    config = storekit if storekit is not None else json.loads(STOREKIT.read_text(encoding="utf-8"))
    configured = {p["productID"]: p.get("type") for p in config.get("products") or []}
    expected = {
        item["id"]: STOREKIT_TYPES[item["type"]]
        for group in ("consumables", "non_consumables", "tips") for item in catalog.get(group, [])
    }
    if configured != expected:
        fail(f"PCMNotes.storekit products {configured} do not match products.json {expected}")
    return seen


def main() -> None:
    ids = check(json.loads(PRODUCTS.read_text(encoding="utf-8")))
    print(f"catalog OK: {len(ids)} products match TipStore.swift and PCMNotes.storekit")


if __name__ == "__main__":
    main()
