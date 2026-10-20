#!/usr/bin/env python3
"""Offline tests for iOS release switches, build waiting, listing, build selection and the asc runner.

No network: every App Store Connect call goes to an in-memory fake of App Store
Connect behind `asc`.
Run: python3 -m unittest scripts/store/test_ios_release.py
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import plistlib
import struct
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import unittest.mock as mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import asc_cli  # noqa: E402
import asc_release  # noqa: E402
import export_listing  # noqa: E402
import print_gates  # noqa: E402
import redact_logs  # noqa: E402
import release_notes  # noqa: E402
import validate_catalog  # noqa: E402
import ios_release  # noqa: E402

ROOT = HERE.parents[1]
TAG = "v1.0.1"
VERSION = "1.0.1"
ALL_ON = {
    "IOS_RELEASE_AUTOMATION": "true",
    "IOS_CREATE_VERSION": "true",
    "IOS_UPLOAD_BUILD": "true",
    "IOS_UPLOAD_LISTING": "true",
    "IOS_UPLOAD_WHATS_NEW": "true",
    "IOS_UPLOAD_SCREENSHOTS": "true",
    "IOS_UPLOAD_CREATIVE_ASSETS": "true",
    "IOS_SUBMIT_REVIEW": "true",
    "IOS_PUBLIC_RELEASE": "true",
}
OPS = ("create_version", "upload_build", "listing", "whats_new", "screenshots", "creative", "submit", "public_release")
FLAG_NAMES = {"UPLOAD_LISTING", "UPLOAD_WHATS_NEW", "UPLOAD_SCREENSHOTS", "UPLOAD_CREATIVE_ASSETS", "SUBMIT_IOS_REVIEW",
              "PUBLIC_RELEASE", "CREATE_IOS_VERSION"}


def gates(env: dict[str, str], *, event: str = "push", ref: str = TAG) -> dict:
    return ios_release.resolve_gates(env, event=event, ref_name=ref, marketing_version=VERSION)


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


class GateTests(unittest.TestCase):
    def test_missing_settings_mean_everything_off(self) -> None:
        result = gates({})
        self.assertEqual(result["mode"], "release")
        self.assertFalse(result["master"])
        self.assertFalse(result["build"])
        self.assertFalse(result["asc"])
        for key in OPS:
            self.assertFalse(result[key], key)
        self.assertEqual(result["poll_seconds"], 30)
        self.assertEqual(result["wait_minutes"], 120)

    def test_master_off_ignores_operation_switches(self) -> None:
        result = gates({**ALL_ON, "IOS_RELEASE_AUTOMATION": "false"})
        for key in OPS:
            self.assertFalse(result[key], key)
        self.assertFalse(result["build"])
        self.assertTrue(any("ignored" in note for note in result["notes"]))

    def test_each_operation_is_independent(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": "yes", "IOS_UPLOAD_LISTING": "on"})
        self.assertTrue(result["listing"])
        self.assertTrue(result["asc"])
        self.assertFalse(result["whats_new"])
        self.assertFalse(result["upload_build"])
        self.assertFalse(result["build"])

    def test_upload_build_alone_builds_without_touching_the_listing(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": "1", "IOS_UPLOAD_BUILD": "1"})
        self.assertTrue(result["build"])
        self.assertFalse(result["asc"])

    def test_creative_assets_are_their_own_switch(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_CREATIVE_ASSETS": "true"})
        self.assertTrue(result["creative"])
        self.assertTrue(result["asc"])
        self.assertFalse(result["screenshots"])
        self.assertFalse(result["build"])
        self.assertFalse(gates({"IOS_UPLOAD_CREATIVE_ASSETS": "true"})["creative"])
        with self.assertRaisesRegex(ios_release.ReleaseError, "IOS_UPLOAD_CREATIVE_ASSETS"):
            gates({"IOS_UPLOAD_CREATIVE_ASSETS": "sometimes"})

    def test_all_on(self) -> None:
        result = gates(ALL_ON)
        for key in OPS:
            self.assertTrue(result[key], key)
        self.assertTrue(result["build"])

    def test_create_version_needs_only_the_master_switch(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": "true", "IOS_CREATE_VERSION": "true"})
        self.assertTrue(result["create_version"])
        self.assertFalse(result["build"])
        self.assertFalse(result["asc"])
        self.assertFalse(gates({"IOS_CREATE_VERSION": "true"})["create_version"])
        self.assertFalse(gates(ALL_ON, event="workflow_dispatch", ref="main")["create_version"])

    def test_invalid_values_are_rejected_even_when_master_is_off(self) -> None:
        for name in ("IOS_RELEASE_AUTOMATION", "IOS_CREATE_VERSION", "IOS_SUBMIT_REVIEW", "IOS_UPLOAD_WHATS_NEW",
                     "IOS_UPLOAD_SCREENSHOTS", "IOS_UPLOAD_CREATIVE_ASSETS"):
            with self.subTest(name=name), self.assertRaisesRegex(ios_release.ReleaseError, name):
                gates({name: "maybe"})
        with self.assertRaisesRegex(ios_release.ReleaseError, "IOS_UPLOAD_BUILD"):
            gates({"IOS_RELEASE_AUTOMATION": "false", "IOS_UPLOAD_BUILD": "enabled"})

    def test_switch_values_are_case_and_space_insensitive(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": " TRUE ", "IOS_UPLOAD_LISTING": "Yes"})
        self.assertTrue(result["listing"])
        self.assertFalse(gates({"IOS_RELEASE_AUTOMATION": "Off"})["master"])

    def test_bad_timing_values_are_rejected(self) -> None:
        for env in (
            {"IOS_BUILD_POLL_SECONDS": "5"},
            {"IOS_BUILD_POLL_SECONDS": "601"},
            {"IOS_BUILD_POLL_SECONDS": "thirty"},
            {"IOS_BUILD_POLL_SECONDS": "٣٠"},
            {"IOS_BUILD_WAIT_MINUTES": "0"},
            {"IOS_BUILD_WAIT_MINUTES": "999"},
            {"IOS_BUILD_WAIT_MINUTES": "-30"},
        ):
            with self.subTest(env=env), self.assertRaises(ios_release.ReleaseError):
                gates(env)
        result = gates({"IOS_BUILD_POLL_SECONDS": "45", "IOS_BUILD_WAIT_MINUTES": "90"})
        self.assertEqual((result["poll_seconds"], result["wait_minutes"]), (45, 90))
        bounds = gates({"IOS_BUILD_POLL_SECONDS": "10", "IOS_BUILD_WAIT_MINUTES": "300"})
        self.assertEqual((bounds["poll_seconds"], bounds["wait_minutes"]), (10, 300))

    def test_submit_requires_this_runs_build(self) -> None:
        env = {"IOS_RELEASE_AUTOMATION": "true", "IOS_SUBMIT_REVIEW": "true"}
        with self.assertRaisesRegex(ios_release.ReleaseError, "IOS_UPLOAD_BUILD"):
            gates(env)

    def test_public_release_requires_submit(self) -> None:
        env = {"IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_BUILD": "true", "IOS_PUBLIC_RELEASE": "true"}
        with self.assertRaisesRegex(ios_release.ReleaseError, "IOS_SUBMIT_REVIEW"):
            gates(env)

    def test_tag_must_equal_marketing_version_exactly(self) -> None:
        self.assertEqual(gates({}, ref="v1.0.1")["marketing_version"], VERSION)
        for tag in ("v1.0.2", "v1.0", "v1.0.1.0", "v1.0.1-beta", "v01.0.1"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ios_release.ReleaseError, "does not match"):
                gates({}, ref=tag)
        for tag in ("ios-v1.0.1", "1.0.1", "release-1.0.1"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ios_release.ReleaseError, r"v\* tag"):
                gates({}, ref=tag)

    def test_historical_tag_style_no_longer_matches(self) -> None:
        # v1.0.0 was pushed for MARKETING_VERSION 1.0; the rule is now exact.
        with self.assertRaisesRegex(ios_release.ReleaseError, "needs v1.0"):
            ios_release.resolve_gates({}, event="push", ref_name="v1.0.0", marketing_version="1.0")

    def test_manual_run_validates_only(self) -> None:
        result = gates(ALL_ON, event="workflow_dispatch", ref="main")
        self.assertEqual(result["mode"], "validate")
        self.assertTrue(result["build"])
        self.assertFalse(result["master"])
        self.assertFalse(result["asc"])
        for key in OPS:
            self.assertFalse(result[key], key)

    def test_manual_run_still_rejects_invalid_switches(self) -> None:
        with self.assertRaisesRegex(ios_release.ReleaseError, "IOS_UPLOAD_LISTING"):
            gates({"IOS_UPLOAD_LISTING": "sure"}, event="workflow_dispatch", ref="main")

    def test_manual_run_can_validate_a_future_version(self) -> None:
        result = ios_release.resolve_gates({}, event="workflow_dispatch", ref_name="main",
                                           marketing_version="1.0", validation_version="1.0.1")
        self.assertEqual(result["mode"], "validate")
        self.assertEqual(result["marketing_version"], "1.0.1")
        self.assertFalse(result["asc"])
        default = ios_release.resolve_gates({}, event="workflow_dispatch", ref_name="main", marketing_version="1.0")
        self.assertEqual(default["marketing_version"], "1.0")

    def test_version_override_is_rejected_for_releases_and_bad_values(self) -> None:
        with self.assertRaisesRegex(ios_release.ReleaseError, "manual validation"):
            ios_release.resolve_gates({}, event="push", ref_name=TAG, marketing_version=VERSION,
                                      validation_version="1.0.2")
        for bad in ("1.x", "v1.0.1", "1.0.1.2", "1.0.1 beta", "١.٠"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ios_release.ReleaseError, "must look like"):
                ios_release.resolve_gates({}, event="workflow_dispatch", ref_name="main",
                                          marketing_version=VERSION, validation_version=bad)

    def test_other_events_are_refused(self) -> None:
        for event in ("pull_request", "schedule", ""):
            with self.subTest(event=event), self.assertRaisesRegex(ios_release.ReleaseError, "unsupported event"):
                gates({}, event=event)

    def test_project_versions_are_consistent(self) -> None:
        self.assertRegex(ios_release.single_project_value("MARKETING_VERSION"), r"^\d+(\.\d+){0,2}$")
        self.assertRegex(ios_release.single_project_value("CURRENT_PROJECT_VERSION"), r"^\d+$")
        with tempfile.TemporaryDirectory() as tmp:
            pbx = Path(tmp) / "project.pbxproj"
            pbx.write_text("MARKETING_VERSION = 1.0;\nMARKETING_VERSION = 1.1;\n")
            with self.assertRaisesRegex(ios_release.ReleaseError, "identical"):
                ios_release.single_project_value("MARKETING_VERSION", pbx)

    def test_cli_writes_outputs_for_the_workflow(self) -> None:
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF_NAME": "main",
               "VALIDATION_MARKETING_VERSION": "1.0.1"}
        with tempfile.NamedTemporaryFile("r", suffix=".out") as out, mock.patch.dict(
            os.environ, {**env, "GITHUB_OUTPUT": out.name}, clear=True
        ), redirect_stdout(io.StringIO()):
            ios_release.main(["gates"])
            written = dict(line.split("=", 1) for line in Path(out.name).read_text().split())
        self.assertEqual(written["mode"], "validate")
        self.assertEqual(written["build"], "true")
        self.assertEqual(written["marketing_version"], "1.0.1")
        self.assertEqual(written["upload_build"], "false")
        self.assertEqual(written["create_version"], "false")
        self.assertEqual(written["creative"], "false")

    def test_cli_exits_non_zero_on_a_bad_switch(self) -> None:
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": TAG, "IOS_SUBMIT_REVIEW": "maybe"}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit) as raised:
            quiet(ios_release.main, ["gates"])
        self.assertEqual(raised.exception.code, 1)


class ExportComplianceTests(unittest.TestCase):
    """ITSAppUsesNonExemptEncryption must be declared, or App Store Connect asks about every build."""

    NO = "\t\t\t\tINFOPLIST_KEY_ITSAppUsesNonExemptEncryption = NO;\n"
    YES = "\t\t\t\tINFOPLIST_KEY_ITSAppUsesNonExemptEncryption = YES;\n"
    CODE = "\t\t\t\tINFOPLIST_KEY_ITSEncryptionExportComplianceCode = ABC123;\n"

    @staticmethod
    def configuration(name: str, bundle_id: str, extra: str = "") -> str:
        """One XCBuildConfiguration block, laid out the way Xcode writes it."""
        return (
            f"\t\tAAA{name}{len(bundle_id)} /* {name} */ = {{\n"
            "\t\t\tisa = XCBuildConfiguration;\n"
            "\t\t\tbuildSettings = {\n"
            "\t\t\t\tLD_RUNPATH_SEARCH_PATHS = (\n"
            "\t\t\t\t\t\"$(inherited)\",\n"
            "\t\t\t\t);\n"
            f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {bundle_id};\n"
            f"{extra}"
            "\t\t\t};\n"
            f"\t\t\tname = {name};\n"
            "\t\t};\n"
        )

    def check(self, debug: str, release: str, others: str = "") -> str:
        app = ios_release.BUNDLE_ID
        text = "/* project */\n" + self.configuration("Debug", app, debug) + self.configuration("Release", app, release)
        with tempfile.TemporaryDirectory() as tmp:
            pbx = Path(tmp) / "project.pbxproj"
            pbx.write_text(text + others)
            return ios_release.check_export_compliance(pbx)

    def test_the_app_declares_no_non_exempt_encryption(self) -> None:
        self.assertEqual(ios_release.check_export_compliance(), "NO")
        configurations = ios_release.app_build_configurations()
        self.assertEqual(sorted(configurations), ["Debug", "Release"])
        for name, settings in configurations.items():
            with self.subTest(name=name):
                self.assertEqual(settings["INFOPLIST_KEY_ITSAppUsesNonExemptEncryption"], "NO")
                self.assertEqual(settings["INFOPLIST_KEY_CFBundleDisplayName"], "PCM Notes")

    def test_no_in_every_configuration_passes(self) -> None:
        self.assertEqual(self.check(self.NO, self.NO), "NO")

    def test_a_configuration_without_the_key_fails_before_the_build(self) -> None:
        # A Debug-only value would leave the Release build that ships without it.
        with self.assertRaisesRegex(ios_release.ReleaseError, "Release configuration does not declare export compliance"):
            self.check(self.NO, "")
        with self.assertRaisesRegex(ios_release.ReleaseError, "Debug configuration does not declare export compliance"):
            self.check("", self.NO)

    def test_a_value_on_another_target_does_not_count_for_the_app(self) -> None:
        tests = "".join(self.configuration(name, ios_release.BUNDLE_ID + "Tests", self.NO) for name in ("Debug", "Release"))
        with self.assertRaisesRegex(ios_release.ReleaseError, "does not declare export compliance"):
            self.check("", "", tests)
        self.assertEqual(self.check(self.NO, self.NO, tests.replace("= NO;", "= YES;")), "NO")

    def test_yes_needs_the_compliance_code_in_that_configuration(self) -> None:
        with self.assertRaisesRegex(ios_release.ReleaseError,
                                    "YES in the Release configuration needs INFOPLIST_KEY_ITSEncryptionExportComplianceCode"):
            self.check(self.YES + self.CODE, self.YES)
        self.assertEqual(self.check(self.YES + self.CODE, self.YES + self.CODE), "YES")

    def test_mixed_or_unknown_values_fail(self) -> None:
        with self.assertRaisesRegex(ios_release.ReleaseError, "in every app configuration alike"):
            self.check(self.NO, self.YES + self.CODE)
        with self.assertRaisesRegex(ios_release.ReleaseError, "must be NO or YES"):
            self.check(self.NO, "\t\t\t\tINFOPLIST_KEY_ITSAppUsesNonExemptEncryption = maybe;\n")

    def test_missing_or_duplicate_app_configurations_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pbx = Path(tmp) / "project.pbxproj"
            pbx.write_text("/* project */\n" + self.configuration("Release", ios_release.BUNDLE_ID + "Tests", self.NO))
            with self.assertRaisesRegex(ios_release.ReleaseError, "could not find the Debug and Release configurations"):
                ios_release.check_export_compliance(pbx)
            pbx.write_text("/* project */\n" + self.configuration("Release", ios_release.BUNDLE_ID, self.NO))
            with self.assertRaisesRegex(ios_release.ReleaseError, r"found \['Release'\]"):
                ios_release.check_export_compliance(pbx)
        with self.assertRaisesRegex(ios_release.ReleaseError, "two Release configurations"):
            self.check(self.NO, self.NO, self.configuration("Release", ios_release.BUNDLE_ID, self.NO))

    def run_gates(self, env: dict[str, str]) -> tuple[int, str]:
        out = io.StringIO()
        code = 0
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            ios_release, "single_project_value", return_value=VERSION
        ), redirect_stdout(out), redirect_stderr(out):
            try:
                ios_release.main(["gates"])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()

    def test_gates_check_it_only_when_this_run_builds(self) -> None:
        validation = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF_NAME": "main"}
        upload = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": TAG,
                  "IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_BUILD": "true"}
        no_build = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": TAG,
                    "IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_LISTING": "true"}
        missing = ios_release.ReleaseError("the app does not declare export compliance")
        for env, builds in ((validation, True), (upload, True), (no_build, False),
                            ({**no_build, "IOS_RELEASE_AUTOMATION": ""}, False)):
            with self.subTest(env=env), mock.patch.object(ios_release, "check_export_compliance", side_effect=missing) as check:
                code, output = self.run_gates(env)
                self.assertEqual(code, 1 if builds else 0, output)
                self.assertEqual(check.called, builds)
                if builds:
                    self.assertIn("does not declare export compliance", output)

    def test_gates_report_the_projects_answer(self) -> None:
        code, output = self.run_gates({"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF_NAME": "main"})
        self.assertEqual(code, 0, output)
        self.assertIn("export compliance: ITSAppUsesNonExemptEncryption = NO", output)

    def archived(self, info: dict, fmt: plistlib.PlistFormat = plistlib.FMT_BINARY) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "Info.plist"
        path.write_bytes(plistlib.dumps({"CFBundleIdentifier": ios_release.BUNDLE_ID, **info}, fmt=fmt))
        return path

    def test_the_archived_app_must_declare_it(self) -> None:
        for fmt in (plistlib.FMT_BINARY, plistlib.FMT_XML):
            with self.subTest(fmt=fmt):
                self.assertEqual(ios_release.check_archived_export_compliance(
                    self.archived({"ITSAppUsesNonExemptEncryption": False}, fmt)), "NO")
        with self.assertRaisesRegex(ios_release.ReleaseError, "does not declare ITSAppUsesNonExemptEncryption"):
            ios_release.check_archived_export_compliance(self.archived({}))

    def test_the_archived_value_must_be_a_boolean(self) -> None:
        for value in ("NO", "false", 0, ""):
            with self.subTest(value=value), self.assertRaisesRegex(ios_release.ReleaseError, "must be a boolean"):
                ios_release.check_archived_export_compliance(self.archived({"ITSAppUsesNonExemptEncryption": value}))

    def test_archived_yes_needs_the_compliance_code(self) -> None:
        for code in (None, "", "  "):
            info = {"ITSAppUsesNonExemptEncryption": True}
            if code is not None:
                info["ITSEncryptionExportComplianceCode"] = code
            with self.subTest(code=code), self.assertRaisesRegex(ios_release.ReleaseError,
                                                                 "without ITSEncryptionExportComplianceCode"):
                ios_release.check_archived_export_compliance(self.archived(info))
        self.assertEqual(ios_release.check_archived_export_compliance(self.archived(
            {"ITSAppUsesNonExemptEncryption": True, "ITSEncryptionExportComplianceCode": "ABC123"})), "YES")

    def test_an_unreadable_archived_plist_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, raw in (("garbage.plist", b"not a plist"), ("truncated.plist", b"<?xml version='1.0'?><plist><dict>"),
                              ("array.plist", plistlib.dumps(["ITSAppUsesNonExemptEncryption"]))):
                path = Path(tmp) / name
                path.write_bytes(raw)
                with self.subTest(name=name), self.assertRaises(ios_release.ReleaseError):
                    ios_release.check_archived_export_compliance(path)
            with self.assertRaisesRegex(ios_release.ReleaseError, "cannot read"):
                ios_release.check_archived_export_compliance(Path(tmp) / "missing.plist")

    def test_cli_checks_the_archived_app(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            ios_release.main(["export-compliance", "--info-plist",
                              str(self.archived({"ITSAppUsesNonExemptEncryption": False}))])
        self.assertIn("no non-exempt encryption", out.getvalue())
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            ios_release.main(["export-compliance", "--info-plist", str(self.archived({}))])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("export compliance", err.getvalue())


APP = asc_release.ASC_APP_ID
BUNDLE = asc_release.DEFAULT_BUNDLE_ID
STARTED = 1_800_000_000.0  # this run's upload start (epoch seconds)


def iso(epoch: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def build_row(
    build_id: str = "B1",
    *,
    number: str = "8",
    state: str = "VALID",
    expired: bool = False,
    uploaded: float | str | None = STARTED + 60,
    app: str = APP,
    pre: str = "P1",
) -> dict:
    attributes = {"version": number, "processingState": state, "expired": expired}
    if uploaded is not None:  # None: App Store Connect left the upload date out
        attributes["uploadedDate"] = uploaded if isinstance(uploaded, str) else iso(uploaded)
    return {
        "type": "builds",
        "id": build_id,
        "attributes": attributes,
        "relationships": {
            "preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": pre}},
            "app": {"data": {"type": "apps", "id": app}},
        },
    }


def pre_row(pre_id: str = "P1", version: str = VERSION, platform: str = "IOS") -> dict:
    return {"type": "preReleaseVersions", "id": pre_id, "attributes": {"version": version, "platform": platform}}


def asc_error(code: int) -> asc_cli.AscError:
    return asc_cli.AscError(("builds", "list"), code, "failed")


def flag(call: tuple[str, ...], name: str) -> str:
    return call[call.index(name) + 1]


def md5(raw: bytes) -> str:
    return hashlib.md5(raw).hexdigest()  # noqa: S324 - App Store Connect's screenshot checksum


MUTATIONS = {
    ("versions", "create"), ("versions", "update"), ("versions", "attach-build"), ("localizations", "update"),
    ("screenshots", "upload"), ("asset-library", "images", "upload"), ("localizations", "placements", "create"),
    ("localizations", "placements", "swap"), ("review", "submissions-create"), ("review", "items-add"),
    ("review", "submissions-submit"),
}
REVIEW_WRITES = {("review", "submissions-create"), ("review", "items-add"), ("review", "submissions-submit")}


def is_write(call: tuple[str, ...]) -> bool:
    return call[:2] in MUTATIONS or call[:3] in MUTATIONS


REPO_WHATS_NEW = "Faster chapter search."
LISTING_VALUES = {
    "description": "Handwritten notes.",
    "keywords": "jee,neet",
    "promotionalText": "Promo",
    "marketingUrl": "https://pcm-notes.apoorvdarshan.com/",
    "supportUrl": "https://pcm-notes.apoorvdarshan.com/#support",
}
APP_INFO_VALUES = {
    "name": "PCM Notes: JEE & NEET",
    "subtitle": "Handwritten Exam Revision",
    "privacyPolicyUrl": "https://pcm-notes.apoorvdarshan.com/privacy",
}
TIP = "com.apoorvdarshan.iitjee.tip."
TIERS = ("small", "medium", "large", "generous")
UPDATE_FIELDS = {flag_name: field for field, flag_name in asc_release.ASC_FLAGS.items()}
ITEM_RELATIONS = {"appStoreVersions": "appStoreVersion", "subscriptionVersions": "subscriptionVersion",
                  "inAppPurchaseVersions": "inAppPurchaseVersion", "appAssetLibraryImages": "appAssetLibraryImage"}


def repository_screenshot_sums() -> dict[str, list[str]]:
    """The checksums of the repository's screenshot sets: what App Store Connect holds today."""
    return {spec.display_type: [md5(path.read_bytes()) for path in asc_release.screenshot_files(spec)]
            for spec in asc_release.SCREENSHOT_SETS}


def item(item_id: str, relation: str, related: str) -> dict:
    """A review submission item that points at `related` through `relation`."""
    return {"id": item_id, "relationships": {relation: {"data": {"id": related}}}}


class FakeAsc:
    """A stateful App Store Connect behind `asc` (no network).

    The app has the live version 1.0 (V0) and, unless `has_version` is False,
    VERSION (V1) in `state`. By default App Store Connect already holds the
    repository's listing, What's New and screenshots (`differ` names what does
    not match), and every tip is approved (`ready_tips` are Ready to Submit,
    each with a draft version)."""

    def __init__(
        self,
        *,
        state: str = "PREPARE_FOR_SUBMISSION",
        attached: str | None = "OLD",
        revert_on_read: int | None = None,
        release_type: str | None = None,
        bundle: str = BUNDLE,
        record_id: str | None = None,
        has_version: bool = True,
        other_versions: list[tuple[str, str]] | None = None,
        creates_version: bool = True,
        listing: dict[str, str] | None = None,
        app_info: dict[str, str] | None = None,
        live_app_info: dict[str, str] | None = None,
        app_info_records: list[tuple[str, str]] | None = None,
        live_whats_new: str | None = "First edition.",
        version_locales: set[str] | None = None,
        screenshots: dict[str, list[str]] | None = None,
        differ: tuple[str, ...] = (),
        upload_state: str = "COMPLETE",
        uploads_land: bool = True,
        ready_tips: tuple[str, ...] = (),
        iaps: list[dict] | None = None,
        iap_versions: dict[str, list[tuple[int, str]]] | None = None,
        subscriptions: list[dict] | None = None,
        subscription_versions: dict[str, list[dict]] | None = None,
        drafts: tuple[str, ...] = ("SUB1",),
        draft_items: dict[str, list[dict]] | None = None,
        refuse_items: set[str] | None = None,
        builds: list[dict] | None = None,
        included: list[dict] | None = None,
        library: list[dict] | None = None,
        placements: list[dict] | None = None,
    ) -> None:
        self.state = state
        self.attached = attached
        self.revert_on_read = revert_on_read  # this read of the selection, and later ones, show OLD
        self.release_type = release_type
        self.bundle = bundle
        self.record_id = record_id
        # False: App Store Connect has no VERSION yet (`versions create` adds it,
        # unless `creates_version` is False and the new version never reads back).
        self.has_version = has_version
        self.creates_version = creates_version
        # The app's other IOS versions as (versionString, appStoreState); the first is V0.
        self.other_versions = [("1.0", "READY_FOR_SALE")] if other_versions is None else other_versions
        self.listing = {**LISTING_VALUES, "whatsNew": REPO_WHATS_NEW} if listing is None else dict(listing)
        self.app_info = dict(APP_INFO_VALUES if app_info is None else app_info)
        self.live_app_info = dict(APP_INFO_VALUES if live_app_info is None else live_app_info)
        self.app_info_records = app_info_records
        self.live_whats_new = live_whats_new
        self.version_locales = version_locales  # None: every requested locale exists
        if "listing" in differ:
            self.listing["promotionalText"] = "Last year's promo"
        if "app_info" in differ:
            self.app_info["subtitle"] = "Last year's subtitle"
        if "whats_new" in differ:
            self.listing["whatsNew"] = "Last year's notes"
        self.screenshots = {kind: list(sums) for kind, sums in (screenshots or {}).items()}
        if "screenshots" in differ:
            self.screenshots = {kind: ["stale"] for kind in self.screenshots}
        self.screenshot_states = {kind: "COMPLETE" for kind in self.screenshots}
        self.upload_state = upload_state
        self.uploads_land = uploads_land
        self.iaps = iaps if iaps is not None else [
            {"type": "inAppPurchases", "id": f"IAP-{tier}", "attributes": {
                "name": tier, "productId": TIP + tier,
                "state": "READY_TO_SUBMIT" if tier in ready_tips else "APPROVED"}}
            for tier in TIERS
        ]
        self.iap_versions = dict(iap_versions or {})
        self.subscriptions = subscriptions or []
        self.subscription_versions = subscription_versions
        self.drafts = list(drafts)
        self.items = {key: list(value) for key, value in (draft_items or {}).items()}
        self.refuse_items = refuse_items or set()
        self.builds = [build_row("B9")] if builds is None else builds
        self.included = [pre_row()] if included is None else included
        self.library = list(library or [])
        self.placements = list(placements or [])
        self.created_drafts = 0
        self.build_reads = 0
        self.uploaded_sets: list[str] = []
        self.uploaded_files: list[list[str]] = []
        self.calls: list[tuple[str, ...]] = []

    def writes(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if is_write(call)]

    def review_writes(self) -> list[tuple[str, ...]]:
        return [call for call in self.writes() if call[:2] in REVIEW_WRITES]

    def version_rows(self) -> list[dict]:
        rows = [{"id": "V0" if i == 0 else f"V0-{i}", "attributes": {"versionString": number, "appStoreState": state}}
                for i, (number, state) in enumerate(self.other_versions)]
        if self.has_version:
            attrs = {"versionString": VERSION, "appStoreState": self.state}
            if self.release_type:
                attrs["releaseType"] = self.release_type
            rows.append({"id": "V1", "attributes": attrs})
        return rows

    def locales(self, args: tuple[str, ...]) -> list[str]:
        """The locales a localization list answers with (filtered by --locale)."""
        known = sorted(self.version_locales) if self.version_locales is not None else None
        if "--locale" in args:
            wanted = flag(args, "--locale")
            return [wanted] if known is None or wanted in known else []
        return known if known is not None else ["en-US"]

    def run(self, *args: str, **_: object):
        self.calls.append(args)
        head = args[:3] if args[:3] in MUTATIONS or args[:2] in {
            ("localizations", "placements"), ("asset-library", "images"), ("subscriptions", "versions"),
            ("iap", "versions"), ("apps", "info")} else args[:2]
        if head == ("apps", "view"):
            return {"data": {"type": "apps", "id": self.record_id or flag(args, "--id"),
                             "attributes": {"bundleId": self.bundle}}}
        if head == ("versions", "list"):
            return {"data": self.version_rows()}
        if head == ("versions", "create"):
            if self.creates_version:
                self.has_version = True
                self.state = "PREPARE_FOR_SUBMISSION"
            return {"data": {"id": "V1"}}
        if head == ("versions", "view"):
            self.build_reads += 1
            build = self.attached
            if self.revert_on_read is not None and self.build_reads >= self.revert_on_read:
                build = "OLD"
            rel = {"build": {"data": {"type": "builds", "id": build} if build else None}}
            return {"data": {"id": flag(args, "--version-id"), "relationships": rel}}
        if head == ("versions", "attach-build"):
            self.attached = flag(args, "--build-id")
            return {"data": {}}
        if head == ("versions", "update"):
            self.release_type = flag(args, "--release-type")
            return {"data": {}}
        if head == ("builds", "list"):
            return {"data": self.builds, "included": self.included}
        if head == ("apps", "info", "list"):
            records = self.app_info_records or [("INFO-LIVE", "READY_FOR_DISTRIBUTION"), ("INFO-NEXT", self.state)]
            return {"data": [{"type": "appInfos", "id": info_id, "attributes": {"state": state}}
                             for info_id, state in records]}
        if head == ("localizations", "list"):
            if "--type" in args and flag(args, "--type") == "app-info":
                info_id = flag(args, "--app-info")
                values = self.live_app_info if info_id == "INFO-LIVE" else self.app_info
                return {"data": [{"type": "appInfoLocalizations", "id": f"LOC-{info_id}",
                                  "attributes": {"locale": locale, **values}} for locale in self.locales(args)]}
            version_id = flag(args, "--version")
            if version_id == "V1":
                values, loc_id = self.listing, "VL"
            elif version_id == "V0":
                values, loc_id = {**LISTING_VALUES, "whatsNew": self.live_whats_new}, "VL0"
            else:
                raise AssertionError(f"unexpected version {version_id}")
            return {"data": [{"type": "appStoreVersionLocalizations", "id": loc_id,
                              "attributes": {"locale": locale, **values}} for locale in self.locales(args)]}
        if head == ("localizations", "update"):
            target = self.app_info if "--type" in args and flag(args, "--type") == "app-info" else self.listing
            for name, field in UPDATE_FIELDS.items():
                if name in args:
                    target[field] = flag(args, name)
            return {"data": {}}
        if head == ("screenshots", "list"):
            return {"versionLocalizationId": "VL", "locale": flag(args, "--locale"), "sets": [
                {"set": {"type": "appScreenshotSets", "id": f"SET-{kind}", "attributes": {"screenshotDisplayType": kind}},
                 "screenshots": [{"type": "appScreenshots", "id": f"{kind}-{i}", "attributes": {
                     "fileName": f"image{i + 1}.png", "fileSize": 100 + i, "sourceFileChecksum": checksum,
                     "assetDeliveryState": {"state": self.screenshot_states.get(kind, "COMPLETE")}}}
                     for i, checksum in enumerate(sums)]}
                for kind, sums in self.screenshots.items()
            ]}
        if head == ("screenshots", "upload"):
            device = flag(args, "--device-type")
            # Like asc: every regular file in --path (not its subfolders), in name order,
            # and nothing is uploaded or replaced when one of them stores alpha.
            files = sorted(p for p in Path(flag(args, "--path")).iterdir() if p.is_file())
            if any(asc_cli.image_has_alpha(p) for p in files):
                raise asc_cli.AscError(args, 1, "image has an alpha channel or transparency; re-export it as RGB")
            self.uploaded_sets.append(device)
            self.uploaded_files.append([p.name for p in files])
            if self.uploads_land:
                # --replace leaves the set holding exactly those files.
                kind = "APP_" + device
                self.screenshots[kind] = [md5(p.read_bytes()) for p in files]
                self.screenshot_states[kind] = self.upload_state
            return {}
        if head == ("asset-library", "view"):
            return {"data": {"type": "appAssetLibraries", "id": "LIB1"}}
        if head == ("asset-library", "images", "list"):
            return {"data": self.library}
        if head == ("asset-library", "images", "upload"):
            path = Path(flag(args, "--file"))
            image_id = f"IMG{len(self.library) + 1}"
            self.library.append({"id": image_id, "attributes": {"fileName": path.name, "fileSize": path.stat().st_size,
                                                                "state": "PREPARE_FOR_SUBMISSION", "createdDate": "z"}})
            return {"imageId": image_id, "ready": True, "state": "PREPARE_FOR_SUBMISSION"}
        if head == ("localizations", "placements", "list"):
            wanted = flag(args, "--placement-type")
            return {"data": [p for p in self.placements if p["attributes"]["placementType"] == wanted]}
        if head in {("localizations", "placements", "create"), ("localizations", "placements", "swap")}:
            kind = flag(args, "--placement-type")
            self.placements = [p for p in self.placements if p["attributes"]["placementType"] != kind]
            self.placements.append({"id": "PL-new", "attributes": {"placementType": kind},
                                    "relationships": {"image": {"data": {"id": flag(args, "--image-id")}}}})
            return {"data": {}}
        if head == ("review", "submissions-list"):
            return {"data": [{"type": "reviewSubmissions", "id": draft} for draft in self.drafts]}
        if head == ("review", "submissions-create"):
            self.created_drafts += 1
            new = "SUB-NEW" if self.created_drafts == 1 else f"SUB-NEW{self.created_drafts}"
            self.drafts.append(new)
            return {"data": {"type": "reviewSubmissions", "id": new}}
        if head == ("review", "items-list"):
            return {"data": self.items.get(flag(args, "--submission"), [])}
        if head == ("review", "items-add"):
            item_type, item_id = flag(args, "--item-type"), flag(args, "--item-id")
            if item_id in self.refuse_items:
                raise asc_cli.AscError(args, 5, "conflict")
            relation = ITEM_RELATIONS[item_type]
            held = self.items.setdefault(flag(args, "--submission"), [])
            if any(asc_release.related_id(existing, relation) == item_id for existing in held):
                if "--if-exists" not in args or flag(args, "--if-exists") != "skip":
                    raise asc_cli.AscError(args, 5, "already on the submission")
                return {"data": {}}
            held.append(item(f"ITEM-{item_id}", relation, item_id))
            return {"data": {}}
        if head == ("review", "submissions-submit"):
            submitted = flag(args, "--id")
            self.drafts = [draft for draft in self.drafts if draft != submitted]  # no longer a draft
            return {"data": {}}
        if head == ("subscriptions", "list"):
            return {"data": self.subscriptions}
        if args[:3] == ("subscriptions", "versions", "list"):
            sub_id = flag(args, "--subscription-id")
            if self.subscription_versions is not None:
                return {"data": self.subscription_versions.get(sub_id, [])}
            return {"data": [{"id": f"{sub_id}-v2", "attributes": {"version": 2}},
                             {"id": f"{sub_id}-v1", "attributes": {"version": 1}}]}
        if head == ("iap", "list"):
            return {"data": self.iaps}
        if args[:3] == ("iap", "versions", "list"):
            iap_id = flag(args, "--iap-id")
            tier = iap_id.removeprefix("IAP-")
            rows = self.iap_versions.get(tier)
            if rows is None:
                ready = any(i["id"] == iap_id and i["attributes"]["state"] == "READY_TO_SUBMIT" for i in self.iaps)
                rows = [(1, "PREPARE_FOR_SUBMISSION" if ready else "APPROVED")]
            return {"data": [{"type": "inAppPurchaseVersions", "id": f"IAPV-{tier}-{number}",
                              "attributes": {"version": number, "state": state}} for number, state in rows]}
        raise AssertionError(f"unexpected asc call {args}")


class SequenceAsc:
    """Returns queued `asc builds list` payloads (or raises queued exceptions) per call."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, **_: object) -> dict:
        self.calls.append(args)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return item


class BuildNumberTests(unittest.TestCase):
    def test_next_build_number(self) -> None:
        self.assertEqual(ios_release.next_build_number(39, ["13", "35", "12"]), 39)
        self.assertEqual(ios_release.next_build_number(39, ["13", "39"]), 40)
        self.assertEqual(ios_release.next_build_number(1, []), 1)
        self.assertEqual(ios_release.next_build_number(5, ["7.1", "abc", "9"]), 10)
        self.assertEqual(ios_release.next_build_number(5, ["٩٩"]), 5)  # only ASCII digits count

    def test_pcm_notes_continues_after_the_xcode_cloud_builds(self) -> None:
        # Xcode Cloud uploaded 1.0 builds 1, 2, 3, 4 and 7; the project still says 1.
        self.assertEqual(ios_release.next_build_number(1, ["1", "2", "3", "4", "7"]), 8)

    def test_command_counts_every_uploaded_build_without_writing(self) -> None:
        fake = FakeAsc(builds=[build_row("B7", number="7"), build_row("B8", number="8", state="PROCESSING")])
        with tempfile.NamedTemporaryFile("r", suffix=".out") as out, mock.patch.object(
            ios_release, "asc_client", return_value=(asc_release, fake)
        ), mock.patch.object(ios_release, "single_project_value", return_value="1"), mock.patch.dict(
            os.environ, {"GITHUB_OUTPUT": out.name}
        ), redirect_stdout(io.StringIO()):
            ios_release.main(["build-number"])
            written = Path(out.name).read_text()
        self.assertIn("build_number=9", written)
        query = [call for call in fake.calls if call[:2] == ("builds", "list")][0]
        self.assertEqual((flag(query, "--app"), flag(query, "--processing-state")), (APP, "all"))
        self.assertIn("--paginate", query)
        self.assertEqual(fake.writes(), [])


class FakeClock:
    def __init__(self, start: float) -> None:
        self.time = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds


def wait(client: SequenceAsc, clock: FakeClock, *, minutes: int = 120, transient=None) -> dict:
    with redirect_stdout(io.StringIO()):
        return ios_release.wait_for_build(
            client,
            app_id=APP,
            marketing_version=VERSION,
            build_number="8",
            uploaded_after=STARTED,
            deadline=STARTED + minutes * 60,
            poll_seconds=30,
            now=clock.now,
            sleep=clock.sleep,
            is_transient=transient,
        )


class WaitForBuildTests(unittest.TestCase):
    def test_waits_through_missing_and_processing_then_returns_build(self) -> None:
        client = SequenceAsc(
            [
                {"data": []},
                {"data": [build_row(state="PROCESSING")], "included": [pre_row()]},
                {"data": [build_row()], "included": [pre_row()]},
            ]
        )
        clock = FakeClock(STARTED + 120)
        build = wait(client, clock)
        self.assertEqual(build["id"], "B1")
        self.assertEqual(clock.sleeps, [30, 30])
        query = client.calls[0]
        self.assertEqual(query[:2], ("builds", "list"))
        self.assertEqual(flag(query, "--app"), APP)
        self.assertEqual(flag(query, "--build-number"), "8")
        self.assertEqual(flag(query, "--processing-state"), "all")
        self.assertEqual(flag(query, "--include"), "preReleaseVersion,app")

    def test_times_out_without_using_another_build(self) -> None:
        client = SequenceAsc([{"data": [build_row(state="PROCESSING")], "included": [pre_row()]}])
        clock = FakeClock(STARTED)
        with self.assertRaisesRegex(ios_release.ReleaseError, "timed out"):
            wait(client, clock, minutes=10)
        self.assertLessEqual(clock.time, STARTED + 10 * 60 + 30)

    def test_ready_build_after_the_deadline_is_still_a_timeout(self) -> None:
        client = SequenceAsc([{"data": [build_row()], "included": [pre_row()]}])
        clock = FakeClock(STARTED + 121 * 60)  # job started after the 120-minute budget
        with self.assertRaisesRegex(ios_release.ReleaseError, "timed out"):
            wait(client, clock)
        self.assertEqual(client.calls, [], "must not poll once the budget is spent")

    def test_processing_failure_stops(self) -> None:
        for state in ("FAILED", "INVALID"):
            client = SequenceAsc([{"data": [build_row(state=state)], "included": [pre_row()]}])
            with self.subTest(state=state), self.assertRaisesRegex(ios_release.ReleaseError, state):
                wait(client, FakeClock(STARTED))

    def test_expired_build_is_rejected(self) -> None:
        client = SequenceAsc([{"data": [build_row(expired=True)], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "expired"):
            wait(client, FakeClock(STARTED))

    def test_ambiguous_match_stops(self) -> None:
        client = SequenceAsc(
            [{"data": [build_row("B1"), build_row("B2")], "included": [pre_row()]}]
        )
        with self.assertRaisesRegex(ios_release.ReleaseError, "ambiguous"):
            wait(client, FakeClock(STARTED))

    def test_older_upload_is_never_substituted(self) -> None:
        old = build_row(uploaded=STARTED - 3600)
        client = SequenceAsc([{"data": [old], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "older build"):
            wait(client, FakeClock(STARTED))

    def test_a_build_without_an_upload_date_is_never_used(self) -> None:
        # The "uploaded by this run" guard fails closed: a VALID build whose upload
        # date is missing is waited on, never returned, and times out.
        for missing in (None, ""):
            client = SequenceAsc([{"data": [build_row(uploaded=missing)], "included": [pre_row()]}])
            clock = FakeClock(STARTED)
            with self.subTest(uploaded=missing), self.assertRaisesRegex(ios_release.ReleaseError, "timed out"):
                wait(client, clock, minutes=10)
            self.assertGreater(len(client.calls), 1, "it keeps polling until the deadline")

    def test_a_build_gets_used_once_its_upload_date_appears(self) -> None:
        client = SequenceAsc([
            {"data": [build_row(uploaded=None)], "included": [pre_row()]},
            {"data": [build_row()], "included": [pre_row()]},
        ])
        self.assertEqual(wait(client, FakeClock(STARTED))["id"], "B1")

    def test_an_unreadable_upload_date_stops(self) -> None:
        client = SequenceAsc([{"data": [build_row(uploaded="yesterday")], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "unreadable upload date"):
            wait(client, FakeClock(STARTED))

    def test_failed_processing_stops_even_without_an_upload_date(self) -> None:
        client = SequenceAsc([{"data": [build_row(state="FAILED", uploaded=None)], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "FAILED"):
            wait(client, FakeClock(STARTED))

    def test_other_versions_and_platforms_are_ignored(self) -> None:
        other_version = build_row("BX", pre="P2")
        other_platform = build_row("BY", pre="P3")
        client = SequenceAsc(
            [
                {
                    "data": [other_version, other_platform],
                    "included": [pre_row("P2", "1.0"), pre_row("P3", VERSION, "MAC_OS")],
                },
                {"data": [other_version, build_row("B1")], "included": [pre_row("P2", "1.0"), pre_row()]},
            ]
        )
        self.assertEqual(wait(client, FakeClock(STARTED))["id"], "B1")

    def test_build_from_another_app_is_a_mismatch(self) -> None:
        client = SequenceAsc([{"data": [build_row(app="OTHER")], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "another app"):
            wait(client, FakeClock(STARTED))

    def test_transient_errors_are_retried_and_others_raise(self) -> None:
        client = SequenceAsc([asc_error(63), {"data": [build_row()], "included": [pre_row()]}])
        with redirect_stderr(io.StringIO()):
            build = wait(client, FakeClock(STARTED), transient=ios_release.is_transient_error)
        self.assertEqual(build["id"], "B1")
        fatal = SequenceAsc([asc_error(3)])
        with self.assertRaises(asc_cli.AscError):
            wait(fatal, FakeClock(STARTED), transient=ios_release.is_transient_error)


class TransientErrorTests(unittest.TestCase):
    def test_retry_rule_follows_asc_exit_codes(self) -> None:
        # HTTP 408, 429, any 5xx and a call that ran out of time are retried.
        for code in (18, 39, 60, 62, 63, 99, 124):
            with self.subTest(code=code):
                self.assertTrue(ios_release.is_transient_error(asc_error(code)))
        # Generic, usage, auth, not found, conflict, read-only refusal, other 4xx,
        # an unverified binary and a missing one.
        for code in (1, 2, 3, 4, 5, 6, 10, 32, 126, 127):
            with self.subTest(code=code):
                self.assertFalse(ios_release.is_transient_error(asc_error(code)))
        for exc in (ValueError("bad"), ios_release.ReleaseError("mismatch"), SystemExit(1), ConnectionResetError()):
            with self.subTest(exc=repr(exc)):
                self.assertFalse(ios_release.is_transient_error(exc))

    def test_network_drops_reported_as_exit_1_are_retried(self) -> None:
        for detail in ('Get "https://api.appstoreconnect.apple.com/v1/builds": read tcp: connection reset by peer',
                       "dial tcp: lookup api.appstoreconnect.apple.com: no such host",
                       "net/http: TLS handshake timeout", "unexpected EOF",
                       'Get "https://api.appstoreconnect.apple.com/v1/builds": EOF'):
            with self.subTest(detail=detail):
                self.assertTrue(ios_release.is_transient_error(asc_cli.AscError(("builds", "list"), 1, detail)))
        for detail in ("invalid character '<' looking for beginning of value", "build not found",
                       "geofence settings are invalid"):
            with self.subTest(detail=detail):
                self.assertFalse(ios_release.is_transient_error(asc_cli.AscError(("builds", "list"), 1, detail)))
        # Only exit 1 carries a network failure; the same words never make an auth error transient.
        self.assertFalse(ios_release.is_transient_error(asc_cli.AscError(("builds", "list"), 3, "connection reset")))

    def test_an_asc_failure_that_is_not_retried_ends_with_its_message(self) -> None:
        class Unauthorized:
            def run(self, *args: str, **_: object):
                raise asc_cli.AscError(args, 3, "401 NOT_AUTHORIZED")

        err = io.StringIO()
        with mock.patch.object(ios_release, "asc_client", return_value=(asc_release, Unauthorized())), \
                mock.patch.object(ios_release.time, "time", return_value=STARTED), redirect_stdout(io.StringIO()), \
                redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            ios_release.main(["wait-build", "--version", VERSION, "--build", "8", "--started-at", str(STARTED),
                              "--wait-minutes", "10", "--poll-seconds", "10"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("error: asc apps view --id failed (exit 3): 401 NOT_AUTHORIZED", err.getvalue())


class AscRunnerTests(unittest.TestCase):
    ENV = {
        "APP_STORE_CONNECT_API_KEY_ID": "TESTKEY0001",
        "APP_STORE_CONNECT_ISSUER_ID": "00000000-test-4000-8000-issuer000001",
        "APP_STORE_CONNECT_API_KEY_P8": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
        "ASC_PROFILE": "personal",
        "ASC_CONFIG_PATH": "/home/me/.asc/config.json",
    }

    def run_once(self, *, returncode: int = 0, stdout: str = '{"data": []}', stderr: str = "", read_only=False, env=None):
        seen: dict = {}

        def runner(argv, **kwargs):
            seen["argv"], seen["env"] = argv, kwargs["env"]
            return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

        with mock.patch.dict(os.environ, env or self.ENV, clear=True):
            result = asc_cli.Asc(binary="asc", read_only=read_only, runner=runner).run("apps", "view", "--id", APP)
        return result, seen

    def test_credentials_come_from_the_release_secrets_and_nothing_else(self) -> None:
        result, seen = self.run_once()
        self.assertEqual(result, {"data": []})
        self.assertEqual(seen["argv"], ["asc", "apps", "view", "--id", APP, "--output", "json"])
        env = seen["env"]
        self.assertEqual(env["ASC_KEY_ID"], "TESTKEY0001")
        self.assertEqual(env["ASC_ISSUER_ID"], self.ENV["APP_STORE_CONNECT_ISSUER_ID"])
        self.assertIn("\nabc\n", env["ASC_PRIVATE_KEY"])
        for name in ("ASC_PROFILE", "ASC_CONFIG_PATH"):
            self.assertNotIn(name, env)
        for name, value in (("ASC_BYPASS_KEYCHAIN", "1"), ("ASC_TELEMETRY_DISABLED", "1"), ("DO_NOT_TRACK", "1"),
                            ("ASC_SKILLS_AUTO_CHECK", "0"), ("ASC_STRICT_AUTH", "1"), ("ASC_MAX_RETRIES", "5")):
            self.assertEqual(env[name], value, name)
        self.assertNotIn("ASC_READ_ONLY", env)

    def test_read_only_from_the_caller_or_the_job(self) -> None:
        _, seen = self.run_once(read_only=True)
        self.assertEqual(seen["env"]["ASC_READ_ONLY"], "1")
        _, seen = self.run_once(env={**self.ENV, "ASC_READ_ONLY": "1"})
        self.assertEqual(seen["env"]["ASC_READ_ONLY"], "1")
        # An empty value (release runs of the workflow) is not read-only.
        _, seen = self.run_once(env={**self.ENV, "ASC_READ_ONLY": ""})
        self.assertNotIn("ASC_READ_ONLY", seen["env"])

    def test_failures_keep_the_exit_code_and_hide_the_ids(self) -> None:
        stderr = f"401 for key {self.ENV['APP_STORE_CONNECT_API_KEY_ID']} issuer {self.ENV['APP_STORE_CONNECT_ISSUER_ID']}"
        with self.assertRaises(asc_cli.AscError) as caught:
            self.run_once(returncode=3, stderr=stderr)
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertNotIn("TESTKEY0001", str(caught.exception))
        self.assertNotIn("issuer000001", str(caught.exception))
        with self.assertRaises(asc_cli.AscError):
            self.run_once(stdout="not json")

    def test_a_read_only_refusal_stops_the_release_with_its_exit_code(self) -> None:
        with self.assertRaises(asc_cli.AscError) as caught:
            self.run_once(returncode=6, stderr="read-only mode refused a mutating request", read_only=True)
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertFalse(ios_release.is_transient_error(caught.exception))

    def test_install_accepts_only_the_pinned_binary(self) -> None:
        good = b"pinned-binary"
        with mock.patch.dict(asc_cli.ASC_SHA256, {"linux_amd64": hashlib.sha256(good).hexdigest()}):
            with tempfile.TemporaryDirectory() as tmp:
                opener = lambda url, timeout: io.BytesIO(good)  # noqa: E731
                target = quiet(asc_cli.install, Path(tmp), key="linux_amd64", opener=opener)
                self.assertEqual(target.read_bytes(), good)
                self.assertTrue(os.access(target, os.X_OK))
                bad = lambda url, timeout: io.BytesIO(b"tampered")  # noqa: E731
                with self.assertRaises(SystemExit):
                    quiet(asc_cli.install, Path(tmp) / "other", key="linux_amd64", opener=bad)
                self.assertFalse((Path(tmp) / "other" / "asc").exists())

    def test_the_pin_is_one_exact_release_with_both_ci_platforms(self) -> None:
        self.assertRegex(asc_cli.ASC_VERSION, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertEqual(set(asc_cli.ASC_SHA256), {"linux_amd64", "macOS_arm64"})
        for digest in asc_cli.ASC_SHA256.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(asc_cli.platform_key("Linux", "x86_64"), "linux_amd64")
        self.assertEqual(asc_cli.platform_key("Darwin", "arm64"), "macOS_arm64")
        with self.assertRaises(SystemExit):
            asc_cli.platform_key("Windows", "x86_64")

    def test_only_the_pinned_binary_receives_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "asc"
            binary.write_bytes(b"pinned-binary")
            pinned = {asc_cli.platform_key(): hashlib.sha256(b"pinned-binary").hexdigest()}
            with mock.patch.dict(asc_cli.ASC_SHA256, pinned), \
                    mock.patch.object(asc_cli.shutil, "which", return_value=str(binary)):
                self.assertEqual(asc_cli.verified_binary("asc"), str(binary))
                binary.write_bytes(b"something else")
                with self.assertRaises(asc_cli.AscError) as caught:
                    asc_cli.verified_binary("asc")
                self.assertEqual(caught.exception.exit_code, 126)
                # A real run checks the binary before anything is executed.
                with mock.patch.object(asc_cli.subprocess, "run") as run, self.assertRaises(asc_cli.AscError):
                    asc_cli.Asc().run("apps", "view", "--id", APP)
                run.assert_not_called()
            with mock.patch.object(asc_cli.shutil, "which", return_value=None), self.assertRaises(asc_cli.AscError):
                asc_cli.verified_binary("asc")
        # No environment variable can point the release at another executable.
        with mock.patch.dict(os.environ, {"ASC_BIN": "/tmp/evil"}):
            self.assertEqual(asc_cli.Asc().binary, "asc")

    def test_alpha_is_detected_the_way_asc_refuses_it(self) -> None:
        cases = {"rgb": (png_bytes(16, 16), False), "rgba": (png_bytes(16, 16, color_type=6), True),
                 "gray-alpha": (png_bytes(16, 16, color_type=4), True),
                 "rgb-trns": (png_bytes(16, 16, trns=True), True), "jpeg": (b"\xff\xd8\xff\xe0rest", False)}
        with tempfile.TemporaryDirectory() as tmp:
            for name, (content, expected) in cases.items():
                path = Path(tmp) / name
                path.write_bytes(content)
                with self.subTest(name=name):
                    self.assertEqual(asc_cli.image_has_alpha(path), expected)

    def test_a_leading_at_sign_is_escaped(self) -> None:
        self.assertEqual(asc_release.asc_value("@apoorvdarshan on X"), "@@apoorvdarshan on X")
        self.assertEqual(asc_release.asc_value("Handwritten notes"), "Handwritten notes")

    def test_escaped_newlines_in_the_secret_are_restored(self) -> None:
        env = {
            "APP_STORE_CONNECT_API_KEY_ID": "KEY123",
            "APP_STORE_CONNECT_ISSUER_ID": "issuer",
            "APP_STORE_CONNECT_API_KEY_P8": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            _, _, key = asc_release.load_credentials()
        self.assertEqual(key.count("\n"), 2)

    def test_missing_credentials_fail_before_asc_runs(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            quiet(asc_release.load_credentials)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(asc_cli, "Asc") as runner, \
                self.assertRaises(SystemExit):
            quiet(asc_release.asc_from_environment)
        runner.assert_not_called()

    def test_checks_and_the_export_run_asc_read_only(self) -> None:
        env = {"APP_STORE_CONNECT_API_KEY_ID": "K", "APP_STORE_CONNECT_ISSUER_ID": "I", "APP_STORE_CONNECT_API_KEY_P8": "P"}
        with mock.patch.dict(os.environ, env, clear=True):
            _, client = ios_release.asc_client()
            self.assertTrue(client.read_only)
            self.assertFalse(asc_release.asc_from_environment().read_only)


class FindAppTests(unittest.TestCase):
    def test_the_pinned_app_must_carry_the_bundle_id(self) -> None:
        fake = FakeAsc()
        self.assertEqual(quiet(asc_release.find_app, fake, BUNDLE), APP)
        self.assertEqual(fake.calls, [("apps", "view", "--id", APP)])
        for bundle in ("com.example.other", "com.apoorvdarshan.iitjeex", "com.apoorvdarshan.iitjee.extra"):
            err = io.StringIO()
            with self.subTest(bundle=bundle), redirect_stderr(err), self.assertRaises(SystemExit):
                asc_release.find_app(FakeAsc(bundle=bundle), BUNDLE)
            self.assertIn(f"expected PCM Notes ({BUNDLE})", err.getvalue())

    def test_another_record_stops(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            asc_release.find_app(FakeAsc(record_id="123"), BUNDLE)
        self.assertIn(f"expected PCM Notes ({APP})", err.getvalue())

    def test_asc_failures_stop_with_a_readable_error(self) -> None:
        class Broken:
            def run(self, *args: str, **_: object):
                raise asc_cli.AscError(args, 3, "unauthorized")

        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            asc_release.find_app(Broken(), BUNDLE)
        self.assertIn("exit 3", err.getvalue())


class BuildSelectionTests(unittest.TestCase):
    def test_selects_and_reads_back_this_runs_build(self) -> None:
        fake = FakeAsc()
        quiet(asc_release.select_build, fake, "V1", "B9", VERSION, "8")
        self.assertEqual(fake.attached, "B9")
        names = [c[:2] for c in fake.calls]
        self.assertEqual(names, [("builds", "list"), ("versions", "view"), ("versions", "attach-build"),
                                 ("versions", "view")])
        attach = fake.writes()[0]
        self.assertEqual((flag(attach, "--version-id"), flag(attach, "--build-id")), ("V1", "B9"))
        verify = fake.calls[0]
        self.assertEqual((flag(verify, "--app"), flag(verify, "--build-number"), flag(verify, "--version"),
                          flag(verify, "--platform")), (APP, "8", VERSION, "IOS"))

    def test_selection_that_does_not_stick_fails(self) -> None:
        fake = FakeAsc(revert_on_read=2)  # the read after selecting still shows the old build
        with self.assertRaises(SystemExit):
            quiet(asc_release.select_build, fake, "V1", "B9", VERSION, "8")

    def test_unprocessed_expired_or_wrong_build_is_refused(self) -> None:
        cases = {
            "processing": ([build_row("B9", state="PROCESSING")], [pre_row()]),
            "expired": ([build_row("B9", expired=True)], [pre_row()]),
            "number": ([build_row("B9", number="9")], [pre_row()]),
            "version": ([build_row("B9")], [pre_row(version="1.0")]),
            "platform": ([build_row("B9")], [pre_row(platform="TV_OS")]),
            "missing": ([build_row("B8")], [pre_row()]),
        }
        for label, (builds, included) in cases.items():
            fake = FakeAsc(attached=None, builds=builds, included=included)
            with self.subTest(label), self.assertRaises(SystemExit):
                quiet(asc_release.select_build, fake, "V1", "B9", VERSION, "8")
            self.assertEqual(fake.writes(), [], label)

    def test_an_already_selected_build_is_not_selected_again(self) -> None:
        # A rerun finds its own build selected (for example in a draft review
        # submission) and must not write the selection again.
        fake = FakeAsc(attached="B9")
        quiet(asc_release.select_build, fake, "V1", "B9", VERSION, "8")
        self.assertEqual(fake.writes(), [])

    def test_release_type(self) -> None:
        for public, expected in ((True, "AFTER_APPROVAL"), (False, "MANUAL")):
            fake = FakeAsc()
            quiet(asc_release.set_release_type, fake, "V1", public_release=public)
            update = fake.calls[-1]
            self.assertEqual(update[:2], ("versions", "update"))
            self.assertEqual((flag(update, "--version-id"), flag(update, "--release-type")), ("V1", expected))

    def test_release_type_already_set_is_left_alone(self) -> None:
        fake = FakeAsc()
        quiet(asc_release.set_release_type, fake, "V1", public_release=False, current="MANUAL")
        self.assertEqual(fake.calls, [])


class PreflightTests(unittest.TestCase):
    def problems(self, state: str | None, *, text: bool = True, screenshots: bool = False,
                 submit: bool = False, creative: bool = False, missing_locales: tuple[str, ...] = ()) -> list[str]:
        version = None if state is None else {"id": "V1", "attributes": {"appStoreState": state}}
        return ios_release.preflight_problems(
            version,
            version_string=VERSION,
            text=text,
            screenshots=screenshots,
            submit=submit,
            text_states=asc_release.TEXT_EDITABLE_VERSION_STATES,
            screenshot_states=asc_release.SCREENSHOT_EDITABLE_VERSION_STATES,
            submittable_states=asc_release.SUBMITTABLE_VERSION_STATES,
            missing_locales=missing_locales,
            creative=creative,
        )

    def test_missing_version_fails_before_anything_is_built(self) -> None:
        problems = " ".join(self.problems(None))
        self.assertRegex(problems, f"create the {VERSION} App Store version")
        self.assertIn("IOS_CREATE_VERSION", problems)

    def test_live_or_in_review_version_cannot_take_listing_edits(self) -> None:
        for state in ("READY_FOR_SALE", "WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE"):
            with self.subTest(state=state):
                self.assertTrue(self.problems(state))
                self.assertTrue(self.problems(state, text=False, screenshots=True))
                self.assertTrue(self.problems(state, text=False, creative=True))

    def test_prepared_version_passes(self) -> None:
        for state in ("PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED"):
            with self.subTest(state=state):
                self.assertEqual(self.problems(state, screenshots=True, creative=True, submit=True), [])

    def test_a_version_in_a_draft_takes_text_but_not_screenshots_or_images(self) -> None:
        # READY_FOR_REVIEW (left in a draft review submission by a failed submit):
        # Apple still accepts text edits and the submit, not new screenshots or
        # header and search results images.
        self.assertEqual(self.problems("READY_FOR_REVIEW", submit=True), [])
        self.assertRegex(" ".join(self.problems("READY_FOR_REVIEW", text=False, screenshots=True)),
                         "screenshots cannot be replaced")
        self.assertRegex(" ".join(self.problems("READY_FOR_REVIEW", text=False, creative=True)),
                         "header and search results images cannot be changed")

    def test_a_new_run_cannot_submit_a_version_already_waiting_for_review(self) -> None:
        # A fresh run always uploads a new build, which can never replace the build
        # already in review, so it stops before building instead of after uploading.
        problems = self.problems("WAITING_FOR_REVIEW", text=False, submit=True)
        self.assertEqual(len(problems), 1)
        self.assertIn("Re-run failed jobs", problems[0])
        self.assertTrue(self.problems("IN_REVIEW", text=False, submit=True))

    def test_missing_localizations_are_reported(self) -> None:
        problems = self.problems("PREPARE_FOR_SUBMISSION", missing_locales=("fr-FR",))
        self.assertRegex(" ".join(problems), "no fr-FR localization")

    def run_preflight(self, fake: FakeAsc, *flags: str) -> tuple[int, str]:
        out = io.StringIO()
        code = 0
        with mock.patch.object(ios_release, "asc_client", return_value=(asc_release, fake)), \
                redirect_stdout(out), redirect_stderr(out):
            try:
                ios_release.main(["preflight", "--version", VERSION, *flags])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        self.assertEqual(fake.writes(), [], "the preflight never writes")
        return code, out.getvalue()

    def test_cli_is_read_only_and_reports_problems(self) -> None:
        code, output = self.run_preflight(FakeAsc(state="READY_FOR_SALE", version_locales={"en-US"}), "--listing")
        self.assertEqual(code, 1)
        self.assertIn("cannot be edited", output)

    def test_cli_checks_every_repository_locale(self) -> None:
        fake = FakeAsc(version_locales={"en-US"}, screenshots=repository_screenshot_sums())
        code, output = self.run_preflight(fake, "--whats-new", "--screenshots", "--submit")
        self.assertEqual(code, 0, output)
        self.assertIn("can take this release", output)
        lookups = [c for c in fake.calls if c[:2] == ("localizations", "list")]
        self.assertEqual([(flag(c, "--version"), flag(c, "--locale")) for c in lookups], [("V1", "en-US")])
        code, output = self.run_preflight(FakeAsc(version_locales=set()), "--listing")
        self.assertEqual(code, 1)
        self.assertIn("no en-US localization", output)

    def test_cli_creative_needs_the_primary_locale_and_an_editable_version(self) -> None:
        code, output = self.run_preflight(FakeAsc(version_locales={"en-US"}), "--creative")
        self.assertEqual(code, 0, output)
        code, output = self.run_preflight(FakeAsc(version_locales=set()), "--creative")
        self.assertEqual(code, 1)
        self.assertIn("no en-US localization", output)
        code, output = self.run_preflight(FakeAsc(state="READY_FOR_REVIEW"), "--creative")
        self.assertEqual(code, 1)
        self.assertIn("header and search results images cannot be changed", output)

    def test_cli_refuses_screenshots_with_alpha_that_would_have_to_be_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sets, sums = [], {}
            for spec in asc_release.SCREENSHOT_SETS:
                directory = root / spec.display_type
                directory.mkdir()
                width, height = sorted(spec.sizes)[0]
                raw = png_bytes(width, height, color_type=6)
                (directory / "01.png").write_bytes(raw)
                sets.append(spec._replace(directory=str(directory)))
                sums[spec.display_type] = [md5(raw)]
            with mock.patch.object(asc_release, "SCREENSHOT_SETS", tuple(sets)):
                # App Store Connect holds both sets already: nothing to upload, nothing to refuse.
                code, output = self.run_preflight(FakeAsc(screenshots=sums), "--screenshots")
                self.assertEqual(code, 0, output)
                # The iPad set differs, so it would have to be uploaded, and asc would refuse it.
                code, output = self.run_preflight(
                    FakeAsc(screenshots={**sums, "APP_IPAD_PRO_3GEN_129": ["older"]}), "--screenshots")
                self.assertEqual(code, 1)
                self.assertIn("APP_IPAD_PRO_3GEN_129 screenshots 01.png have an alpha channel", output)
                self.assertNotIn("APP_IPHONE_67 screenshots", output)
                # Without the screenshot switch, the screenshots are not read.
                fake = FakeAsc(screenshots={})
                code, output = self.run_preflight(fake, "--submit")
                self.assertEqual(code, 0, output)
                self.assertFalse([c for c in fake.calls if c[:2] == ("screenshots", "list")])

    def test_cli_submit_only_skips_the_locale_lookup(self) -> None:
        fake = FakeAsc(version_locales=set())
        code, output = self.run_preflight(fake, "--submit")
        self.assertEqual(code, 0, output)
        self.assertFalse([c for c in fake.calls if c[:2] == ("localizations", "list")])

    def test_cli_reads_the_pinned_app(self) -> None:
        code, output = self.run_preflight(FakeAsc(bundle="com.example.other"), "--submit")
        self.assertEqual(code, 1)
        self.assertIn("expected PCM Notes", output)


def write_listing(root: Path, locale: str = "en-US", **overrides: str) -> Path:
    loc = root / "ios" / locale
    loc.mkdir(parents=True, exist_ok=True)
    files = {
        "name.txt": "PCM Notes: JEE & NEET",
        "subtitle.txt": "Handwritten Exam Revision",
        "privacy_url.txt": "https://pcm-notes.apoorvdarshan.com/privacy",
        "description.txt": "Handwritten notes.",
        "keywords.txt": "jee,neet",
        "promotional_text.txt": "Promo",
        "marketing_url.txt": "https://pcm-notes.apoorvdarshan.com/",
        "support_url.txt": "https://pcm-notes.apoorvdarshan.com/#support",
    }
    files.update(overrides)
    for name, text in files.items():
        (loc / name).write_text(text + "\n")
    return loc


def run_asc_dry_run(env: dict[str, str], *args: str) -> tuple[int, str]:
    """asc_release.py --dry-run with these switches; returns (exit code, output)."""
    out = io.StringIO()
    full_env = {k: v for k, v in os.environ.items() if k not in FLAG_NAMES}
    full_env.update(env)
    with mock.patch.dict(os.environ, full_env, clear=True), mock.patch.object(
        sys, "argv", ["asc_release.py", "--dry-run", *args]
    ), mock.patch.object(asc_release, "asc_from_environment", side_effect=AssertionError("a dry run calls asc")), \
            redirect_stdout(out), redirect_stderr(out):
        try:
            asc_release.main()
        except SystemExit as exc:
            return (exc.code if isinstance(exc.code, int) else 1), out.getvalue()
    return 0, out.getvalue()


class AscCliTests(unittest.TestCase):
    def run_cli(self, env: dict[str, str], *args: str) -> tuple[int, str]:
        return run_asc_dry_run(env, *args)

    def test_everything_off_is_a_no_op(self) -> None:
        code, output = self.run_cli({})
        self.assertEqual(code, 0)
        self.assertIn("skipped", output)

    def test_submit_without_this_runs_build_fails(self) -> None:
        code, output = self.run_cli({"SUBMIT_IOS_REVIEW": "true"})
        self.assertEqual(code, 1)
        self.assertIn("--build-id", output)

    def test_public_release_without_submit_fails(self) -> None:
        code, _ = self.run_cli({"UPLOAD_LISTING": "true", "PUBLIC_RELEASE": "true"})
        self.assertEqual(code, 1)

    def test_only_the_pcm_notes_bundle_is_supported(self) -> None:
        code, output = self.run_cli({"UPLOAD_LISTING": "true"}, "--bundle-id", "com.example.other")
        self.assertEqual(code, 1)
        self.assertIn("PCM Notes iOS bundle", output)

    def test_whats_new_requires_text_for_every_locale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_listing(Path(tmp))
            fr = write_listing(Path(tmp), "fr-FR")
            code, output = self.run_cli({"UPLOAD_WHATS_NEW": "true"}, "--metadata-dir", tmp)
            self.assertEqual(code, 1)
            self.assertIn("whats_new.txt", output)
            (Path(tmp) / "ios" / "en-US" / "whats_new.txt").write_text("Fixes\n")
            code, output = self.run_cli({"UPLOAD_WHATS_NEW": "true"}, "--metadata-dir", tmp)
            self.assertEqual(code, 1)
            self.assertIn("fr-FR", output)
            (fr / "whats_new.txt").write_text("Corrections\n")
            code, output = self.run_cli({"UPLOAD_WHATS_NEW": "true"}, "--metadata-dir", tmp)
            self.assertEqual(code, 0, output)

    def test_whats_new_off_never_blocks_on_its_text(self) -> None:
        # With What's New upload off, a missing or overlong whats_new.txt must not
        # stop a release; with it on, the same text fails before anything is built.
        for whats_new in (None, "w" * 4001):
            with self.subTest(whats_new=whats_new and len(whats_new)), tempfile.TemporaryDirectory() as tmp:
                loc = write_listing(Path(tmp))
                if whats_new is not None:
                    (loc / "whats_new.txt").write_text(whats_new + "\n")
                code, output = self.run_cli({"UPLOAD_LISTING": "true"}, "--metadata-dir", tmp)
                self.assertEqual(code, 0, output)
                self.assertIn("whats_new=False", output)
                code, output = self.run_cli(
                    {"UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true"}, "--metadata-dir", tmp
                )
                self.assertEqual(code, 1, output)
                self.assertIn("whats_new.txt", output)

    def test_listing_limits_and_required_fields_are_enforced(self) -> None:
        cases = {
            "name": {"name.txt": "N" * 31},
            "subtitle": {"subtitle.txt": "S" * 31},
            "keywords": {"keywords.txt": "k" * 101},
            "promotional text": {"promotional_text.txt": "p" * 171},
            "description": {"description.txt": "d" * 4001},
            "http url": {"support_url.txt": "http://pcm-notes.apoorvdarshan.com"},
            "missing description": {"description.txt": ""},
        }
        for label, override in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                write_listing(Path(tmp), **override)
                code, _ = self.run_cli({"UPLOAD_LISTING": "true"}, "--metadata-dir", tmp)
                self.assertEqual(code, 1, label)

    def test_primary_locale_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_listing(Path(tmp), "fr-FR")
            code, output = self.run_cli({"UPLOAD_LISTING": "true"}, "--metadata-dir", tmp)
        self.assertEqual(code, 1)
        self.assertIn("en-US", output)

    def test_creative_assets_need_one_image_per_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, output = self.run_cli({"UPLOAD_CREATIVE_ASSETS": "true"}, "--creative-dir", tmp)
            self.assertEqual(code, 1)
            self.assertIn("no header or search results image", output)
            (Path(tmp) / "header").mkdir()
            (Path(tmp) / "header" / "a.png").write_bytes(b"x")
            (Path(tmp) / "header" / "b.png").write_bytes(b"y")
            code, output = self.run_cli({"UPLOAD_CREATIVE_ASSETS": "true"}, "--creative-dir", tmp)
            self.assertEqual(code, 1)
            self.assertIn("takes one image", output)
            (Path(tmp) / "header" / "b.png").unlink()
            (Path(tmp) / "header" / "notes.txt").write_text("not an image")
            code, output = self.run_cli({"UPLOAD_CREATIVE_ASSETS": "true"}, "--creative-dir", tmp)
            self.assertEqual(code, 0, output)
            self.assertIn("creative=True", output)

    def test_screenshots_with_alpha_get_a_note_in_the_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sets = []
            for spec in asc_release.SCREENSHOT_SETS:
                directory = root / spec.display_type
                directory.mkdir()
                width, height = sorted(spec.sizes)[0]
                (directory / "01.png").write_bytes(png_bytes(width, height, color_type=6))
                sets.append(spec._replace(directory=str(directory)))
            with mock.patch.object(asc_release, "SCREENSHOT_SETS", tuple(sets)):
                code, output = self.run_cli({"UPLOAD_SCREENSHOTS": "true"})
        self.assertEqual(code, 0, output)
        self.assertIn("note: the APP_IPHONE_67 screenshots 01.png have an alpha channel", output)

    def test_creative_images_with_alpha_are_refused_before_any_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "header").mkdir()
            (Path(tmp) / "header" / "header.png").write_bytes(png_bytes(3840, 1646, color_type=6))
            code, output = self.run_cli({"UPLOAD_CREATIVE_ASSETS": "true"}, "--creative-dir", tmp)
        self.assertEqual(code, 1)
        self.assertIn("header.png has an alpha channel", output)

    def test_creative_switch_off_never_reads_the_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_listing(Path(tmp))
            code, output = self.run_cli({"UPLOAD_LISTING": "true"}, "--metadata-dir", tmp,
                                        "--creative-dir", str(Path(tmp) / "missing"))
        self.assertEqual(code, 0, output)
        self.assertIn("creative=False", output)

    def test_listing_excludes_whats_new_unless_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loc = Path(tmp) / "ios" / "en-US"
            loc.mkdir(parents=True)
            (loc / "description.txt").write_text("Desc")
            (loc / "whats_new.txt").write_text("New")
            for whats_new, expected in ((False, {"--description"}), (True, {"--description", "--whats-new"})):
                fake = FakeAsc(listing={})
                with mock.patch.object(asc_release, "upload_app_info"), \
                        mock.patch.object(asc_release, "check_whats_new_is_new"):
                    quiet(
                        asc_release.upload_listing,
                        fake,
                        app_id=APP,
                        version_id="V1",
                        locale="en-US",
                        metadata_dir=Path(tmp),
                        include_listing=True,
                        include_whats_new=whats_new,
                    )
                update = [c for c in fake.writes() if c[:2] == ("localizations", "update")][0]
                self.assertEqual({a for a in update if a.startswith("--")} - {"--id"}, expected)
                self.assertEqual(flag(update, "--id"), "VL")


class ListingSyncTests(unittest.TestCase):
    """Version localization: only differences are sent; a frozen version only compares."""

    def sync(self, current: dict[str, str], *, frozen_state: str | None = None, fake: FakeAsc | None = None,
             **overrides: str) -> FakeAsc:
        fake = fake or FakeAsc(listing=current)
        with tempfile.TemporaryDirectory() as tmp:
            write_listing(Path(tmp), **overrides)
            with mock.patch.object(asc_release, "upload_app_info") as app_info:
                try:
                    quiet(asc_release.upload_listing, fake, app_id=APP, version_id="V1", locale="en-US",
                          metadata_dir=Path(tmp), include_listing=True, frozen_state=frozen_state)
                finally:
                    app_info.assert_called_once()
                    self.assertEqual(app_info.call_args.kwargs["frozen_state"], frozen_state)
        return fake

    def test_only_changed_fields_are_sent(self) -> None:
        fake = self.sync({**LISTING_VALUES, "keywords": "jee"})
        self.assertEqual(len(fake.writes()), 1)
        update = fake.writes()[0]
        self.assertEqual(update, ("localizations", "update", "--id", "VL", "--keywords", "jee,neet"))

    def test_matching_text_is_not_sent_again(self) -> None:
        stored = {**LISTING_VALUES, "description": "Handwritten notes.\r\n"}
        self.assertEqual(self.sync(stored).writes(), [])

    def test_a_leading_at_sign_reaches_asc_escaped(self) -> None:
        fake = self.sync(LISTING_VALUES, **{"promotional_text.txt": "@pcmnotes on X"})
        self.assertEqual(flag(fake.writes()[0], "--promotional-text"), "@@pcmnotes on X")

    def test_a_frozen_version_passes_when_it_already_matches(self) -> None:
        self.assertEqual(self.sync(LISTING_VALUES, frozen_state="WAITING_FOR_REVIEW").writes(), [])

    def test_a_frozen_version_stops_on_a_difference_without_writing(self) -> None:
        fake = FakeAsc(listing={**LISTING_VALUES, "promotionalText": "Old"})
        with self.assertRaises(SystemExit):
            self.sync({}, frozen_state="WAITING_FOR_REVIEW", fake=fake)
        self.assertEqual(fake.writes(), [])

    def test_a_missing_localization_stops_with_the_fix(self) -> None:
        fake = FakeAsc(version_locales=set())
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(asc_release, "upload_app_info"), \
                redirect_stdout(io.StringIO()), redirect_stderr(err), self.assertRaises(SystemExit):
            write_listing(Path(tmp))
            asc_release.upload_listing(fake, app_id=APP, version_id="V1", locale="en-US", metadata_dir=Path(tmp))
        self.assertIn("add that language in App Store Connect first", err.getvalue())
        self.assertEqual(fake.writes(), [])


# The repository's own listing and screenshots. The Store release checks workflow
# runs these on every pull request and main push that touches them; the iOS
# Release workflow sets STORE_TESTS_SKIP_REPOSITORY_FILES=1 and checks the files
# only when their upload switch is on, so unused store files never block a release.
@unittest.skipIf(os.environ.get("STORE_TESTS_SKIP_REPOSITORY_FILES") == "1",
                 "repository listing and screenshots are checked by the release only when their switch is on")
class RepositoryStoreFileTests(unittest.TestCase):
    def test_the_repository_listing_and_screenshots_are_upload_ready(self) -> None:
        env = {"UPLOAD_LISTING": "true", "UPLOAD_SCREENSHOTS": "true", "SUBMIT_IOS_REVIEW": "true"}
        code, output = run_asc_dry_run(env, "--build-id", "B9", "--build-number", "8")
        self.assertEqual(code, 0, output)
        self.assertIn("locales=en-US", output)

    def test_exported_listing_matches_the_app(self) -> None:
        loc = ROOT / "store" / "metadata" / "ios" / "en-US"
        self.assertEqual((loc / "name.txt").read_text().strip(), "PCM Notes: JEE & NEET")
        self.assertIn("91 chapters", (loc / "promotional_text.txt").read_text())
        self.assertEqual(asc_release.listing_locales(ROOT / "store" / "metadata"), ["en-US"])

    def test_repository_screenshots_have_app_store_sizes(self) -> None:
        quiet(asc_release.validate_screenshots)
        counts = {spec.display_type: len(asc_release.screenshot_files(spec)) for spec in asc_release.SCREENSHOT_SETS}
        self.assertEqual(counts, {"APP_IPHONE_67": 5, "APP_IPAD_PRO_3GEN_129": 5})


class AppInfoTests(unittest.TestCase):
    def upload(self, fake: FakeAsc, frozen_state: str | None = None, **overrides: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_listing(Path(tmp), **overrides)
            quiet(asc_release.upload_app_info, fake, app_id=APP, locale="en-US", metadata_dir=Path(tmp),
                  frozen_state=frozen_state)

    @staticmethod
    def fake(*, editable: bool = True, next_state: str = "PREPARE_FOR_SUBMISSION", next_first: bool = False,
             current: dict[str, str] | None = None, live: dict[str, str] | None = None) -> FakeAsc:
        records = [("INFO-LIVE", "READY_FOR_DISTRIBUTION")]
        if editable:
            records.insert(0 if next_first else 1, ("INFO-NEXT", next_state))
        return FakeAsc(app_info_records=records, app_info=current or APP_INFO_VALUES,
                       live_app_info=live or current or APP_INFO_VALUES)

    def test_a_version_in_a_draft_still_takes_app_information(self) -> None:
        fake = self.fake(next_state="READY_FOR_REVIEW")
        self.upload(fake, **{"subtitle.txt": "Offline JEE & NEET Notes"})
        self.assertEqual(fake.writes(), [("localizations", "update", "--type", "app-info", "--id", "LOC-INFO-NEXT",
                                          "--subtitle", "Offline JEE & NEET Notes")])

    def test_the_new_versions_copy_is_compared_whatever_the_order(self) -> None:
        # Waiting for review, the in-flight copy (not the live one) holds this
        # release's values; a rerun that already sent them passes untouched.
        wanted = {**APP_INFO_VALUES, "subtitle": "Offline JEE & NEET Notes"}
        for next_first in (True, False):
            fake = self.fake(next_state="WAITING_FOR_REVIEW", next_first=next_first, current=wanted,
                             live=APP_INFO_VALUES)
            with self.subTest(next_first=next_first):
                self.upload(fake, frozen_state="WAITING_FOR_REVIEW", **{"subtitle.txt": "Offline JEE & NEET Notes"})
                self.assertEqual(fake.writes(), [])
                lookup = [c for c in fake.calls if c[:2] == ("localizations", "list")][0]
                self.assertEqual(flag(lookup, "--app-info"), "INFO-NEXT")

    def test_a_frozen_version_refuses_changes_without_writing(self) -> None:
        fake = self.fake(next_state="WAITING_FOR_REVIEW")
        with self.assertRaises(SystemExit):
            self.upload(fake, frozen_state="WAITING_FOR_REVIEW", **{"name.txt": "PCM Notes"})
        self.assertEqual(fake.writes(), [])

    def test_unchanged_app_information_is_not_updated(self) -> None:
        fake = self.fake()
        self.upload(fake)
        self.assertEqual(fake.writes(), [])

    def test_only_changed_fields_go_to_the_editable_copy(self) -> None:
        fake = self.fake()
        self.upload(fake, **{"subtitle.txt": "Offline JEE & NEET Notes"})
        self.assertEqual(len(fake.writes()), 1)
        update = fake.writes()[0]
        self.assertEqual((flag(update, "--type"), flag(update, "--id")), ("app-info", "LOC-INFO-NEXT"))
        self.assertEqual({a for a in update if a.startswith("--")} - {"--type", "--id"}, {"--subtitle"})

    def test_changes_without_an_editable_copy_stop(self) -> None:
        fake = self.fake(editable=False)
        with self.assertRaises(SystemExit):
            self.upload(fake, **{"name.txt": "PCM Notes"})
        self.assertEqual(fake.writes(), [])
        self.upload(self.fake(editable=False))  # unchanged is fine


class WhatsNewGuardTests(unittest.TestCase):
    def check(self, live: str | None, text: str) -> FakeAsc:
        fake = FakeAsc(live_whats_new=live)
        quiet(asc_release.check_whats_new_is_new, fake, app_id=APP, version_id="V1", locale="en-US", text=text)
        return fake

    def test_the_live_versions_text_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.check("Bug fixes.\n", "Bug fixes.")

    def test_new_text_or_a_first_release_passes(self) -> None:
        fake = self.check("Bug fixes.", "Faster chapter search.")
        lookup = [c for c in fake.calls if c[:2] == ("localizations", "list")][0]
        self.assertEqual((flag(lookup, "--version"), flag(lookup, "--locale")), ("V0", "en-US"))
        self.check(None, "Bug fixes.")
        # No live version yet: nothing to compare with.
        quiet(asc_release.check_whats_new_is_new, FakeAsc(other_versions=[]), app_id=APP, version_id="V1",
              locale="en-US", text="Bug fixes.")


def png_bytes(width: int, height: int, *, color_type: int = 2, trns: bool = False) -> bytes:
    """A minimal PNG header: colour type 2 is RGB, 4 and 6 store alpha, tRNS adds transparency."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    extra = chunk(b"tRNS", b"\x00\x00") if trns else b""
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + extra + chunk(b"IDAT", b"x") + chunk(b"IEND", b"")


class ScreenshotTests(unittest.TestCase):
    SPEC = asc_release.ScreenshotSet("APP_IPHONE_67", "shots", frozenset({(1290, 2796)}))

    def make_dir(self, tmp: str, images: list[bytes]) -> Path:
        root = Path(tmp)
        (root / "shots").mkdir()
        for i, raw in enumerate(images):
            (root / "shots" / f"0{i + 1}.png").write_bytes(raw)
        return root

    def upload(self, fake: FakeAsc, root: Path, *, spec: asc_release.ScreenshotSet = SPEC,
               frozen_state: str | None = None) -> bool:
        live = asc_release.live_screenshot_sets(fake, app_id=APP, version_id="V1", locale="en-US")
        return quiet(asc_release.upload_screenshot_set, fake, app_id=APP, version_id="V1", locale="en-US",
                     spec=spec, live=live, root=root, frozen_state=frozen_state)

    def test_matching_screenshots_are_left_alone_whatever_their_live_names(self) -> None:
        # App Store Connect keeps the names a set was uploaded with (image1.png …);
        # the set matches when the checksums match, in order.
        images = [png_bytes(1290, 2796) + bytes([i]) for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, images)
            fake = FakeAsc(screenshots={"APP_IPHONE_67": [md5(raw) for raw in images]})
            self.assertFalse(self.upload(fake, root))
        self.assertEqual(fake.writes(), [])
        listing = fake.calls[0]
        self.assertEqual((flag(listing, "--app"), flag(listing, "--version-id"), flag(listing, "--locale"),
                          flag(listing, "--platform")), (APP, "V1", "en-US", "IOS"))

    def test_a_set_in_another_order_is_replaced(self) -> None:
        images = [png_bytes(1290, 2796) + bytes([i]) for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, images)
            fake = FakeAsc(screenshots={"APP_IPHONE_67": [md5(raw) for raw in reversed(images)]})
            self.assertTrue(self.upload(fake, root))
        self.assertEqual(fake.screenshots["APP_IPHONE_67"], [md5(raw) for raw in images])

    def test_changed_screenshots_are_replaced_in_order_and_processed(self) -> None:
        old = [png_bytes(1290, 2796) + b"old"]
        new = [png_bytes(1290, 2796) + bytes([i]) for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, new)
            fake = FakeAsc(screenshots={"APP_IPHONE_67": [md5(raw) for raw in old]})
            self.assertTrue(self.upload(fake, root))
            upload = fake.writes()[0]
            self.assertEqual(fake.writes(), [upload])
            self.assertEqual(upload[:2], ("screenshots", "upload"))
            self.assertEqual((flag(upload, "--app"), flag(upload, "--version-id"), flag(upload, "--locale"),
                              flag(upload, "--device-type")), (APP, "V1", "en-US", "IPHONE_67"))
            self.assertIn("--replace", upload)
            self.assertIn("--confirm", upload)
            self.assertFalse(Path(flag(upload, "--path")).exists(), "the staging folder is removed afterwards")
        self.assertEqual(fake.uploaded_files, [["01.png", "02.png"]])
        self.assertEqual(fake.screenshots["APP_IPHONE_67"], [md5(raw) for raw in new])
        # The set is read back after the upload.
        self.assertEqual(fake.calls[-1][:2], ("screenshots", "list"))

    def test_only_the_sets_pngs_reach_asc(self) -> None:
        # asc uploads every file in the folder it is given; other files next to
        # the PNGs (or in a subfolder) must neither be uploaded nor fail the run.
        images = [png_bytes(1290, 2796) + bytes([i]) for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, images)
            (root / "shots" / "00-notes.txt").write_text("not a screenshot")
            (root / "shots" / "03-extra.jpg").write_bytes(b"jpeg")
            (root / "shots" / "old").mkdir()
            (root / "shots" / "old" / "01.png").write_bytes(png_bytes(1290, 2796))
            fake = FakeAsc(screenshots={"APP_IPHONE_67": ["stale"]})
            self.assertTrue(self.upload(fake, root))
        self.assertEqual(fake.uploaded_files, [["01.png", "02.png"]])
        self.assertEqual(fake.screenshots["APP_IPHONE_67"], [md5(raw) for raw in images])

    def test_a_set_with_alpha_is_never_handed_to_asc(self) -> None:
        # asc refuses any PNG that stores alpha, even an opaque one.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, [png_bytes(1290, 2796, color_type=6)])
            fake = FakeAsc(screenshots={"APP_IPHONE_67": ["stale"]})
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err), self.assertRaises(SystemExit):
                live = asc_release.live_screenshot_sets(fake, app_id=APP, version_id="V1", locale="en-US")
                asc_release.upload_screenshot_set(fake, app_id=APP, version_id="V1", locale="en-US", spec=self.SPEC,
                                                  live=live, root=root)
        self.assertIn("alpha channel", err.getvalue())
        self.assertEqual(fake.writes(), [])

    def test_a_set_with_alpha_that_app_store_connect_holds_is_left_alone(self) -> None:
        # The committed RGBA screenshots are live already; nothing has to be uploaded.
        images = [png_bytes(1290, 2796, color_type=6) + bytes([i]) for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, images)
            for frozen_state in (None, "READY_FOR_REVIEW", "WAITING_FOR_REVIEW"):
                fake = FakeAsc(screenshots={"APP_IPHONE_67": [md5(raw) for raw in images]})
                with self.subTest(frozen_state=frozen_state):
                    self.assertFalse(self.upload(fake, root, frozen_state=frozen_state))
                    self.assertEqual(fake.writes(), [])

    def test_every_set_with_alpha_is_refused_before_any_set_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for spec, color_type in zip(asc_release.SCREENSHOT_SETS, (2, 6)):
                (root / spec.directory).mkdir(parents=True)
                width, height = sorted(spec.sizes)[0]
                (root / spec.directory / "01.png").write_bytes(png_bytes(width, height, color_type=color_type))
            fake = FakeAsc()
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err), self.assertRaises(SystemExit):
                asc_release.upload_screenshots(fake, app_id=APP, version_id="V1", root=root)
            problems = asc_release.screenshot_upload_problems({}, root)
        # The iPhone set (RGB) would upload, but the iPad set (RGBA) stops the run first.
        self.assertEqual(fake.writes(), [])
        self.assertIn("APP_IPAD_PRO_3GEN_129 screenshots 01.png have an alpha channel", err.getvalue())
        self.assertEqual(len(problems), 1)

    def test_processing_failure_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, [png_bytes(1290, 2796)])
            fake = FakeAsc(upload_state="FAILED")
            with self.assertRaises(SystemExit):
                self.upload(fake, root)

    def test_an_upload_that_does_not_land_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, [png_bytes(1290, 2796) + b"new"])
            fake = FakeAsc(screenshots={"APP_IPHONE_67": ["stale"]}, uploads_land=False)
            with self.assertRaises(SystemExit):
                self.upload(fake, root)
        self.assertEqual(fake.uploaded_sets, ["IPHONE_67"])

    def test_frozen_screenshots_pass_when_they_already_match(self) -> None:
        images = [png_bytes(1290, 2796) + bytes([i]) for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, images)
            fake = FakeAsc(screenshots={"APP_IPHONE_67": [md5(raw) for raw in images]})
            self.assertFalse(self.upload(fake, root, frozen_state="READY_FOR_REVIEW"))
        self.assertEqual(fake.writes(), [])

    def test_frozen_screenshots_that_differ_stop_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, [png_bytes(1290, 2796) + b"new"])
            fake = FakeAsc(screenshots={"APP_IPHONE_67": [md5(png_bytes(1290, 2796) + b"old")]})
            with self.assertRaises(SystemExit):
                self.upload(fake, root, frozen_state="READY_FOR_REVIEW")
        self.assertEqual(fake.writes(), [])

    def test_a_frozen_version_never_creates_a_screenshot_set(self) -> None:
        ipad = asc_release.ScreenshotSet("APP_IPAD_PRO_3GEN_129", "shots", frozenset({(1290, 2796)}))
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, [png_bytes(1290, 2796)])
            fake = FakeAsc(screenshots={"APP_IPHONE_67": [md5(png_bytes(1290, 2796))]})
            with self.assertRaises(SystemExit):
                self.upload(fake, root, spec=ipad, frozen_state="WAITING_FOR_REVIEW")
        self.assertEqual(fake.writes(), [])

    def test_processing_timeout_stops(self) -> None:
        fake = FakeAsc(screenshots={"APP_IPHONE_67": ["c"]})
        fake.screenshot_states["APP_IPHONE_67"] = "UPLOAD_COMPLETE"
        clock = FakeClock(0)
        with self.assertRaises(SystemExit):
            quiet(asc_release.wait_for_screenshot_processing, fake, app_id=APP, version_id="V1", locale="en-US",
                  display_type="APP_IPHONE_67", timeout_seconds=60, poll_seconds=10, now=clock.now, sleep=clock.sleep)
        self.assertLessEqual(clock.time, 70)
        self.assertEqual(fake.writes(), [])

    def test_every_set_uploads_with_its_own_device_type(self) -> None:
        self.assertEqual([spec.device_type for spec in asc_release.SCREENSHOT_SETS], ["IPHONE_67", "IPAD_PRO_3GEN_129"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for spec in asc_release.SCREENSHOT_SETS:
                (root / spec.directory).mkdir(parents=True)
                width, height = sorted(spec.sizes)[0]
                (root / spec.directory / "01.png").write_bytes(png_bytes(width, height))
            fake = FakeAsc()
            quiet(asc_release.upload_screenshots, fake, app_id=APP, version_id="V1", root=root)
        self.assertEqual(fake.uploaded_sets, ["IPHONE_67", "IPAD_PRO_3GEN_129"])
        self.assertEqual([flag(c, "--locale") for c in fake.writes()], ["en-US", "en-US"])

    def test_a_missing_primary_localization_stops_before_any_upload(self) -> None:
        fake = FakeAsc(version_locales=set())
        with self.assertRaises(SystemExit):
            quiet(asc_release.upload_screenshots, fake, app_id=APP, version_id="V1")
        self.assertEqual(fake.writes(), [])

    def test_png_size_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            path.write_bytes(png_bytes(2048, 2732))
            self.assertEqual(asc_release.png_size(path), (2048, 2732))
            path.write_bytes(b"not a png at all, just text....")
            with self.assertRaises(SystemExit):
                quiet(asc_release.png_size, path)

    def test_wrong_sized_or_missing_screenshots_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for spec in asc_release.SCREENSHOT_SETS:
                (root / spec.directory).mkdir(parents=True)
                width, height = sorted(spec.sizes)[0]
                (root / spec.directory / "01.png").write_bytes(png_bytes(width, height))
            quiet(asc_release.validate_screenshots, root)
            (root / "store/metadata/screenshots/ios/6.7/02.png").write_bytes(png_bytes(1179, 2556))
            with self.assertRaises(SystemExit):
                quiet(asc_release.validate_screenshots, root)
            # asc refuses 1284x2778 for the 6.7" set, so the release does too.
            (root / "store/metadata/screenshots/ios/6.7/02.png").write_bytes(png_bytes(1284, 2778))
            with self.assertRaises(SystemExit):
                quiet(asc_release.validate_screenshots, root)
            for path in (root / "store/metadata/screenshots/ios/6.7").glob("*.png"):
                path.unlink()
            with self.assertRaises(SystemExit):
                quiet(asc_release.validate_screenshots, root)


def tips_fake(*, draft: bool = True, versions: dict[str, list[tuple[int, str]]] | None = None, **states: str) -> FakeAsc:
    """Catalog tips in the given states. Each Ready-to-Submit tip has a version 1 in
    PREPARE_FOR_SUBMISSION unless `versions` says otherwise."""
    iaps = [{"type": "inAppPurchases", "id": f"IAP-{tier}", "attributes": {"name": tier, "productId": TIP + tier,
                                                                         "state": state}}
            for tier, state in states.items()]
    return FakeAsc(iaps=iaps, iap_versions=versions, drafts=("SUB1",) if draft else ())


def attached_iap_versions(fake: FakeAsc) -> list[str]:
    return [flag(c, "--item-id") for c in fake.writes()
            if c[:2] == ("review", "items-add") and flag(c, "--item-type") == "inAppPurchaseVersions"]


class ProductSubmissionTests(unittest.TestCase):
    def submit(self, fake: FakeAsc, intended: set[str] | None = None, **kwargs):
        ids = intended if intended is not None else {TIP + t for t in TIERS}
        return quiet(asc_release.submit_in_app_purchases, fake, APP, intended_ids=ids, **kwargs)

    def test_only_ready_catalog_tips_join_the_review_submission(self) -> None:
        fake = tips_fake(small="APPROVED", medium="READY_TO_SUBMIT", large="APPROVED", generous="APPROVED",
                         extra="READY_TO_SUBMIT")
        result = self.submit(fake)
        self.assertEqual(attached_iap_versions(fake), ["IAPV-medium-1"])
        add = fake.writes()[0]
        self.assertEqual((flag(add, "--submission"), flag(add, "--if-exists")), ("SUB1", "skip"))
        self.assertEqual(result.submitted, 1)
        self.assertTrue(result.has_products)
        self.assertFalse([c for c in fake.calls if "IAP-extra" in c], "a product outside the catalog was read")

    def test_attaching_never_submits(self) -> None:
        # Products wait in the draft for the single submit after the build
        # recheck; nothing sends one to App Review on its own.
        fake = tips_fake(small="READY_TO_SUBMIT", medium="READY_TO_SUBMIT")
        self.submit(fake)
        self.assertEqual({c[:2] for c in fake.writes()}, {("review", "items-add")})
        self.assertFalse([c for c in fake.calls if c[:3] == ("iap", "versions", "submit")])

    def test_approved_tips_mean_nothing_to_submit(self) -> None:
        fake = tips_fake(small="APPROVED", medium="APPROVED")
        self.assertEqual(self.submit(fake).submitted, 0)
        self.assertEqual(fake.writes(), [])

    def test_no_draft_is_created_when_nothing_needs_review(self) -> None:
        # The idempotent rerun path passes no draft; with all four tips approved
        # (PCM Notes today) it must not leave an empty review submission behind.
        fake = tips_fake(draft=False, small="APPROVED", medium="APPROVED")
        result = self.submit(fake)
        self.assertEqual(fake.writes(), [])
        self.assertFalse(result.has_products)

    def test_a_draft_is_created_only_to_attach_a_product(self) -> None:
        fake = tips_fake(draft=False, small="READY_TO_SUBMIT")
        result = self.submit(fake)
        self.assertEqual([c[:2] for c in fake.writes()], [("review", "submissions-create"), ("review", "items-add")])
        self.assertEqual(flag(fake.writes()[0], "--platform"), "IOS")
        self.assertEqual(result.submission_id, "SUB-NEW")

    def test_a_tip_that_cannot_be_submitted_blocks_the_release(self) -> None:
        fake = tips_fake(small="READY_TO_SUBMIT")
        fake.refuse_items = {"IAPV-small-1"}
        with self.assertRaisesRegex(asc_release.ProductSubmitError, "small"):
            self.submit(fake)

    def test_a_ready_tip_without_a_draft_version_blocks_the_release(self) -> None:
        fake = tips_fake(small="READY_TO_SUBMIT", versions={"small": [(1, "APPROVED")]})
        with self.assertRaisesRegex(asc_release.ProductSubmitError, "no version to submit"):
            self.submit(fake)
        self.assertEqual(fake.writes(), [])

    def test_the_newest_draft_version_is_attached(self) -> None:
        versions = {"small": [(1, "REPLACED_WITH_NEW_VERSION"), (2, "APPROVED"), (3, "PREPARE_FOR_SUBMISSION")]}
        fake = tips_fake(small="READY_TO_SUBMIT", versions=versions)
        self.submit(fake)
        self.assertEqual(attached_iap_versions(fake), ["IAPV-small-3"])

    def test_an_already_attached_tip_is_not_added_again(self) -> None:
        fake = tips_fake(small="READY_TO_SUBMIT")
        fake.items = {"SUB1": [item("I", "inAppPurchaseVersion", "IAPV-small-1")]}
        result = self.submit(fake)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(result.submitted, 1)
        self.assertTrue(result.has_products)

    def test_subscriptions_attach_their_current_version_once(self) -> None:
        subs = [
            {"id": "S0", "attributes": {"name": "Plus", "productId": "plus", "state": "DEVELOPER_REJECTED"}},
            {"id": "S1", "attributes": {"name": "Pro", "productId": "pro", "state": "READY_TO_SUBMIT"}},
        ]
        fake = FakeAsc(iaps=[], subscriptions=subs, draft_items={"SUB1": [item("I", "subscriptionVersion", "S0-v2")]})
        result = self.submit(fake, {"plus", "pro"})
        items = [flag(c, "--item-id") for c in fake.writes() if flag(c, "--item-type") == "subscriptionVersions"]
        self.assertEqual(items, ["S1-v2"])  # S0's current version was already attached
        self.assertEqual(result.submitted, 2)
        self.assertTrue(result.has_products)
        listing = [c for c in fake.calls if c[:2] == ("subscriptions", "list")][0]
        self.assertEqual(flag(listing, "--app"), APP)  # every subscription group of the app

    def test_every_catalog_products_versions_are_allowed_in_the_draft(self) -> None:
        fake = tips_fake(small="APPROVED", medium="READY_TO_SUBMIT", extra="READY_TO_SUBMIT",
                         versions={"small": [(1, "APPROVED"), (2, "REPLACED_WITH_NEW_VERSION")]})
        result = self.submit(fake)
        self.assertEqual(result.allowed_versions, {"IAPV-small-1", "IAPV-small-2", "IAPV-medium-1"})

    def test_empty_catalog_submits_nothing(self) -> None:
        fake = tips_fake(small="READY_TO_SUBMIT")
        self.assertEqual(self.submit(fake, set()).submitted, 0)
        self.assertEqual(fake.calls, [])

    def test_draft_items_are_read_with_their_versions(self) -> None:
        fake = FakeAsc(draft_items={"SUB1": [item("I", "inAppPurchaseVersion", "IAPV-1")]})
        self.assertEqual(asc_release.draft_item_version_ids(fake, "SUB1"), {"IAPV-1"})
        listing = fake.calls[0]
        self.assertEqual(listing[:2], ("review", "items-list"))
        self.assertEqual(set(flag(listing, "--include").split(",")),
                         {"appStoreVersion", "subscriptionVersion", "inAppPurchaseVersion", "appAssetLibraryImage"})

    def test_catalog_lists_the_four_tips(self) -> None:
        self.assertEqual(asc_release.intended_product_ids(), {TIP + t for t in TIERS})

    def test_a_creative_draft_is_never_used_for_products(self) -> None:
        fake = tips_fake(small="READY_TO_SUBMIT")
        fake.drafts = ["CREATIVE"]
        fake.items = {"CREATIVE": [item("IMG", "appAssetLibraryImage", "H")]}
        result = self.submit(fake)
        self.assertEqual(result.submission_id, "SUB-NEW")
        self.assertEqual(fake.items["CREATIVE"], [item("IMG", "appAssetLibraryImage", "H")])

    def test_submit_for_review_does_not_readd_a_held_version(self) -> None:
        fake = FakeAsc(draft_items={"SUB1": [item("I", "appStoreVersion", "AV1")]})
        quiet(asc_release.submit_for_review, fake, APP, "AV1", "PREPARE_FOR_SUBMISSION", submission_id="SUB1")
        self.assertEqual(fake.writes(), [("review", "submissions-submit", "--id", "SUB1", "--confirm")])
        fake = FakeAsc(draft_items={"SUB1": [item("I", "appStoreVersion", "AV1")]})
        self.assertTrue(asc_release.draft_holds_app_version(fake, "SUB1"))

    def test_a_version_draft_without_the_version_is_never_submitted(self) -> None:
        # READY_FOR_REVIEW skips adding the version, so the draft must already hold it.
        fake = FakeAsc(draft_items={"SUB1": [item("P", "inAppPurchaseVersion", "IAPV-small-1")]})
        with self.assertRaises(SystemExit):
            quiet(asc_release.submit_for_review, fake, APP, "V1", "READY_FOR_REVIEW", submission_id="SUB1",
                  allowed_products={"IAPV-small-1"})
        self.assertEqual(fake.writes(), [])

    def test_submit_for_review_refuses_items_it_did_not_choose(self) -> None:
        for stray in (item("X", "appStoreVersion", "V0"), item("X", "inAppPurchaseVersion", "OTHER"),
                      item("X", "appAssetLibraryImage", "H")):
            fake = FakeAsc(draft_items={"SUB1": [stray]})
            with self.subTest(stray=stray), self.assertRaises(SystemExit):
                quiet(asc_release.submit_for_review, fake, APP, "V1", "PREPARE_FOR_SUBMISSION", submission_id="SUB1",
                      allowed_products={"IAPV-small-1"})
            self.assertNotIn(("review", "submissions-submit"), [c[:2] for c in fake.writes()])


class CmdWaitBuildTests(unittest.TestCase):
    def test_deadline_counts_from_upload_start_and_app_lookup_is_retried(self) -> None:
        lookups = []

        class Lookup:
            def run(self, *args: str, **_: object):
                lookups.append(args)
                if len(lookups) == 1:
                    raise asc_error(63)
                return {"data": {"id": APP, "attributes": {"bundleId": ios_release.BUNDLE_ID}}}

        captured = {}

        def fake_wait(client, **kwargs):
            captured.update(kwargs)
            return {"id": "B9"}

        args = argparse.Namespace(version=VERSION, build=8, started_at=STARTED, wait_minutes=120, poll_seconds=30)
        with tempfile.NamedTemporaryFile("r", suffix=".out") as out, mock.patch.object(
            ios_release, "asc_client", return_value=(asc_release, Lookup())
        ), mock.patch.object(ios_release, "wait_for_build", side_effect=fake_wait), mock.patch.object(
            ios_release.time, "sleep"
        ), mock.patch.object(ios_release.time, "time", return_value=STARTED + 30), mock.patch.dict(
            os.environ, {"GITHUB_OUTPUT": out.name}
        ), redirect_stdout(io.StringIO()):
            ios_release.cmd_wait_build(args)
            written = Path(out.name).read_text()
        self.assertEqual(lookups, [("apps", "view", "--id", APP)] * 2)
        self.assertEqual(captured["deadline"], STARTED + 120 * 60)
        self.assertEqual(captured["uploaded_after"], STARTED)
        self.assertEqual(captured["build_number"], "8")
        self.assertIs(captured["is_transient"], ios_release.is_transient_error)
        self.assertIn("build_id=B9", written)

    def test_another_app_record_stops_the_wait(self) -> None:
        class Other:
            def run(self, *args: str, **_: object):
                return {"data": {"id": APP, "attributes": {"bundleId": "com.example.other"}}}

        args = argparse.Namespace(version=VERSION, build=8, started_at=STARTED, wait_minutes=120, poll_seconds=30)
        with mock.patch.object(ios_release, "asc_client", return_value=(asc_release, Other())), \
                mock.patch.object(ios_release.time, "time", return_value=STARTED), \
                redirect_stdout(io.StringIO()), self.assertRaises(ios_release.ReleaseError):
            ios_release.cmd_wait_build(args)

    def test_app_lookup_gives_up_at_the_deadline(self) -> None:
        def boom():
            raise asc_error(63)

        clock = FakeClock(STARTED)
        with redirect_stdout(io.StringIO()), self.assertRaises(asc_cli.AscError):
            ios_release.call_with_retries(boom, deadline=STARTED + 90, poll_seconds=30, now=clock.now, sleep=clock.sleep)
        self.assertEqual(clock.sleeps, [30, 30, 30])

    def test_a_fatal_lookup_error_is_not_retried(self) -> None:
        calls = []

        def unauthorized():
            calls.append(1)
            raise asc_error(3)

        clock = FakeClock(STARTED)
        with redirect_stdout(io.StringIO()), self.assertRaises(asc_cli.AscError):
            ios_release.call_with_retries(unauthorized, deadline=STARTED + 600, poll_seconds=30, now=clock.now,
                                          sleep=clock.sleep)
        self.assertEqual((len(calls), clock.sleeps), (1, []))


class LiveFlowCase(unittest.TestCase):
    """asc_release.main() against FakeAsc, with a temporary listing and screenshots."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.metadata = root / "metadata"
        (write_listing(self.metadata) / "whats_new.txt").write_text(REPO_WHATS_NEW + "\n")
        sets, self.screenshot_sums = [], {}
        for spec in asc_release.SCREENSHOT_SETS:
            directory = root / spec.display_type
            directory.mkdir()
            width, height = sorted(spec.sizes)[0]
            images = [png_bytes(width, height) + bytes([i]) for i in range(2)]
            for i, raw in enumerate(images):
                (directory / f"0{i + 1}.png").write_bytes(raw)
            self.screenshot_sums[spec.display_type] = [md5(raw) for raw in images]
            sets.append(spec._replace(directory=str(directory)))  # absolute: independent of ROOT
        patcher = mock.patch.object(asc_release, "SCREENSHOT_SETS", tuple(sets))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, **kwargs) -> FakeAsc:
        kwargs.setdefault("screenshots", self.screenshot_sums)
        return FakeAsc(**kwargs)

    def run_main(self, fake: FakeAsc, env_flags: dict[str, str] | None = None, *,
                 real_products: bool = False, argv: list[str] | None = None) -> tuple[int, mock.Mock]:
        products = mock.Mock(return_value=asc_release.ProductSubmitResult("SUB1", 0, False))
        argv = argv if argv is not None else ["--version", VERSION, "--build-id", "B9", "--build-number", "8",
                                              "--metadata-dir", str(self.metadata)]
        env = {k: v for k, v in os.environ.items() if k not in FLAG_NAMES}
        env.update(env_flags or {"SUBMIT_IOS_REVIEW": "true"})
        patches = [mock.patch.object(asc_release, "asc_from_environment", return_value=fake)]
        if not real_products:
            patches.append(mock.patch.object(asc_release, "submit_in_app_purchases", products))
        code = 0
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", ["asc_release.py", *argv]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for patch in patches:
                patch.start()
            try:
                asc_release.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            finally:
                for patch in patches:
                    patch.stop()
        return code, products


class LiveSubmitFlowTests(LiveFlowCase):
    def test_selects_rechecks_then_submits(self) -> None:
        fake = self.fake()
        code, products = self.run_main(fake)
        self.assertEqual(code, 0)
        calls = [c[:2] for c in fake.calls]
        select = calls.index(("versions", "attach-build"))
        submit = calls.index(("review", "submissions-submit"))
        reads = [i for i, c in enumerate(calls) if c == ("versions", "view")]
        build_checks = [i for i, c in enumerate(calls) if c == ("builds", "list")]
        self.assertEqual(len(reads), 3, "selection is read before and after selecting, then rechecked")
        self.assertTrue(reads[0] < select < reads[1] < reads[2] < submit)
        self.assertEqual(len(build_checks), 2)
        self.assertTrue(build_checks[1] < reads[2] < submit, "build must be re-verified before submitting")
        first_review_write = fake.calls.index(fake.review_writes()[0])
        self.assertTrue(reads[2] < first_review_write, "the recheck comes before any review-submission write")
        update = [c for c in fake.calls if c[:2] == ("versions", "update")][0]
        self.assertEqual(flag(update, "--release-type"), "MANUAL")
        products.assert_called_once()
        self.assertEqual(flag(fake.calls[submit], "--id"), "SUB1")

    def test_selection_changed_before_submit_aborts(self) -> None:
        fake = self.fake(revert_on_read=3)  # the recheck sees another build selected
        code, products = self.run_main(fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.review_writes(), [])
        products.assert_not_called()

    def test_products_join_the_draft_after_the_recheck_and_go_out_with_one_submit(self) -> None:
        fake = self.fake(ready_tips=("medium",))
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 0)
        recheck = [i for i, c in enumerate(fake.calls) if c[:2] == ("versions", "view")][-1]
        writes = fake.review_writes()
        self.assertEqual([c[:2] for c in writes], [("review", "items-add"), ("review", "items-add"),
                                                   ("review", "submissions-submit")])
        self.assertTrue(all(fake.calls.index(write) > recheck for write in writes))
        self.assertEqual([flag(c, "--item-type") for c in writes[:2]], ["inAppPurchaseVersions", "appStoreVersions"])
        self.assertEqual(flag(writes[0], "--item-id"), "IAPV-medium-1")
        self.assertEqual(flag(writes[-1], "--id"), "SUB1")

    def test_no_review_write_when_the_recheck_fails(self) -> None:
        fake = self.fake(ready_tips=("medium",), revert_on_read=3)
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.review_writes(), [])

    def test_a_product_that_cannot_be_added_keeps_the_version_out_of_review(self) -> None:
        fake = self.fake(ready_tips=("medium",), refuse_items={"IAPV-medium-1"})
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 1)
        self.assertNotIn(("review", "submissions-submit"), [c[:2] for c in fake.writes()])
        self.assertNotIn("appStoreVersions", [flag(c, "--item-type") for c in fake.review_writes()])

    def test_a_reused_draft_with_items_this_release_did_not_choose_is_not_submitted(self) -> None:
        fake = self.fake(draft_items={"SUB1": [item("STRAY", "inAppPurchaseVersion", "OTHER-IAPV")]})
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 1)
        self.assertNotIn(("review", "submissions-submit"), [c[:2] for c in fake.writes()])
        # A catalog product's version (in any state) and this version are fine.
        fake = self.fake(draft_items={"SUB1": [item("OK", "inAppPurchaseVersion", "IAPV-small-1")]})
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes()[-1][:2], ("review", "submissions-submit"))

    def test_another_versions_draft_is_never_reused(self) -> None:
        theirs = [item("V", "appStoreVersion", "V0")]
        fake = self.fake(drafts=("THEIRS",), draft_items={"THEIRS": theirs})
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 0)
        self.assertIn(("review", "submissions-create"), [c[:2] for c in fake.writes()])
        self.assertEqual(flag(fake.writes()[-1], "--id"), "SUB-NEW")
        self.assertEqual(fake.items["THEIRS"], theirs)

    def test_the_draft_holding_this_version_is_chosen_over_others(self) -> None:
        other = [item("P", "inAppPurchaseVersion", "IAPV-small-1")]
        mine = [item("V", "appStoreVersion", "V1")]
        fake = self.fake(state="READY_FOR_REVIEW", attached="B9", ready_tips=("medium",), drafts=("OTHER", "MINE"),
                         draft_items={"OTHER": other, "MINE": mine})
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 0)
        self.assertEqual({flag(c, "--submission") for c in fake.writes() if c[:2] == ("review", "items-add")}, {"MINE"})
        self.assertEqual(flag(fake.writes()[-1], "--id"), "MINE")
        self.assertEqual(fake.items["OTHER"], other)

    def test_a_ready_for_review_version_that_no_draft_holds_stops_before_writing_to_review(self) -> None:
        # READY_FOR_REVIEW says a draft holds the version; the only draft here does not.
        held = [item("P", "inAppPurchaseVersion", "IAPV-medium-1")]
        fake = self.fake(state="READY_FOR_REVIEW", attached="B9", ready_tips=("medium",), drafts=("D1",),
                         draft_items={"D1": held})
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.review_writes(), [])
        fake = self.fake(state="READY_FOR_REVIEW", attached="B9", drafts=())
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.review_writes(), [])

    def test_asc_placeholders_for_missing_relationships_count_as_absent(self) -> None:
        # asc writes a missing relationship as {"type": "", "id": ""}.
        blank = {"data": {"type": "", "id": ""}}
        placeholder = {"id": "I", "relationships": {"appStoreVersion": blank, "appAssetLibraryImage": blank,
                                                    "subscriptionVersion": blank,
                                                    "inAppPurchaseVersion": {"data": {"type": "inAppPurchaseVersions",
                                                                                      "id": "IAPV-small-1"}}}}
        fake = self.fake(draft_items={"SUB1": [placeholder]})
        self.assertEqual(asc_release.find_draft_submission(fake, APP, "V1"), "SUB1")
        self.assertFalse(asc_release.draft_holds_app_version(fake, "SUB1"))
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 0)
        self.assertEqual(flag(fake.writes()[-1], "--id"), "SUB1")

    def test_a_creative_draft_is_never_used_for_the_version(self) -> None:
        fake = self.fake(drafts=("CREATIVE",), draft_items={"CREATIVE": [item("IMG", "appAssetLibraryImage", "H")]})
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 0)
        self.assertIn(("review", "submissions-create"), [c[:2] for c in fake.writes()])
        self.assertEqual(flag(fake.writes()[-1], "--id"), "SUB-NEW")
        self.assertEqual(fake.items["CREATIVE"], [item("IMG", "appAssetLibraryImage", "H")])

    def test_public_release_sets_after_approval(self) -> None:
        fake = self.fake()
        code, _ = self.run_main(fake, {"SUBMIT_IOS_REVIEW": "true", "PUBLIC_RELEASE": "true"})
        self.assertEqual(code, 0)
        update = [c for c in fake.calls if c[:2] == ("versions", "update")][0]
        self.assertEqual(flag(update, "--release-type"), "AFTER_APPROVAL")

    def test_waiting_for_review_with_another_build_aborts(self) -> None:
        fake = self.fake(state="WAITING_FOR_REVIEW", attached="OTHER")
        code, products = self.run_main(fake)
        self.assertEqual(code, 1)
        products.assert_not_called()
        self.assertEqual(fake.writes(), [])

    def test_waiting_for_review_with_this_build_is_idempotent(self) -> None:
        fake = self.fake(state="WAITING_FOR_REVIEW", attached="B9")
        code, products = self.run_main(fake)
        self.assertEqual(code, 0)
        products.assert_called_once()
        self.assertNotIn(("versions", "attach-build"), [c[:2] for c in fake.calls])

    def test_metadata_on_a_live_version_is_refused_without_writes(self) -> None:
        fake = self.fake(state="READY_FOR_SALE")
        code, _ = self.run_main(fake, {"UPLOAD_LISTING": "true"})
        self.assertEqual(code, 1)
        self.assertEqual(fake.writes(), [])

    def test_another_app_record_stops_before_any_write(self) -> None:
        fake = self.fake(bundle="com.example.other")
        code, _ = self.run_main(fake, {"UPLOAD_LISTING": "true", "SUBMIT_IOS_REVIEW": "true"})
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [("apps", "view", "--id", APP)])


LIVE_SWITCHES = {"UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true", "SUBMIT_IOS_REVIEW": "true"}


class RerunTests(LiveFlowCase):
    """"Re-run failed jobs" after the first attempt got the version into a draft
    (READY_FOR_REVIEW) or into review (WAITING_FOR_REVIEW), with metadata switches on."""

    HELD = {"SUB1": [item("I1", "appStoreVersion", "V1")]}

    def test_rerun_in_a_draft_finishes_the_submission(self) -> None:
        fake = self.fake(state="READY_FOR_REVIEW", attached="B9", release_type="MANUAL", draft_items=self.HELD)
        code, _ = self.run_main(fake, {**LIVE_SWITCHES, "UPLOAD_SCREENSHOTS": "true"}, real_products=True)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [("review", "submissions-submit", "--id", "SUB1", "--confirm")])

    def test_rerun_in_a_draft_still_updates_changed_text(self) -> None:
        fake = self.fake(state="READY_FOR_REVIEW", attached="B9", differ=("listing", "app_info"), draft_items=self.HELD)
        code, _ = self.run_main(fake, LIVE_SWITCHES, real_products=True)
        self.assertEqual(code, 0)
        updates = [c for c in fake.writes() if c[:2] == ("localizations", "update")]
        self.assertEqual([flag(c, "--id") for c in updates], ["LOC-INFO-NEXT", "VL"])
        self.assertEqual(flag(updates[0], "--subtitle"), APP_INFO_VALUES["subtitle"])
        self.assertEqual(flag(updates[1], "--promotional-text"), LISTING_VALUES["promotionalText"])
        self.assertEqual(fake.writes()[-1][:2], ("review", "submissions-submit"))

    def test_rerun_in_a_draft_refuses_changed_screenshots(self) -> None:
        fake = self.fake(state="READY_FOR_REVIEW", attached="B9", differ=("screenshots",), draft_items=self.HELD)
        code, _ = self.run_main(fake, {**LIVE_SWITCHES, "UPLOAD_SCREENSHOTS": "true"}, real_products=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.review_writes(), [])
        self.assertEqual(fake.uploaded_sets, [])

    def test_rerun_waiting_for_review_is_idempotent(self) -> None:
        fake = self.fake(state="WAITING_FOR_REVIEW", attached="B9")
        code, _ = self.run_main(fake, {**LIVE_SWITCHES, "UPLOAD_SCREENSHOTS": "true"}, real_products=True)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [], "nothing to change and no product left to submit")

    def test_rerun_waiting_for_review_stops_on_changed_metadata_without_writing(self) -> None:
        for what in ("listing", "app_info", "whats_new", "screenshots"):
            fake = self.fake(state="WAITING_FOR_REVIEW", attached="B9", differ=(what,))
            with self.subTest(what=what):
                code, _ = self.run_main(fake, {**LIVE_SWITCHES, "UPLOAD_SCREENSHOTS": "true"},
                                        real_products=True)
                self.assertEqual(code, 1)
                self.assertEqual(fake.writes(), [])

    def test_rerun_waiting_for_review_catches_up_a_late_product(self) -> None:
        fake = self.fake(state="WAITING_FOR_REVIEW", attached="B9", drafts=(), ready_tips=("small",))
        code, _ = self.run_main(fake, LIVE_SWITCHES, real_products=True)
        self.assertEqual(code, 0)
        self.assertEqual([c[:2] for c in fake.writes()], [("review", "submissions-create"), ("review", "items-add"),
                                                         ("review", "submissions-submit")])
        self.assertEqual(flag(fake.writes()[-1], "--id"), "SUB-NEW")

    def test_catch_up_never_touches_a_draft_holding_an_app_version(self) -> None:
        # The late product goes out in a new product-only draft instead.
        held = [item("X", "appStoreVersion", "V0")]
        fake = self.fake(state="WAITING_FOR_REVIEW", attached="B9", ready_tips=("small",), draft_items={"SUB1": held})
        code, _ = self.run_main(fake, LIVE_SWITCHES, real_products=True)
        self.assertEqual(code, 0)
        self.assertNotIn("SUB1", [flag(c, "--submission") for c in fake.writes() if c[:2] == ("review", "items-add")])
        self.assertEqual(flag(fake.writes()[-1], "--id"), "SUB-NEW")
        self.assertEqual(fake.items["SUB1"], held)

    def test_catch_up_never_submits_a_draft_with_items_this_release_did_not_choose(self) -> None:
        fake = self.fake(state="WAITING_FOR_REVIEW", attached="B9", ready_tips=("small",),
                         draft_items={"SUB1": [item("X", "subscriptionVersion", "OTHER-SUB")]})
        code, _ = self.run_main(fake, LIVE_SWITCHES, real_products=True)
        self.assertEqual(code, 1)
        self.assertNotIn(("review", "submissions-submit"), [c[:2] for c in fake.writes()])

    def test_a_rerun_with_nothing_to_send_leaves_no_empty_draft(self) -> None:
        fake = self.fake(state="WAITING_FOR_REVIEW", attached="B9", drafts=())
        code, _ = self.run_main(fake, LIVE_SWITCHES, real_products=True)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])

    def test_metadata_only_run_on_a_version_in_review_passes_when_it_matches(self) -> None:
        fake = self.fake(state="WAITING_FOR_REVIEW", attached="B9")
        code, _ = self.run_main(fake, {"UPLOAD_LISTING": "true"})
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])


def run_create_version(fake: FakeAsc, env: dict[str, str], *args: str) -> tuple[int, str]:
    """asc_release.py --create-version against FakeAsc; returns (exit code, output)."""
    out = io.StringIO()
    full_env = {k: v for k, v in os.environ.items() if k not in FLAG_NAMES}
    full_env.update(env)
    code = 0
    with mock.patch.dict(os.environ, full_env, clear=True), mock.patch.object(
        sys, "argv", ["asc_release.py", "--create-version", *args]
    ), mock.patch.object(asc_release, "asc_from_environment", return_value=fake), \
            redirect_stdout(out), redirect_stderr(out):
        try:
            asc_release.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue()


class CreateVersionTests(unittest.TestCase):
    """--create-version (IOS_CREATE_VERSION): add the tag's App Store version only when it is missing."""

    ON = {"CREATE_IOS_VERSION": "true"}

    def create(self, fake: FakeAsc, flags: dict[str, str] | None = None, version: str = VERSION) -> tuple[int, str]:
        return run_create_version(fake, self.ON if flags is None else flags, "--version", version)

    def test_switch_off_makes_no_calls(self) -> None:
        for flags in ({}, {"CREATE_IOS_VERSION": "false"}):
            with self.subTest(flags=flags):
                fake = FakeAsc(has_version=False)
                code, output = self.create(fake, flags)
                self.assertEqual(code, 0, output)
                self.assertIn("skipped", output)
                self.assertEqual(fake.calls, [])

    def test_existing_version_is_left_alone(self) -> None:
        for state in ("PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW", "WAITING_FOR_REVIEW"):
            with self.subTest(state=state):
                fake = FakeAsc(state=state)
                code, output = self.create(fake)
                self.assertEqual(code, 0, output)
                self.assertIn(f"already exists ({state})", output)
                self.assertEqual(fake.writes(), [])

    def test_missing_version_is_created_for_this_app_and_read_back(self) -> None:
        fake = FakeAsc(has_version=False)
        code, output = self.create(fake)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(fake.writes()), 1)
        create = fake.writes()[0]
        self.assertEqual(create[:2], ("versions", "create"))
        self.assertEqual((flag(create, "--app"), flag(create, "--version"), flag(create, "--platform")),
                         (APP, VERSION, "IOS"))
        self.assertEqual(fake.calls[-1][:2], ("versions", "list"), "the created version must be read back")
        self.assertIn(f"created App Store version {VERSION} (PREPARE_FOR_SUBMISSION)", output)

    def test_a_created_version_that_does_not_read_back_fails_naming_it(self) -> None:
        fake = FakeAsc(has_version=False, creates_version=False)
        code, output = self.create(fake)
        self.assertEqual(code, 1)
        self.assertIn(f"created App Store version {VERSION}, but reading it back found 0 records", output)
        self.assertIn("check it in App Store Connect before rerunning", output)
        self.assertNotIn("turn on IOS_CREATE_VERSION", output)
        self.assertEqual([c[:2] for c in fake.writes()], [("versions", "create")])

    def test_a_created_version_that_reads_back_twice_fails_naming_it(self) -> None:
        class Doubled(FakeAsc):
            def run(self, *args: str, **kwargs: object):
                result = super().run(*args, **kwargs)
                if args[:2] == ("versions", "create"):  # App Store Connect now lists two records for it
                    self.other_versions = [*self.other_versions, (VERSION, "PREPARE_FOR_SUBMISSION")]
                return result

        fake = Doubled(has_version=False)
        code, output = self.create(fake)
        self.assertEqual(code, 1)
        self.assertIn(f"created App Store version {VERSION}, but reading it back found 2 records", output)
        self.assertEqual([c[:2] for c in fake.writes()], [("versions", "create")])

    def test_the_preflight_accepts_a_version_this_step_created(self) -> None:
        # A new version starts with the previous version's screenshots.
        fake = FakeAsc(has_version=False, screenshots=repository_screenshot_sums())
        code, output = self.create(fake)
        self.assertEqual(code, 0, output)
        out = io.StringIO()
        with mock.patch.object(ios_release, "asc_client", return_value=(asc_release, fake)), \
                redirect_stdout(out), redirect_stderr(out):
            ios_release.main(["preflight", "--version", VERSION, "--whats-new", "--screenshots", "--submit"])
        self.assertIn("can take this release", out.getvalue())
        self.assertEqual([c[:2] for c in fake.writes()], [("versions", "create")])

    def test_another_version_on_its_way_blocks_creation(self) -> None:
        for state in ("PREPARE_FOR_SUBMISSION", "WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE", ""):
            with self.subTest(state=state):
                fake = FakeAsc(has_version=False, other_versions=[("0.9", "REPLACED_WITH_NEW_VERSION"), ("1.0", state)])
                code, output = self.create(fake)
                self.assertEqual(code, 1)
                self.assertIn(f"is still {state or 'in an unknown state'}", output)
                self.assertIn("nothing was changed", output)
                self.assertEqual(fake.writes(), [])

    def test_version_not_newer_than_an_existing_one_is_refused(self) -> None:
        for existing in ("1.1", "1.0.10", "2"):
            with self.subTest(existing=existing):
                fake = FakeAsc(has_version=False, other_versions=[(existing, "READY_FOR_SALE")])
                code, output = self.create(fake)
                self.assertEqual(code, 1)
                self.assertIn(f"not newer than the existing version {existing}", output)
                self.assertEqual(fake.writes(), [])

    def test_older_finished_versions_allow_creation(self) -> None:
        for state in sorted(asc_release.FINISHED_VERSION_STATES):
            with self.subTest(state=state):
                fake = FakeAsc(has_version=False, other_versions=[("0.9", "REPLACED_WITH_NEW_VERSION"), ("1.0", state)])
                code, output = self.create(fake)
                self.assertEqual(code, 0, output)
                self.assertEqual([c[:2] for c in fake.writes()], [("versions", "create")])
        fake = FakeAsc(has_version=False, other_versions=[])
        code, output = self.create(fake)
        self.assertEqual(code, 0, output)
        self.assertEqual([c[:2] for c in fake.writes()], [("versions", "create")])

    def test_other_app_or_bad_version_fails_without_writing(self) -> None:
        for fake in (FakeAsc(bundle="com.example.other", has_version=False),
                     FakeAsc(record_id="1234567890", has_version=False)):
            code, output = self.create(fake)
            self.assertEqual(code, 1)
            self.assertIn("expected PCM Notes", output)
            self.assertEqual(fake.writes(), [])
        fake = FakeAsc(has_version=False)
        with mock.patch.object(asc_release, "find_app", return_value="1234567890"):
            code, output = self.create(fake)
        self.assertEqual(code, 1)
        self.assertIn(f"expected {APP}", output)
        self.assertEqual(fake.writes(), [])
        for bad in ("1.0.1-beta", "v1.0.1", "1.0.1.2", "١.٠"):
            with self.subTest(bad=bad):
                fake = FakeAsc(has_version=False)
                code, output = self.create(fake, version=bad)
                self.assertEqual(code, 1)
                self.assertIn("must look like", output)
                self.assertEqual(fake.calls, [])
        fake = FakeAsc(has_version=False)
        code, output = run_create_version(fake, self.ON)
        self.assertEqual(code, 1)
        self.assertIn("needs --version", output)
        self.assertEqual(fake.calls, [])

    def test_dry_run_makes_no_calls(self) -> None:
        fake = FakeAsc(has_version=False)
        code, output = run_create_version(fake, self.ON, "--dry-run", "--version", VERSION)
        self.assertEqual(code, 0, output)
        self.assertIn("would create", output)
        self.assertEqual(fake.calls, [])

    def test_dry_run_rejects_a_bad_version_like_a_live_run(self) -> None:
        for bad in ("1.0.1-beta", "v1.0.1", "1.0.1.2", "١.٠"):
            with self.subTest(bad=bad):
                fake = FakeAsc(has_version=False)
                code, output = run_create_version(fake, self.ON, "--dry-run", "--version", bad)
                self.assertEqual(code, 1)
                self.assertIn("must look like", output)
                self.assertNotIn("would create", output)
                self.assertEqual(fake.calls, [])


class CreativeAssetTests(LiveFlowCase):
    """IOS_UPLOAD_CREATIVE_ASSETS: product page header and search results images."""

    ENV = {"UPLOAD_CREATIVE_ASSETS": "true"}

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for folder, raw in (("header", b"header-image"), ("search-results", b"search-image")):
            (self.dir / folder).mkdir()
            (self.dir / folder / f"{folder}.png").write_bytes(raw)

    def image(self, image_id: str, folder: str, state: str) -> dict:
        path = self.dir / folder / f"{folder}.png"
        return {"id": image_id, "attributes": {"fileName": path.name, "fileSize": path.stat().st_size, "state": state,
                                               "createdDate": "2026-10-20"}}

    def run_creative(self, fake: FakeAsc) -> int:
        code, _ = self.run_main(fake, self.ENV, argv=["--version", VERSION, "--creative-dir", str(self.dir)])
        return code

    def test_new_images_are_uploaded_and_reviewed_on_their_own(self) -> None:
        fake = self.fake(drafts=())
        self.assertEqual(self.run_creative(fake), 0)
        uploads = [Path(flag(c, "--file")).name for c in fake.writes() if c[:3] == ("asset-library", "images", "upload")]
        self.assertEqual(uploads, ["header.png", "search-results.png"])
        review = fake.review_writes()
        self.assertEqual([c[:2] for c in review], [("review", "submissions-create"), ("review", "items-add"),
                                                   ("review", "items-add"), ("review", "submissions-submit")])
        self.assertEqual({flag(c, "--item-type") for c in review if c[:2] == ("review", "items-add")},
                         {"appAssetLibraryImages"})
        self.assertFalse([c for c in fake.writes() if c[:2] == ("localizations", "placements")])

    def test_images_in_review_are_neither_reuploaded_nor_placed(self) -> None:
        fake = self.fake(library=[self.image("H", "header", "WAITING_FOR_REVIEW"),
                                  self.image("S", "search-results", "IN_REVIEW")])
        self.assertEqual(self.run_creative(fake), 0)
        self.assertEqual(fake.writes(), [])

    def test_approved_images_are_placed_and_existing_ones_swapped(self) -> None:
        old = {"id": "PL-old", "attributes": {"placementType": "PRODUCT_PAGE_HEADER_ASSET"},
               "relationships": {"image": {"data": {"id": "OLDIMG"}}}}
        fake = self.fake(library=[self.image("H", "header", "APPROVED"), self.image("S", "search-results", "APPROVED")],
                         placements=[old])
        self.assertEqual(self.run_creative(fake), 0)
        swap = [c for c in fake.writes() if c[:3] == ("localizations", "placements", "swap")][0]
        self.assertEqual((flag(swap, "--image-id"), flag(swap, "--placement-id"), flag(swap, "--localization-id")),
                         ("H", "PL-old", "VL"))
        self.assertIn("--confirm", swap)
        create = [c for c in fake.writes() if c[:3] == ("localizations", "placements", "create")][0]
        self.assertEqual((flag(create, "--image-id"), flag(create, "--placement-type"), flag(create, "--placement-group")),
                         ("S", "APP_STORE_SEARCH_RESULTS_ASSET", "DEFAULT_PROFILE"))
        # A second run finds both placed and writes nothing.
        writes = len(fake.writes())
        self.assertEqual(self.run_creative(fake), 0)
        self.assertEqual(len(fake.writes()), writes)

    def test_a_live_version_takes_no_images(self) -> None:
        for state in ("READY_FOR_SALE", "IN_REVIEW"):
            fake = self.fake(state=state)
            with self.subTest(state=state):
                self.assertEqual(self.run_creative(fake), 1)
                self.assertIn(("versions", "list"), [c[:2] for c in fake.calls], "stopped by the version's state")
                self.assertEqual(fake.writes(), [])

    def test_an_image_with_alpha_fails_before_any_call(self) -> None:
        (self.dir / "header" / "header.png").write_bytes(png_bytes(3840, 1646, color_type=6))
        fake = self.fake()
        self.assertEqual(self.run_creative(fake), 1)
        self.assertEqual(fake.calls, [])

    def test_a_rejected_image_stops_the_run(self) -> None:
        fake = self.fake(library=[self.image("H", "header", "REJECTED")])
        self.assertEqual(self.run_creative(fake), 1)
        self.assertEqual(fake.writes(), [])

    def test_a_locked_version_takes_no_new_images(self) -> None:
        for state in ("WAITING_FOR_REVIEW", "READY_FOR_REVIEW"):
            fake = self.fake(state=state, attached="B9")
            with self.subTest(state=state):
                self.assertEqual(self.run_creative(fake), 1)
                self.assertEqual(fake.writes(), [])

    def test_a_locked_version_sends_nothing_to_review(self) -> None:
        for state in ("WAITING_FOR_REVIEW", "READY_FOR_REVIEW"):
            for image_state in ("UPLOAD_COMPLETE", "PREPARE_FOR_SUBMISSION"):
                fake = self.fake(state=state, attached="B9", library=[self.image("H", "header", image_state)])
                with self.subTest(state=state, image_state=image_state):
                    self.assertEqual(self.run_creative(fake), 1)
                    self.assertEqual(fake.writes(), [])

    def test_a_locked_version_passes_when_nothing_needs_to_change(self) -> None:
        placed = [{"id": f"PL-{kind}", "attributes": {"placementType": kind},
                   "relationships": {"image": {"data": {"id": image_id}}}}
                  for kind, image_id in (("PRODUCT_PAGE_HEADER_ASSET", "H"), ("APP_STORE_SEARCH_RESULTS_ASSET", "S"))]
        for state in ("WAITING_FOR_REVIEW", "READY_FOR_REVIEW"):
            fake = self.fake(state=state, attached="B9", placements=placed,
                             library=[self.image("H", "header", "APPROVED"), self.image("S", "search-results", "APPROVED")])
            with self.subTest(state=state):
                self.assertEqual(self.run_creative(fake), 0)
                self.assertEqual(fake.writes(), [])

    def test_a_retry_finishes_the_creative_draft_it_left_behind(self) -> None:
        library = [self.image("H", "header", "PREPARE_FOR_SUBMISSION"),
                   self.image("S", "search-results", "PREPARE_FOR_SUBMISSION")]
        fake = self.fake(library=library, drafts=("OLD",), draft_items={"OLD": [item("I-H", "appAssetLibraryImage", "H")]})
        self.assertEqual(self.run_creative(fake), 0)
        self.assertNotIn(("review", "submissions-create"), [c[:2] for c in fake.writes()])
        self.assertEqual(flag(fake.writes()[-1], "--id"), "OLD")
        # A draft holding anything else is left alone and a new one is used.
        fake = self.fake(library=library, drafts=("VERSION",),
                         draft_items={"VERSION": [item("X", "appStoreVersion", "V1")]})
        self.assertEqual(self.run_creative(fake), 0)
        self.assertEqual(flag(fake.writes()[-1], "--id"), "SUB-NEW")
        self.assertEqual(fake.items["VERSION"], [item("X", "appStoreVersion", "V1")])

    def test_images_and_the_version_go_to_review_in_separate_submissions(self) -> None:
        # An empty draft left by an earlier attempt is finished with the images;
        # the version then gets a draft of its own that holds only the version.
        fake = self.fake()
        code, _ = self.run_main(fake, {**self.ENV, "SUBMIT_IOS_REVIEW": "true"}, real_products=True,
                                argv=["--version", VERSION, "--build-id", "B9", "--build-number", "8",
                                      "--creative-dir", str(self.dir)])
        self.assertEqual(code, 0)
        submitted = [flag(c, "--id") for c in fake.writes() if c[:2] == ("review", "submissions-submit")]
        self.assertEqual(submitted, ["SUB1", "SUB-NEW"])
        self.assertEqual({rel for i in fake.items["SUB1"] for rel in i["relationships"]}, {"appAssetLibraryImage"})
        self.assertEqual(fake.items["SUB-NEW"], [item("ITEM-V1", "appStoreVersion", "V1")])

    def test_a_creative_submission_with_other_items_is_not_submitted(self) -> None:
        fake = self.fake(drafts=())
        fake_run = fake.run

        def run(*args, **kwargs):
            result = fake_run(*args, **kwargs)
            if args[:2] == ("review", "submissions-create"):
                fake.items[result["data"]["id"]] = [item("X", "appStoreVersion", "V1")]
            return result

        fake.run = run  # type: ignore[method-assign]
        self.assertEqual(self.run_creative(fake), 1)
        self.assertNotIn(("review", "submissions-submit"), [c[:2] for c in fake.writes()])


class ExportListingTests(unittest.TestCase):
    def test_exports_the_live_listing_read_only(self) -> None:
        fake = FakeAsc(live_whats_new="First edition.")
        factory = mock.Mock(return_value=fake)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(asc_release, "asc_from_environment", factory), \
                mock.patch.object(sys, "argv", ["export_listing.py", "--out", tmp]), redirect_stdout(io.StringIO()):
            export_listing.main()
            loc = Path(tmp) / "ios" / "en-US"
            self.assertEqual((loc / "name.txt").read_text(), APP_INFO_VALUES["name"] + "\n")
            self.assertEqual((loc / "description.txt").read_text(), LISTING_VALUES["description"] + "\n")
            self.assertEqual((loc / "whats_new.txt").read_text(), "First edition.\n")
        factory.assert_called_once_with(read_only=True)
        self.assertEqual(fake.writes(), [])
        info = [c for c in fake.calls if c[:2] == ("localizations", "list") and "--app-info" in c][0]
        self.assertEqual(flag(info, "--app-info"), "INFO-LIVE")
        version = [c for c in fake.calls if c[:2] == ("localizations", "list") and "--version" in c][0]
        self.assertEqual(flag(version, "--version"), "V0")


class RedactLogsTests(unittest.TestCase):
    KEY = "AB12CD34EF"
    ISSUER = "69a6de7e-0b7f-47e3-e053-5b8c7c11a4d1"

    def run_redaction(self, root: Path) -> None:
        env = {"ASC_KEY_ID": self.KEY, "ASC_ISSUER_ID": self.ISSUER}
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(io.StringIO()):
            redact_logs.main([str(root)])

    def test_ids_are_replaced_in_text_and_unredactable_files_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "PCMNotes.xcdistributionlogs"
            logs.mkdir()
            (root / "export.log").write_text(
                f"-authenticationKeyID {self.KEY} -authenticationKeyIssuerID {self.ISSUER.upper()}\n"
                f"key path AuthKey_{self.KEY}.p8\n")
            (logs / "options.plist").write_bytes(b"bplist00\x00\x01" + self.KEY.encode() + b"\x00")
            (logs / "utf16.log").write_bytes(f"issuer {self.ISSUER}".encode("utf-16"))
            (logs / "clean.bin").write_bytes(b"\x00\x01binary without ids")
            (logs / f"AuthKey_{self.KEY}.p8").write_text("copied key")
            (root / f"{self.ISSUER}-dir").mkdir()
            (root / f"{self.ISSUER}-dir" / "inside.log").write_text("hidden by its folder name")
            (root / "link.log").symlink_to(root / "export.log")
            (root / "link-dir").symlink_to(logs, target_is_directory=True)
            self.run_redaction(root)
            text = (root / "export.log").read_text()
            self.assertEqual(text, "-authenticationKeyID <ASC_KEY_ID> -authenticationKeyIssuerID <ASC_ISSUER_ID>\n"
                                   "key path AuthKey_<ASC_KEY_ID>.p8\n")
            kept = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            self.assertEqual(kept, ["PCMNotes.xcdistributionlogs", "PCMNotes.xcdistributionlogs/clean.bin",
                                    "export.log"])
            forms = redact_logs.encoded_forms([self.KEY, self.ISSUER])
            for path in root.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    self.assertFalse([form for form in forms if form in data], path)

    def test_without_ids_nothing_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.log").write_text(f"key {self.KEY}")
            with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
                redact_logs.main([str(root)])
            self.assertEqual((root / "a.log").read_text(), f"key {self.KEY}")

    def test_a_missing_folder_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_redaction(Path(tmp) / "missing")


class ReleaseNotesTests(unittest.TestCase):
    TEXT = "# Release notes\n\n## v1.0.2\n\n- Faster search\n\n## v1.0.1\n\n## v1.0.0\n\nFirst edition.\n"

    def test_matching_section_is_used(self) -> None:
        self.assertEqual(release_notes.section_for(self.TEXT, "v1.0.2"), "- Faster search")
        self.assertEqual(release_notes.section_for(self.TEXT, "v1.0.0"), "First edition.")

    def test_missing_section_falls_back_to_generated_notes(self) -> None:
        self.assertIsNone(release_notes.section_for(self.TEXT, "v1.0.3"))

    def test_cli_without_a_section_succeeds_with_empty_notes(self) -> None:
        # The release still gets a GitHub Release (--generate-notes) when the tag
        # has no section, so the pre-build check must not fail on it.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            release_notes, "NOTES", Path(tmp) / "RELEASE_NOTES.md"
        ):
            release_notes.NOTES.write_text(self.TEXT, encoding="utf-8")
            out = Path(tmp) / "notes.md"
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                release_notes.main(["v1.0.3"])
                release_notes.main(["v1.0.3", "--out", str(out)])
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("generated notes", stderr.getvalue())
            self.assertEqual(out.read_text(encoding="utf-8"), "")

    def test_empty_section_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            release_notes.section_for(self.TEXT, "v1.0.1")

    def test_duplicate_sections_are_an_error(self) -> None:
        text = self.TEXT + "\n## v1.0.2\n\n- Something else\n"
        with self.assertRaisesRegex(ValueError, "2 ## v1.0.2 sections"):
            release_notes.section_for(text, "v1.0.2")
        # Other tags are unaffected by a duplicate elsewhere.
        self.assertEqual(release_notes.section_for(text, "v1.0.0"), "First edition.")

    def test_missing_notes_file_means_generated_notes(self) -> None:
        self.assertIsNone(release_notes.section_for(None, "v1.0.2"))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            release_notes, "NOTES", Path(tmp) / "RELEASE_NOTES.md"
        ):
            out = Path(tmp) / "notes.md"
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                release_notes.main(["v1.0.2"])
                release_notes.main(["v1.0.2", "--out", str(out)])
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("does not exist", stderr.getvalue())
            self.assertEqual(out.read_text(encoding="utf-8"), "")

    def test_cli_fails_on_duplicate_or_empty_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            release_notes, "NOTES", Path(tmp) / "RELEASE_NOTES.md"
        ):
            release_notes.NOTES.write_text(self.TEXT + "\n## v1.0.2\n\nAgain\n", encoding="utf-8")
            for tag in ("v1.0.2", "v1.0.1"):
                with self.subTest(tag=tag), self.assertRaises(SystemExit) as raised:
                    quiet(release_notes.main, [tag])
                self.assertEqual(raised.exception.code, 2)

    def test_repository_notes_parse(self) -> None:
        # Read the file the way the release does: a missing RELEASE_NOTES.md means
        # GitHub's generated notes, so it must not fail the offline tests that the
        # iOS Release workflow runs before building.
        text = release_notes.read_notes()
        if text is None:
            self.skipTest("RELEASE_NOTES.md does not exist; releases use generated notes")
        self.assertIsNone(release_notes.section_for(text, "v0.0.0-not-a-release"))
        titles = [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(len(titles), len(set(titles)), "RELEASE_NOTES.md repeats a section")

    def run_repository_check(self, text: str | None) -> unittest.TestResult:
        """Run test_repository_notes_parse on `text` (None: no RELEASE_NOTES.md)."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            release_notes, "NOTES", Path(tmp) / "RELEASE_NOTES.md"
        ):
            if text is not None:
                release_notes.NOTES.write_text(text, encoding="utf-8")
            result = unittest.TestResult()
            ReleaseNotesTests("test_repository_notes_parse").run(result)
        return result

    def test_repository_check_skips_a_missing_notes_file(self) -> None:
        # Before, the check read the file unconditionally, so without it the
        # release's offline tests failed, and with them the build and the GitHub
        # Release, although a missing file only means generated notes.
        result = self.run_repository_check(None)
        self.assertEqual((result.testsRun, result.errors, result.failures), (1, [], []))
        self.assertEqual(len(result.skipped), 1)

    def test_repository_check_still_rejects_a_repeated_section(self) -> None:
        result = self.run_repository_check(self.TEXT + "\n## v1.0.0\n\nAgain.\n")
        self.assertEqual((result.testsRun, result.errors), (1, []))
        self.assertEqual(len(result.failures), 1)
        self.assertIn("repeats a section", result.failures[0][1])


class PrintGatesTests(unittest.TestCase):
    def printed(self, env: dict[str, str]) -> str:
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out):
            print_gates.main()
        return out.getvalue()

    def test_values_read_the_way_the_release_reads_them(self) -> None:
        output = self.printed({"IOS_RELEASE_AUTOMATION": " Yes ", "IOS_UPLOAD_BUILD": "off",
                               "IOS_BUILD_POLL_SECONDS": "45"})
        self.assertIn("IOS_RELEASE_AUTOMATION=true", output)
        self.assertIn("IOS_UPLOAD_BUILD=false\n", output)
        self.assertIn("IOS_UPLOAD_LISTING=false (unset)", output)
        self.assertIn("IOS_CREATE_VERSION=false (unset)", output)
        self.assertIn("IOS_UPLOAD_CREATIVE_ASSETS=false (unset)", output)
        self.assertIn("IOS_BUILD_POLL_SECONDS=45", output)
        self.assertIn("IOS_BUILD_WAIT_MINUTES=120 (default)", output)
        self.assertIn("every store operation is off", output)

    def test_invalid_values_are_shown_as_invalid(self) -> None:
        # Before, "maybe" was echoed like a value and an invalid master switch
        # printed "off"; the release run rejects both.
        output = self.printed({"IOS_RELEASE_AUTOMATION": "maybe", "IOS_SUBMIT_REVIEW": "enabled",
                               "IOS_BUILD_WAIT_MINUTES": "999", "IOS_UPLOAD_CREATIVE_ASSETS": "sometimes"})
        self.assertIn("IOS_RELEASE_AUTOMATION=INVALID 'maybe'", output)
        self.assertIn("IOS_SUBMIT_REVIEW=INVALID 'enabled'", output)
        self.assertIn("IOS_BUILD_WAIT_MINUTES=INVALID '999'", output)
        self.assertIn("IOS_UPLOAD_CREATIVE_ASSETS=INVALID 'sometimes'", output)
        self.assertIn("every release run fails", output)
        self.assertNotIn("is off, so every iOS store operation is skipped", output)

    def test_every_value_print_gates_accepts_is_accepted_by_the_release(self) -> None:
        for raw in sorted(ios_release.TRUE_VALUES | ios_release.FALSE_VALUES) + ["maybe", "2"]:
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"X": raw}, clear=True):
                shown, on = print_gates.describe("X")
                try:
                    expected = ios_release.parse_switch("X", raw)
                except ios_release.ReleaseError:
                    self.assertTrue(shown.startswith("INVALID"))
                else:
                    self.assertEqual(on, expected)
                    self.assertFalse(shown.startswith("INVALID"))

    def test_live_operations_are_called_out(self) -> None:
        output = self.printed({"IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_BUILD": "true"})
        self.assertIn("WARNING: live App Store operations are on: IOS_UPLOAD_BUILD", output)
        output = self.printed({"IOS_RELEASE_AUTOMATION": "true", "IOS_CREATE_VERSION": "true"})
        self.assertIn("WARNING: live App Store operations are on: IOS_CREATE_VERSION", output)
        output = self.printed({"IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_CREATIVE_ASSETS": "true"})
        self.assertIn("WARNING: live App Store operations are on: IOS_UPLOAD_CREATIVE_ASSETS", output)


class CatalogTests(unittest.TestCase):
    def test_catalog_matches_the_app_and_storekit_configuration(self) -> None:
        quiet(validate_catalog.main)

    def test_catalog_drift_is_caught(self) -> None:
        catalog = json.loads((ROOT / "store/catalog/products.json").read_text())
        catalog["tips"] = catalog["tips"][:3]
        with self.assertRaises(SystemExit):
            quiet(validate_catalog.check, catalog)


if __name__ == "__main__":
    unittest.main()
