#!/usr/bin/env python3
"""Check store/catalog/products.json against the app and its StoreKit configuration.

The catalog decides which App Store products a release may submit for review
(scripts/store/asc_release.py). It must list exactly the tip products the app sells
(TipProductID in TipStore.swift) and the local StoreKit configuration tests, with
matching types. Read-only; never calls a store.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PRODUCTS = ROOT / "store" / "catalog" / "products.json"
TIP_STORE = ROOT / "ios" / "PCMNotes" / "PCMNotes" / "TipStore.swift"
STOREKIT = ROOT / "ios" / "PCMNotes" / "PCMNotes.storekit"
BUNDLE_ID = "com.apoorvdarshan.iitjee"
GROUPS = ("subscriptions", "consumables", "tips")
# products.json type -> StoreKit configuration "type"
STOREKIT_TYPES = {"consumable": "Consumable", "non_consumable": "NonConsumable"}


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
          storekit: dict[str, Any] | None = None) -> list[str]:
    """Validate the catalog; returns every product id. Fails on any mismatch."""
    if catalog.get("bundle_id") != BUNDLE_ID:
        fail(f"products.json bundle_id must be {BUNDLE_ID}")
    seen: list[str] = []
    for group in GROUPS:
        items = catalog.get(group)
        if not isinstance(items, list):
            fail(f"products.json {group!r} must be a list")
        for item in items:
            pid = item.get("id")
            if not isinstance(pid, str) or not pid.startswith(f"{BUNDLE_ID}."):
                fail(f"{group}: product id {pid!r} must start with {BUNDLE_ID}.")
            if not re.fullmatch(r"[0-9]+", str(item.get("apple_id", ""))):
                fail(f"{pid}: apple_id must be the numeric App Store Connect id")
            if not item.get("reference_name"):
                fail(f"{pid}: reference_name is required")
            if group != "subscriptions" and item.get("type") not in STOREKIT_TYPES:
                fail(f"{pid}: type must be one of {', '.join(STOREKIT_TYPES)}")
            seen.append(pid)
    if len(seen) != len(set(seen)):
        fail("duplicate product ids in products.json")

    tips = [item["id"] for item in catalog["tips"]]
    app_ids = app_tip_ids(tip_source if tip_source is not None else TIP_STORE.read_text(encoding="utf-8"))
    if tips != app_ids:
        fail(f"products.json tips {tips} do not match TipProductID in TipStore.swift {app_ids}")

    config = storekit if storekit is not None else json.loads(STOREKIT.read_text(encoding="utf-8"))
    configured = {p["productID"]: p.get("type") for p in config.get("products") or []}
    expected = {
        item["id"]: STOREKIT_TYPES[item["type"]]
        for group in ("consumables", "tips") for item in catalog[group]
    }
    if configured != expected:
        fail(f"PCMNotes.storekit products {configured} do not match products.json {expected}")
    return seen


def main() -> None:
    ids = check(json.loads(PRODUCTS.read_text(encoding="utf-8")))
    print(f"catalog OK: {len(ids)} products match TipStore.swift and PCMNotes.storekit")


if __name__ == "__main__":
    main()
