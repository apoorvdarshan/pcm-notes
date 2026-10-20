#!/usr/bin/env python3
"""Offline tests for iOS release switches, build waiting, listing and build selection.

No network: every App Store Connect call goes to an in-memory fake.
Run: python3 -m unittest scripts/store/test_ios_release.py
"""
from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import plistlib
import sys
import tempfile
import unittest
import urllib.parse
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import unittest.mock as mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import asc_release  # noqa: E402
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
    "IOS_SUBMIT_REVIEW": "true",
    "IOS_PUBLIC_RELEASE": "true",
}
OPS = ("create_version", "upload_build", "listing", "whats_new", "screenshots", "submit", "public_release")
FLAG_NAMES = {"UPLOAD_LISTING", "UPLOAD_WHATS_NEW", "UPLOAD_SCREENSHOTS", "UPLOAD_CREATIVE_ASSETS",
              "SUBMIT_IOS_REVIEW", "PUBLIC_RELEASE", "CREATE_IOS_VERSION"}


def make_png(width: int, height: int, colour_type: int = 2, *, chunks: tuple[tuple[bytes, bytes], ...] = ()) -> bytes:
    """A valid PNG of black pixels (8 bits per sample), built without Pillow.

    chunks are extra (type, data) chunks written before the image data, e.g. tRNS.
    """
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour_type]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, colour_type, 0, 0, 0])
    extra = b"".join(chunk(kind, data) for kind, data in chunks)
    palette = chunk(b"PLTE", b"\x00\x00\x00") if colour_type == 3 else b""
    pixels = zlib.compress((b"\x00" + b"\x00" * (width * channels)) * height)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + palette + extra
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


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
                     "IOS_UPLOAD_SCREENSHOTS"):
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
            ios_release.resolve_gates({}, event="push", ref_name=TAG, marketing_version=VERSION, validation_version="1.0.2")
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
        for env, builds in ((validation, True), (upload, True), (no_build, False), ({**no_build, "IOS_RELEASE_AUTOMATION": ""}, False)):
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


APP = asc_release.APP_STORE_APP_ID
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


class FakeClock:
    def __init__(self, start: float) -> None:
        self.time = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds


class SequenceClient:
    """Returns queued /builds payloads (or raises queued exceptions) per GET."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.paths: list[str] = []

    def get(self, path: str) -> dict:
        self.paths.append(path)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return item


def wait(client: SequenceClient, clock: FakeClock, *, minutes: int = 120, transient=None) -> dict:
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
        client = SequenceClient(
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
        self.assertIn("filter%5Bversion%5D=8", client.paths[0])
        self.assertIn(f"filter%5Bapp%5D={APP}", client.paths[0])

    def test_times_out_without_using_another_build(self) -> None:
        client = SequenceClient([{"data": [build_row(state="PROCESSING")], "included": [pre_row()]}])
        clock = FakeClock(STARTED)
        with self.assertRaisesRegex(ios_release.ReleaseError, "timed out"):
            wait(client, clock, minutes=10)
        self.assertLessEqual(clock.time, STARTED + 10 * 60 + 30)

    def test_ready_build_after_the_deadline_is_still_a_timeout(self) -> None:
        client = SequenceClient([{"data": [build_row()], "included": [pre_row()]}])
        clock = FakeClock(STARTED + 121 * 60)  # job started after the 120-minute budget
        with self.assertRaisesRegex(ios_release.ReleaseError, "timed out"):
            wait(client, clock)
        self.assertEqual(client.paths, [], "must not poll once the budget is spent")

    def test_processing_failure_stops(self) -> None:
        for state in ("FAILED", "INVALID"):
            client = SequenceClient([{"data": [build_row(state=state)], "included": [pre_row()]}])
            with self.subTest(state=state), self.assertRaisesRegex(ios_release.ReleaseError, state):
                wait(client, FakeClock(STARTED))

    def test_expired_build_is_rejected(self) -> None:
        client = SequenceClient([{"data": [build_row(expired=True)], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "expired"):
            wait(client, FakeClock(STARTED))

    def test_ambiguous_match_stops(self) -> None:
        client = SequenceClient(
            [{"data": [build_row("B1"), build_row("B2")], "included": [pre_row()]}]
        )
        with self.assertRaisesRegex(ios_release.ReleaseError, "ambiguous"):
            wait(client, FakeClock(STARTED))

    def test_older_upload_is_never_substituted(self) -> None:
        old = build_row(uploaded=STARTED - 3600)
        client = SequenceClient([{"data": [old], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "older build"):
            wait(client, FakeClock(STARTED))

    def test_a_build_without_an_upload_date_is_never_used(self) -> None:
        # The "uploaded by this run" guard fails closed: a VALID build whose upload
        # date is missing is waited on, never returned, and times out.
        for missing in (None, ""):
            client = SequenceClient([{"data": [build_row(uploaded=missing)], "included": [pre_row()]}])
            clock = FakeClock(STARTED)
            with self.subTest(uploaded=missing), self.assertRaisesRegex(ios_release.ReleaseError, "timed out"):
                wait(client, clock, minutes=10)
            self.assertGreater(len(client.paths), 1, "it keeps polling until the deadline")

    def test_a_build_gets_used_once_its_upload_date_appears(self) -> None:
        client = SequenceClient([
            {"data": [build_row(uploaded=None)], "included": [pre_row()]},
            {"data": [build_row()], "included": [pre_row()]},
        ])
        self.assertEqual(wait(client, FakeClock(STARTED))["id"], "B1")

    def test_an_unreadable_upload_date_stops(self) -> None:
        client = SequenceClient([{"data": [build_row(uploaded="yesterday")], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "unreadable upload date"):
            wait(client, FakeClock(STARTED))

    def test_failed_processing_stops_even_without_an_upload_date(self) -> None:
        client = SequenceClient([{"data": [build_row(state="FAILED", uploaded=None)], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "FAILED"):
            wait(client, FakeClock(STARTED))

    def test_other_versions_and_platforms_are_ignored(self) -> None:
        other_version = build_row("BX", pre="P2")
        other_platform = build_row("BY", pre="P3")
        client = SequenceClient(
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
        client = SequenceClient([{"data": [build_row(app="OTHER")], "included": [pre_row()]}])
        with self.assertRaisesRegex(ios_release.ReleaseError, "another app"):
            wait(client, FakeClock(STARTED))

    def test_transient_errors_are_retried_and_others_raise(self) -> None:
        transient = asc_release.AscHttpError(503, "unavailable")
        client = SequenceClient([transient, {"data": [build_row()], "included": [pre_row()]}])
        with redirect_stderr(io.StringIO()):
            build = wait(client, FakeClock(STARTED), transient=lambda exc: getattr(exc, "status", 0) == 503)
        self.assertEqual(build["id"], "B1")
        fatal = SequenceClient([asc_release.AscHttpError(401, "unauthorized")])
        with self.assertRaises(SystemExit):
            wait(fatal, FakeClock(STARTED), transient=lambda exc: getattr(exc, "status", 0) == 503)


class SelectionClient:
    """Fake ASC client for build verification and selection."""

    def __init__(self, *, build: dict, included: list, attached: str | None, sticky: bool = True) -> None:
        self.build = build
        self.included = included
        self.attached = attached
        self.sticky = sticky
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path, None))
        if path.startswith(f"/builds/{self.build['id']}"):
            return {"data": self.build, "included": self.included}
        if path.endswith("/build"):
            return {"data": {"type": "builds", "id": self.attached} if self.attached else None}
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path: str, body: dict) -> dict:
        self.calls.append(("PATCH", path, body))
        return {"data": body["data"]}

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path, body))
        if method == "PATCH" and path.endswith("/relationships/build") and self.sticky:
            self.attached = body["data"]["id"]
        return None


class BuildSelectionTests(unittest.TestCase):
    def test_selects_and_reads_back_this_runs_build(self) -> None:
        client = SelectionClient(build=build_row("B9"), included=[pre_row()], attached="OLD")
        quiet(asc_release.select_build, client, "V1", "B9", VERSION, "8")
        self.assertEqual(client.attached, "B9")
        methods = [(m, p) for m, p, _ in client.calls]
        self.assertIn(("PATCH", "/appStoreVersions/V1/relationships/build"), methods)
        self.assertEqual(methods[-1], ("GET", "/appStoreVersions/V1/build"))

    def test_selection_that_does_not_stick_fails(self) -> None:
        client = SelectionClient(build=build_row("B9"), included=[pre_row()], attached="OLD", sticky=False)
        with self.assertRaises(SystemExit):
            quiet(asc_release.select_build, client, "V1", "B9", VERSION, "8")

    def test_unprocessed_expired_or_wrong_build_is_refused(self) -> None:
        cases = {
            "processing": (build_row("B9", state="PROCESSING"), [pre_row()]),
            "expired": (build_row("B9", expired=True), [pre_row()]),
            "number": (build_row("B9", number="9"), [pre_row()]),
            "version": (build_row("B9"), [pre_row(version="1.0")]),
            "platform": (build_row("B9"), [pre_row(platform="TV_OS")]),
        }
        for label, (build, included) in cases.items():
            client = SelectionClient(build=build, included=included, attached=None)
            with self.subTest(label), self.assertRaises(SystemExit):
                quiet(asc_release.select_build, client, "V1", "B9", VERSION, "8")
            self.assertFalse([c for c in client.calls if c[0] == "PATCH"], label)

    def test_an_already_selected_build_is_not_selected_again(self) -> None:
        # A rerun finds its own build selected (for example in a draft review
        # submission) and must not write the selection again.
        client = SelectionClient(build=build_row("B9"), included=[pre_row()], attached="B9")
        quiet(asc_release.select_build, client, "V1", "B9", VERSION, "8")
        self.assertFalse([c for c in client.calls if c[0] == "PATCH"])

    def test_release_type(self) -> None:
        for public, expected in ((True, "AFTER_APPROVAL"), (False, "MANUAL")):
            client = SelectionClient(build=build_row(), included=[], attached=None)
            quiet(asc_release.set_release_type, client, "V1", public_release=public)
            body = client.calls[-1][2]
            self.assertEqual(body["data"]["attributes"]["releaseType"], expected)

    def test_release_type_already_set_is_left_alone(self) -> None:
        client = SelectionClient(build=build_row(), included=[], attached=None)
        quiet(asc_release.set_release_type, client, "V1", public_release=False, current="MANUAL")
        self.assertEqual(client.calls, [])


class TokenRefreshTests(unittest.TestCase):
    def test_token_is_reminted_during_long_waits(self) -> None:
        minted: list[str] = []

        def factory() -> str:
            minted.append(f"t{len(minted)}")
            return minted[-1]

        with mock.patch.object(asc_release.time, "monotonic", side_effect=[0, 100, 700, 700, 800]):
            client = asc_release.AscClient(token_factory=factory)
            self.assertEqual(client._authorization(), "Bearer t0")
            self.assertEqual(client._authorization(), "Bearer t1")
            self.assertEqual(client._authorization(), "Bearer t1")

    def test_minted_token_is_an_es256_app_store_connect_jwt(self) -> None:
        try:
            import jwt
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError:  # pragma: no cover - CI installs requirements-store.txt
            raise unittest.SkipTest(
                "PyJWT[crypto] is not installed (pip install --require-hashes -r requirements-store.txt)"
            ) from None
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        token = asc_release.make_asc_token("KEY123", "issuer-uuid", pem)
        header = jwt.get_unverified_header(token)
        self.assertEqual((header["alg"], header["kid"], header["typ"]), ("ES256", "KEY123", "JWT"))
        claims = jwt.decode(token, key.public_key(), algorithms=["ES256"], audience="appstoreconnect-v1")
        self.assertEqual(claims["iss"], "issuer-uuid")
        self.assertLessEqual(claims["exp"] - claims["iat"], 1200)  # Apple's 20-minute maximum

    def test_escaped_newlines_in_the_secret_are_restored(self) -> None:
        env = {
            "APP_STORE_CONNECT_API_KEY_ID": "KEY123",
            "APP_STORE_CONNECT_ISSUER_ID": "issuer",
            "APP_STORE_CONNECT_API_KEY_P8": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            _, _, key = asc_release.load_credentials()
        self.assertEqual(key.count("\n"), 2)

    def test_missing_credentials_fail(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            quiet(asc_release.load_credentials)


class FindAppTests(unittest.TestCase):
    def client(self, rows: list[dict]) -> mock.Mock:
        client = mock.Mock()
        client.get.return_value = {"data": rows}
        return client

    def test_returns_the_pinned_pcm_notes_app(self) -> None:
        rows = [
            {"id": "999", "attributes": {"bundleId": "com.apoorvdarshan.iitjee.extra"}},
            {"id": APP, "attributes": {"bundleId": "com.apoorvdarshan.iitjee"}},
        ]
        self.assertEqual(asc_release.find_app(self.client(rows), "com.apoorvdarshan.iitjee"), APP)

    def test_wrong_ambiguous_or_missing_app_stops(self) -> None:
        cases = {
            "other id": [{"id": "123", "attributes": {"bundleId": "com.apoorvdarshan.iitjee"}}],
            "ambiguous": [
                {"id": APP, "attributes": {"bundleId": "com.apoorvdarshan.iitjee"}},
                {"id": "124", "attributes": {"bundleId": "com.apoorvdarshan.iitjee"}},
            ],
            "missing": [],
            "prefix only": [{"id": APP, "attributes": {"bundleId": "com.apoorvdarshan.iitjeex"}}],
        }
        for label, rows in cases.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                quiet(asc_release.find_app, self.client(rows), "com.apoorvdarshan.iitjee")


class PreflightTests(unittest.TestCase):
    def problems(self, state: str | None, *, text: bool = True, screenshots: bool = False,
                 submit: bool = False, missing_locales: tuple[str, ...] = ()) -> list[str]:
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

    def test_prepared_version_passes(self) -> None:
        for state in ("PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED"):
            with self.subTest(state=state):
                self.assertEqual(self.problems(state, screenshots=True, submit=True), [])

    def test_a_version_in_a_draft_takes_text_but_not_screenshots(self) -> None:
        # READY_FOR_REVIEW (left in a draft review submission by a failed submit):
        # Apple still accepts text edits and the submit, not new screenshots.
        self.assertEqual(self.problems("READY_FOR_REVIEW", submit=True), [])
        self.assertRegex(" ".join(self.problems("READY_FOR_REVIEW", text=False, screenshots=True)),
                         "screenshots cannot be replaced")

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

    def preflight_client(self, state: str, locales: set[str]) -> mock.Mock:
        def get(path: str) -> dict:
            if path.startswith("/apps?"):
                return {"data": [{"id": APP, "attributes": {"bundleId": ios_release.BUNDLE_ID}}]}
            if path.startswith(f"/apps/{APP}/appStoreVersions"):
                return {"data": [{"id": "V1", "attributes": {"versionString": VERSION, "appStoreState": state}}]}
            if path.startswith("/appStoreVersions/V1/appStoreVersionLocalizations?"):
                locale = urllib.parse.parse_qs(path.split("?", 1)[1])["filter[locale]"][0]
                return {"data": [{"id": f"L-{locale}", "attributes": {"locale": locale}}] if locale in locales else []}
            raise AssertionError(f"unexpected GET {path}")

        client = mock.Mock()
        client.get.side_effect = get
        return client

    def run_preflight(self, client: mock.Mock, *flags: str) -> tuple[int, str]:
        out = io.StringIO()
        code = 0
        with mock.patch.object(ios_release, "asc_client", return_value=(asc_release, client)), \
                redirect_stdout(out), redirect_stderr(out):
            try:
                ios_release.main(["preflight", "--version", VERSION, *flags])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        client.patch.assert_not_called()
        client.post.assert_not_called()
        client.request.assert_not_called()
        return code, out.getvalue()

    def test_cli_is_read_only_and_reports_problems(self) -> None:
        code, output = self.run_preflight(self.preflight_client("READY_FOR_SALE", {"en-US"}), "--listing")
        self.assertEqual(code, 1)
        self.assertIn("cannot be edited", output)

    def test_cli_checks_every_repository_locale(self) -> None:
        client = self.preflight_client("PREPARE_FOR_SUBMISSION", {"en-US"})
        code, output = self.run_preflight(client, "--whats-new", "--screenshots", "--submit")
        self.assertEqual(code, 0, output)
        self.assertIn("can take this release", output)
        client = self.preflight_client("PREPARE_FOR_SUBMISSION", set())
        code, output = self.run_preflight(client, "--listing")
        self.assertEqual(code, 1)
        self.assertIn("no en-US localization", output)

    def test_cli_submit_only_skips_the_locale_lookup(self) -> None:
        client = self.preflight_client("PREPARE_FOR_SUBMISSION", set())
        code, output = self.run_preflight(client, "--submit")
        self.assertEqual(code, 0, output)
        looked_up = [c.args[0] for c in client.get.call_args_list]
        self.assertFalse([p for p in looked_up if "Localizations" in p])


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
    ), redirect_stdout(out), redirect_stderr(out):
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

    def test_listing_excludes_whats_new_unless_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loc = Path(tmp) / "ios" / "en-US"
            loc.mkdir(parents=True)
            (loc / "description.txt").write_text("Desc")
            (loc / "whats_new.txt").write_text("New")
            for whats_new, expected in ((False, {"description"}), (True, {"description", "whatsNew"})):
                client = mock.Mock()
                client.get.return_value = {"data": [{"id": "LOC", "attributes": {"locale": "en-US"}}]}
                with mock.patch.object(asc_release, "upload_app_info"), \
                        mock.patch.object(asc_release, "check_whats_new_is_new"):
                    quiet(
                        asc_release.upload_listing,
                        client,
                        app_id=APP,
                        version_id="V1",
                        locale="en-US",
                        metadata_dir=Path(tmp),
                        include_listing=True,
                        include_whats_new=whats_new,
                    )
                attrs = client.patch.call_args[0][1]["data"]["attributes"]
                self.assertEqual(set(attrs), expected)



LISTING_VALUES = {
    "description": "Handwritten notes.",
    "keywords": "jee,neet",
    "promotionalText": "Promo",
    "marketingUrl": "https://pcm-notes.apoorvdarshan.com/",
    "supportUrl": "https://pcm-notes.apoorvdarshan.com/#support",
}


class ListingSyncTests(unittest.TestCase):
    """Version localization: only differences are sent; a frozen version only compares."""

    def sync(self, current: dict[str, str], *, frozen_state: str | None = None,
             client: mock.Mock | None = None) -> mock.Mock:
        client = client or mock.Mock()
        client.get.return_value = {"data": [{"id": "LOC", "attributes": {"locale": "en-US", **current}}]}
        with tempfile.TemporaryDirectory() as tmp:
            write_listing(Path(tmp))
            with mock.patch.object(asc_release, "upload_app_info") as app_info:
                quiet(asc_release.upload_listing, client, app_id=APP, version_id="V1", locale="en-US",
                      metadata_dir=Path(tmp), include_listing=True, frozen_state=frozen_state)
        app_info.assert_called_once()
        self.assertEqual(app_info.call_args.kwargs["frozen_state"], frozen_state)
        return client

    def test_only_changed_fields_are_sent(self) -> None:
        client = self.sync({**LISTING_VALUES, "keywords": "jee"})
        client.patch.assert_called_once()
        self.assertEqual(client.patch.call_args[0][1]["data"]["attributes"], {"keywords": "jee,neet"})

    def test_matching_text_is_not_sent_again(self) -> None:
        stored = {**LISTING_VALUES, "description": "Handwritten notes.\r\n"}
        self.sync(stored).patch.assert_not_called()

    def test_a_frozen_version_passes_when_it_already_matches(self) -> None:
        self.sync(LISTING_VALUES, frozen_state="WAITING_FOR_REVIEW").patch.assert_not_called()

    def test_a_frozen_version_stops_on_a_difference_without_writing(self) -> None:
        client = mock.Mock()
        with self.assertRaises(SystemExit):
            self.sync({**LISTING_VALUES, "promotionalText": "Old"}, frozen_state="WAITING_FOR_REVIEW",
                      client=client)
        client.patch.assert_not_called()


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

    def test_the_repository_creative_folders_are_ready(self) -> None:
        # Both slot folders exist. PCM Notes has no header or search results image
        # yet, so with IOS_UPLOAD_CREATIVE_ASSETS on the run stops before the build;
        # an image added later must pass the local checks.
        images = []
        for folder, _, _, _ in asc_release.CREATIVE_SLOTS:
            slot = asc_release.DEFAULT_CREATIVE_DIR / folder
            self.assertTrue(slot.is_dir(), slot)
            images += [p for p in slot.iterdir() if p.suffix.lower() in asc_release.CREATIVE_EXTENSIONS]
        if images:
            quiet(asc_release.creative_sources, asc_release.DEFAULT_CREATIVE_DIR)
        else:
            with self.assertRaises(SystemExit):
                quiet(asc_release.creative_sources, asc_release.DEFAULT_CREATIVE_DIR)


class AppInfoClient:
    """Fake ASC client for app information (name, subtitle, privacy URL)."""

    def __init__(self, *, editable: bool, current: dict[str, str], next_state: str = "PREPARE_FOR_SUBMISSION",
                 next_first: bool = False, live: dict[str, str] | None = None) -> None:
        self.infos = [{"id": "INFO-LIVE", "attributes": {"state": "READY_FOR_DISTRIBUTION"}}]
        if editable:
            pending = {"id": "INFO-NEXT", "attributes": {"state": next_state}}
            self.infos.insert(0 if next_first else 1, pending)
        self.current = current
        self.live = live  # the live copy's values when they differ from `current`
        self.patches: list[tuple[str, dict]] = []

    def get(self, path: str) -> dict:
        if path == f"/apps/{APP}/appInfos":
            return {"data": self.infos}
        if path.startswith("/appInfos/"):
            info_id = path.split("/")[2]
            values = self.live if info_id == "INFO-LIVE" and self.live is not None else self.current
            return {"data": [{"id": f"LOC-{info_id}", "attributes": {"locale": "en-US", **values}}]}
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path: str, body: dict) -> dict:
        self.patches.append((path, body))
        return body


LIVE_INFO = {
    "name": "PCM Notes: JEE & NEET",
    "subtitle": "Handwritten Exam Revision",
    "privacyPolicyUrl": "https://pcm-notes.apoorvdarshan.com/privacy",
}


class AppInfoTests(unittest.TestCase):
    def upload(self, client: AppInfoClient, frozen_state: str | None = None, **overrides: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_listing(Path(tmp), **overrides)
            quiet(asc_release.upload_app_info, client, app_id=APP, locale="en-US", metadata_dir=Path(tmp),
                  frozen_state=frozen_state)

    def test_a_version_in_a_draft_still_takes_app_information(self) -> None:
        client = AppInfoClient(editable=True, current=LIVE_INFO, next_state="READY_FOR_REVIEW")
        self.upload(client, **{"subtitle.txt": "Offline JEE & NEET Notes"})
        self.assertEqual([path for path, _ in client.patches], ["/appInfoLocalizations/LOC-INFO-NEXT"])

    def test_the_new_versions_copy_is_compared_whatever_the_order(self) -> None:
        # Waiting for review, the in-flight copy (not the live one) holds this
        # release's values; a rerun that already sent them passes untouched.
        wanted = {**LIVE_INFO, "subtitle": "Offline JEE & NEET Notes"}
        for next_first in (True, False):
            client = AppInfoClient(editable=True, current=wanted, next_state="WAITING_FOR_REVIEW",
                                   next_first=next_first, live=LIVE_INFO)
            with self.subTest(next_first=next_first):
                self.upload(client, frozen_state="WAITING_FOR_REVIEW",
                            **{"subtitle.txt": "Offline JEE & NEET Notes"})
                self.assertEqual(client.patches, [])

    def test_a_frozen_version_refuses_changes_without_writing(self) -> None:
        client = AppInfoClient(editable=True, current=LIVE_INFO, next_state="WAITING_FOR_REVIEW")
        with self.assertRaises(SystemExit):
            self.upload(client, frozen_state="WAITING_FOR_REVIEW", **{"name.txt": "PCM Notes"})
        self.assertEqual(client.patches, [])

    def test_unchanged_app_information_is_not_patched(self) -> None:
        client = AppInfoClient(editable=True, current=LIVE_INFO)
        self.upload(client)
        self.assertEqual(client.patches, [])

    def test_only_changed_fields_go_to_the_editable_copy(self) -> None:
        client = AppInfoClient(editable=True, current=LIVE_INFO)
        self.upload(client, **{"subtitle.txt": "Offline JEE & NEET Notes"})
        self.assertEqual(len(client.patches), 1)
        path, body = client.patches[0]
        self.assertEqual(path, "/appInfoLocalizations/LOC-INFO-NEXT")
        self.assertEqual(body["data"]["attributes"], {"subtitle": "Offline JEE & NEET Notes"})

    def test_changes_without_an_editable_copy_stop(self) -> None:
        client = AppInfoClient(editable=False, current=LIVE_INFO)
        with self.assertRaises(SystemExit):
            self.upload(client, **{"name.txt": "PCM Notes"})
        self.assertEqual(client.patches, [])
        self.upload(AppInfoClient(editable=False, current=LIVE_INFO))  # unchanged is fine


class WhatsNewClient:
    def __init__(self, live_whats_new: str | None) -> None:
        self.live_whats_new = live_whats_new

    def get(self, path: str) -> dict:
        if path.startswith(f"/apps/{APP}/appStoreVersions"):
            return {"data": [
                {"id": "V-LIVE", "attributes": {"versionString": "1.0", "appStoreState": "READY_FOR_SALE"}},
                {"id": "V-NEXT", "attributes": {"versionString": VERSION, "appStoreState": "PREPARE_FOR_SUBMISSION"}},
            ]}
        if path.startswith("/appStoreVersions/V-LIVE/appStoreVersionLocalizations"):
            return {"data": [{"id": "L", "attributes": {"locale": "en-US", "whatsNew": self.live_whats_new}}]}
        raise AssertionError(f"unexpected GET {path}")


class WhatsNewGuardTests(unittest.TestCase):
    def check(self, live: str | None, text: str) -> None:
        quiet(asc_release.check_whats_new_is_new, WhatsNewClient(live),
              app_id=APP, version_id="V-NEXT", locale="en-US", text=text)

    def test_the_live_versions_text_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.check("Bug fixes.\n", "Bug fixes.")

    def test_new_text_or_a_first_release_passes(self) -> None:
        self.check("Bug fixes.", "Faster chapter search.")
        self.check(None, "Bug fixes.")


@functools.lru_cache(maxsize=None)
def png_bytes(width: int, height: int) -> bytes:
    """A complete RGB PNG: the upload path refuses anything else (cached, the sizes are large)."""
    return make_png(width, height)


class ScreenshotClient:
    """Fake ASC client for one screenshot set."""

    def __init__(self, existing: list[bytes], *, final_state: str = "COMPLETE") -> None:
        self.shots = [
            {"id": f"OLD{i}", "attributes": {"sourceFileChecksum": hashlib.md5(raw).hexdigest(),  # noqa: S324
                                             "assetDeliveryState": {"state": "COMPLETE"}}}
            for i, raw in enumerate(existing)
        ]
        self.final_state = final_state
        self.calls: list[tuple[str, str]] = []
        self.uploaded: list[str] = []

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        if path.endswith("/appScreenshotSets"):
            return {"data": [{"id": "SET", "attributes": {"screenshotDisplayType": "APP_IPHONE_67"}}]}
        if path == "/appScreenshotSets/SET/appScreenshots":
            return {"data": self.shots}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body: dict) -> dict:
        self.calls.append(("POST", path))
        name = body["data"]["attributes"]["fileName"]
        shot = {"id": f"NEW-{name}", "attributes": {
            "uploadOperations": [{"url": "https://upload.example/x", "length": body["data"]["attributes"]["fileSize"],
                                  "offset": 0, "method": "PUT", "requestHeaders": []}],
            "assetDeliveryState": {"state": "AWAITING_UPLOAD"}}}
        self.shots.append(shot)
        return {"data": shot}

    def upload_bytes(self, url: str, data: bytes, headers: dict, *, method: str = "PUT") -> None:
        self.calls.append(("UPLOAD", url))

    def patch(self, path: str, body: dict) -> dict:
        self.calls.append(("PATCH", path))
        for shot in self.shots:
            if path == f"/appScreenshots/{shot['id']}":
                shot["attributes"]["sourceFileChecksum"] = body["data"]["attributes"]["sourceFileChecksum"]
                shot["attributes"]["assetDeliveryState"] = {"state": self.final_state}
                self.uploaded.append(shot["id"])
        return body

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path))
        if method == "DELETE":
            self.shots = [s for s in self.shots if path != f"/appScreenshots/{s['id']}"]
        return None


class ScreenshotTests(unittest.TestCase):
    SPEC = asc_release.ScreenshotSet("APP_IPHONE_67", "shots", frozenset({(1290, 2796)}))

    def make_dir(self, tmp: str, images: list[bytes]) -> Path:
        root = Path(tmp)
        (root / "shots").mkdir()
        for i, raw in enumerate(images):
            (root / "shots" / f"0{i + 1}.png").write_bytes(raw)
        return root

    def test_matching_screenshots_are_left_alone(self) -> None:
        images = [png_bytes(1290, 2796) + bytes([i]) for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, images)
            client = ScreenshotClient(images)
            changed = quiet(asc_release.upload_screenshot_set, client, "LOC", self.SPEC, root)
        self.assertFalse(changed)
        self.assertFalse([c for c in client.calls if c[0] != "GET"])

    def test_changed_screenshots_are_replaced_in_order_and_processed(self) -> None:
        old = [png_bytes(1290, 2796) + b"old"]
        new = [png_bytes(1290, 2796) + bytes([i]) for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, new)
            client = ScreenshotClient(old)
            changed = quiet(asc_release.upload_screenshot_set, client, "LOC", self.SPEC, root)
        self.assertTrue(changed)
        self.assertEqual(client.uploaded, ["NEW-01.png", "NEW-02.png"])
        self.assertIn(("DELETE", "/appScreenshots/OLD0"), client.calls)
        self.assertEqual([s["attributes"]["sourceFileChecksum"] for s in client.shots],
                         [hashlib.md5(raw).hexdigest() for raw in new])  # noqa: S324

    def test_processing_failure_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, [png_bytes(1290, 2796)])
            client = ScreenshotClient([], final_state="FAILED")
            with self.assertRaises(SystemExit):
                quiet(asc_release.upload_screenshot_set, client, "LOC", self.SPEC, root)

    def test_frozen_screenshots_pass_when_they_already_match(self) -> None:
        images = [png_bytes(1290, 2796) + bytes([i]) for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, images)
            client = ScreenshotClient(images)
            changed = quiet(asc_release.upload_screenshot_set, client, "LOC", self.SPEC, root,
                            frozen_state="READY_FOR_REVIEW")
        self.assertFalse(changed)
        self.assertFalse([c for c in client.calls if c[0] != "GET"])

    def test_frozen_screenshots_that_differ_stop_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, [png_bytes(1290, 2796) + b"new"])
            client = ScreenshotClient([png_bytes(1290, 2796) + b"old"])
            with self.assertRaises(SystemExit):
                quiet(asc_release.upload_screenshot_set, client, "LOC", self.SPEC, root,
                      frozen_state="READY_FOR_REVIEW")
        self.assertFalse([c for c in client.calls if c[0] != "GET"])

    def test_a_frozen_version_never_creates_a_screenshot_set(self) -> None:
        ipad = asc_release.ScreenshotSet("APP_IPAD_PRO_3GEN_129", "shots", frozenset({(1290, 2796)}))
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_dir(tmp, [png_bytes(1290, 2796)])
            client = ScreenshotClient([])
            with self.assertRaises(SystemExit):
                quiet(asc_release.upload_screenshot_set, client, "LOC", ipad, root,
                      frozen_state="WAITING_FOR_REVIEW")
        self.assertFalse([c for c in client.calls if c[0] != "GET"])

    def test_processing_timeout_stops(self) -> None:
        client = mock.Mock()
        client.get.return_value = {"data": [{"id": "S", "attributes": {"assetDeliveryState": {"state": "UPLOAD_COMPLETE"}}}]}
        clock = FakeClock(0)
        with self.assertRaises(SystemExit):
            quiet(asc_release.wait_for_screenshot_processing, client, "SET",
                  timeout_seconds=60, poll_seconds=10, now=clock.now, sleep=clock.sleep)
        self.assertLessEqual(clock.time, 70)

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
            for path in (root / "store/metadata/screenshots/ios/6.7").glob("*.png"):
                path.unlink()
            with self.assertRaises(SystemExit):
                quiet(asc_release.validate_screenshots, root)


class RecordingClient:
    """Offline AscClient stand-in that serves fixed collections."""

    def __init__(self, collections: dict[str, list[dict]], *, failing_posts: set[str] = frozenset()) -> None:
        self.collections = collections
        self.failing_posts = failing_posts
        self.posts: list[tuple[str, dict]] = []
        self.requests: list[tuple[str, str, dict | None]] = []

    def get(self, path: str) -> dict:
        return {"data": self.collections.get(path, [])}

    def post(self, path: str, body: dict) -> dict:
        if path in self.failing_posts:
            raise asc_release.AscHttpError(409, "conflict")
        self.posts.append((path, body))
        return {"data": {"id": "NEW"}}

    def request(self, method: str, path: str, body: dict | None = None):
        self.requests.append((method, path, body))
        return {"data": {"id": "fake"}}


TIP = "com.apoorvdarshan.iitjee.tip."
ITEMS_PATH = f"/reviewSubmissions/SUB1/items?limit=50&include={asc_release.DRAFT_ITEM_INCLUDE}"
DRAFTS_PATH = f"/apps/{APP}/reviewSubmissions?filter%5Bstate%5D=READY_FOR_REVIEW&limit=5"
REVIEW_WRITES = ("/reviewSubmissions", "/reviewSubmissionItems", "/inAppPurchaseSubmissions")


def iap_versions_path(iap_id: str) -> str:
    return f"{asc_release.ASC_V2_BASE}/inAppPurchases/{iap_id}/versions?limit=200"


def product_collections(*, draft: bool = True, versions: dict[str, list[tuple[int, str]]] | None = None,
                        **states: str) -> dict[str, list[dict]]:
    """Catalog tips in the given states. Each Ready-to-Submit tip has a version 1 in
    PREPARE_FOR_SUBMISSION unless `versions` says otherwise."""
    collections = {
        DRAFTS_PATH: [{"id": "SUB1"}] if draft else [],
        f"/apps/{APP}/inAppPurchasesV2?limit=200": [
            {"id": f"IAP-{tier}", "attributes": {"name": tier, "productId": TIP + tier, "state": state}}
            for tier, state in states.items()
        ],
    }
    for tier, state in states.items():
        rows = (versions or {}).get(tier)
        if rows is None:
            rows = [(1, "PREPARE_FOR_SUBMISSION" if state == "READY_TO_SUBMIT" else "APPROVED")]
        collections[iap_versions_path(f"IAP-{tier}")] = [
            {"type": "inAppPurchaseVersions", "id": f"IAPV-{tier}-{number}",
             "attributes": {"version": number, "state": version_state}}
            for number, version_state in rows
        ]
    return collections


def attached_iap_versions(client: RecordingClient) -> list[str]:
    return [body["data"]["relationships"]["inAppPurchaseVersion"]["data"]["id"]
            for path, body in client.posts
            if path == "/reviewSubmissionItems" and "inAppPurchaseVersion" in body["data"]["relationships"]]


class ProductSubmissionTests(unittest.TestCase):
    def submit(self, client: RecordingClient, intended: set[str] | None = None, **kwargs):
        ids = intended if intended is not None else {TIP + t for t in ("small", "medium", "large", "generous")}
        return quiet(asc_release.submit_in_app_purchases, client, APP, intended_ids=ids, **kwargs)

    def test_only_ready_catalog_tips_join_the_review_submission(self) -> None:
        collections = product_collections(small="APPROVED", medium="READY_TO_SUBMIT", large="APPROVED",
                                          generous="APPROVED", extra="READY_TO_SUBMIT")
        client = RecordingClient(collections)
        result = self.submit(client)
        self.assertEqual(attached_iap_versions(client), ["IAPV-medium-1"])
        body = client.posts[0][1]
        self.assertEqual(body["data"]["relationships"]["reviewSubmission"]["data"]["id"], "SUB1")
        self.assertEqual(body["data"]["relationships"]["inAppPurchaseVersion"]["data"]["type"],
                         "inAppPurchaseVersions")
        self.assertEqual(result.submitted, 1)
        self.assertTrue(result.has_products)

    def test_the_deprecated_immediate_endpoint_is_never_used(self) -> None:
        # POST /inAppPurchaseSubmissions sent a product to App Review on its own,
        # before the build recheck; products now wait in the draft for one submit.
        client = RecordingClient(product_collections(small="READY_TO_SUBMIT", medium="READY_TO_SUBMIT"))
        self.submit(client)
        self.assertNotIn("/inAppPurchaseSubmissions", [path for path, _ in client.posts])
        self.assertEqual(client.requests, [], "attaching must never submit the draft")

    def test_approved_tips_mean_nothing_to_submit(self) -> None:
        client = RecordingClient(product_collections(small="APPROVED", medium="APPROVED"))
        self.assertEqual(self.submit(client).submitted, 0)
        self.assertEqual(client.posts, [])

    def test_no_draft_is_created_when_nothing_needs_review(self) -> None:
        # The idempotent rerun path passes no draft; with all four tips approved
        # (PCM Notes today) it must not leave an empty review submission behind.
        client = RecordingClient(product_collections(draft=False, small="APPROVED", medium="APPROVED"))
        result = self.submit(client)
        self.assertEqual(client.posts, [])
        self.assertFalse(result.has_products)

    def test_a_draft_is_created_only_to_attach_a_product(self) -> None:
        client = RecordingClient(product_collections(draft=False, small="READY_TO_SUBMIT"))
        result = self.submit(client)
        self.assertEqual([path for path, _ in client.posts], ["/reviewSubmissions", "/reviewSubmissionItems"])
        self.assertEqual(result.submission_id, "NEW")

    def test_a_tip_that_cannot_be_submitted_blocks_the_release(self) -> None:
        client = RecordingClient(product_collections(small="READY_TO_SUBMIT"),
                                 failing_posts={"/reviewSubmissionItems"})
        with self.assertRaises(asc_release.ProductSubmitError):
            self.submit(client)

    def test_a_ready_tip_without_a_draft_version_blocks_the_release(self) -> None:
        collections = product_collections(small="READY_TO_SUBMIT", versions={"small": [(1, "APPROVED")]})
        client = RecordingClient(collections)
        with self.assertRaisesRegex(asc_release.ProductSubmitError, "no version to submit"):
            self.submit(client)
        self.assertEqual(client.posts, [])

    def test_the_newest_draft_version_is_attached(self) -> None:
        versions = {"small": [(1, "REPLACED_WITH_NEW_VERSION"), (2, "APPROVED"), (3, "PREPARE_FOR_SUBMISSION")]}
        client = RecordingClient(product_collections(small="READY_TO_SUBMIT", versions=versions))
        self.submit(client)
        self.assertEqual(attached_iap_versions(client), ["IAPV-small-3"])

    def test_an_already_attached_tip_is_not_posted_again(self) -> None:
        collections = product_collections(small="READY_TO_SUBMIT")
        collections[ITEMS_PATH] = [{"id": "I", "relationships": {
            "inAppPurchaseVersion": {"data": {"type": "inAppPurchaseVersions", "id": "IAPV-small-1"}}}}]
        client = RecordingClient(collections)
        result = self.submit(client)
        self.assertEqual(client.posts, [])
        self.assertEqual(result.submitted, 1)
        self.assertTrue(result.has_products)

    def test_subscriptions_attach_their_current_version_once(self) -> None:
        collections = product_collections()
        collections[f"/apps/{APP}/subscriptionGroups?limit=200"] = [{"id": "G1"}]
        collections["/subscriptionGroups/G1/subscriptions?limit=200"] = [
            {"id": "S0", "attributes": {"name": "Plus", "productId": "plus", "state": "DEVELOPER_REJECTED"}},
            {"id": "S1", "attributes": {"name": "Pro", "productId": "pro", "state": "READY_TO_SUBMIT"}},
        ]
        for sub in ("S0", "S1"):
            collections[f"/subscriptions/{sub}/versions"] = [
                {"id": f"{sub}-v2", "attributes": {"version": 2}},
                {"id": f"{sub}-v1", "attributes": {"version": 1}},
            ]
        collections[ITEMS_PATH] = [{"id": "I", "relationships": {
            "subscriptionVersion": {"data": {"type": "subscriptionVersions", "id": "S0-v2"}}}}]
        client = RecordingClient(collections)
        result = self.submit(client, {"plus", "pro"})
        items = [b["data"]["relationships"]["subscriptionVersion"]["data"]["id"]
                 for path, b in client.posts if path == "/reviewSubmissionItems"]
        self.assertEqual(items, ["S1-v2"])  # S0's current version was already attached
        self.assertEqual(result.submitted, 2)
        self.assertTrue(result.has_products)

    def test_empty_catalog_submits_nothing(self) -> None:
        client = RecordingClient(product_collections(small="READY_TO_SUBMIT"))
        self.assertEqual(self.submit(client, set()).submitted, 0)
        self.assertEqual(client.posts, [])

    def test_draft_items_are_read_with_their_in_app_purchase_versions(self) -> None:
        client = RecordingClient({ITEMS_PATH: [{"id": "I", "relationships": {
            "inAppPurchaseVersion": {"data": {"type": "inAppPurchaseVersions", "id": "IAPV-1"}}}}]})
        self.assertEqual(asc_release.draft_item_version_ids(client, "SUB1"), {"IAPV-1"})

    def test_catalog_lists_the_four_tips(self) -> None:
        self.assertEqual(asc_release.intended_product_ids(),
                         {TIP + t for t in ("small", "medium", "large", "generous")})

    def test_submit_for_review_does_not_readd_a_held_version(self) -> None:
        client = RecordingClient({ITEMS_PATH: [{"id": "I", "relationships": {
            "appStoreVersion": {"data": {"type": "appStoreVersions", "id": "AV1"}}}}]})
        quiet(asc_release.submit_for_review, client, APP, "AV1", "PREPARE_FOR_SUBMISSION", submission_id="SUB1")
        self.assertEqual(client.posts, [])
        self.assertIn(("PATCH", "/reviewSubmissions/SUB1"), [(m, p) for m, p, _ in client.requests])
        self.assertTrue(asc_release.draft_holds_app_version(client, "SUB1"))


class TransientErrorTests(unittest.TestCase):
    def test_real_retry_rule(self) -> None:
        import http.client
        import urllib.error

        transient = [
            http.client.RemoteDisconnected("closed"),
            ConnectionResetError("reset"),
            http.client.IncompleteRead(b""),
            urllib.error.URLError("dns"),
            TimeoutError("slow"),
            asc_release.AscHttpError(503, "busy"),
            asc_release.AscHttpError(429, "rate limited"),
            urllib.error.HTTPError("https://x", 502, "bad gateway", {}, None),
        ]
        fatal = [
            asc_release.AscHttpError(401, "unauthorized"),
            asc_release.AscHttpError(409, "conflict"),
            urllib.error.HTTPError("https://x", 404, "missing", {}, None),
            ValueError("bad"),
            ios_release.ReleaseError("mismatch"),
            SystemExit(1),
        ]
        for exc in transient:
            with self.subTest(exc=repr(exc)):
                self.assertTrue(ios_release.is_transient_error(exc))
        for exc in fatal:
            with self.subTest(exc=repr(exc)):
                self.assertFalse(ios_release.is_transient_error(exc))


class CmdWaitBuildTests(unittest.TestCase):
    def test_deadline_counts_from_upload_start_and_app_lookup_is_retried(self) -> None:
        import argparse
        import http.client

        lookups = []

        def find_app(_client, bundle_id):
            lookups.append(bundle_id)
            if len(lookups) == 1:
                raise http.client.RemoteDisconnected("closed")
            return APP

        fake_module = mock.Mock(find_app=find_app)
        captured = {}

        def fake_wait(client, **kwargs):
            captured.update(kwargs)
            return {"id": "B9"}

        args = argparse.Namespace(
            version=VERSION, build=8, started_at=STARTED, wait_minutes=120, poll_seconds=30
        )
        with tempfile.NamedTemporaryFile("r", suffix=".out") as out, mock.patch.object(
            ios_release, "asc_client", return_value=(fake_module, object())
        ), mock.patch.object(ios_release, "wait_for_build", side_effect=fake_wait), mock.patch.object(
            ios_release.time, "sleep"
        ), mock.patch.object(
            ios_release.time, "time", return_value=STARTED + 30
        ), mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out.name}), redirect_stdout(io.StringIO()):
            ios_release.cmd_wait_build(args)
            written = Path(out.name).read_text()
        self.assertEqual(lookups, [ios_release.BUNDLE_ID] * 2)
        self.assertEqual(captured["deadline"], STARTED + 120 * 60)
        self.assertEqual(captured["uploaded_after"], STARTED)
        self.assertEqual(captured["build_number"], "8")
        self.assertIs(captured["is_transient"], ios_release.is_transient_error)
        self.assertIn("build_id=B9", written)

    def test_app_lookup_gives_up_at_the_deadline(self) -> None:
        import http.client

        def boom():
            raise http.client.RemoteDisconnected("closed")

        clock = FakeClock(STARTED)
        with redirect_stdout(io.StringIO()), self.assertRaises(http.client.RemoteDisconnected):
            ios_release.call_with_retries(
                boom, deadline=STARTED + 90, poll_seconds=30, now=clock.now, sleep=clock.sleep
            )
        self.assertEqual(clock.sleeps, [30, 30, 30])


REPO_WHATS_NEW = "Faster chapter search."
APP_INFO_VALUES = {
    "name": "PCM Notes: JEE & NEET",
    "subtitle": "Handwritten Exam Revision",
    "privacyPolicyUrl": "https://pcm-notes.apoorvdarshan.com/privacy",
}


def json_copy(value):
    return json.loads(json.dumps(value))


def related(item: dict, name: str):
    return ((item.get("relationships") or {}).get(name) or {}).get("data", {}).get("id")


def image_item(image_id: str) -> dict:
    return {"id": f"I-{image_id}", "relationships": {"appAssetLibraryImage": {"data": {"type": "appAssetLibraryImages", "id": image_id}}}}


def version_item(version_id: str = "V1") -> dict:
    return {"id": f"I-{version_id}", "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}}}


class FakeAsc:
    """App Store Connect stand-in for asc_release.main() (live path, no network).

    By default App Store Connect already holds the repository's listing, What's
    New and screenshots (`differ` names what does not match) and every tip is
    approved (`ready_tips` are Ready to Submit, each with a draft version)."""

    def __init__(self, *, state: str = "PREPARE_FOR_SUBMISSION", attached: str | None = "OLD",
                 revert_on_read: int | None = None, release_type: str | None = None,
                 screenshots: dict[str, list[str]] | None = None, differ: tuple[str, ...] = (),
                 ready_tips: tuple[str, ...] = (), drafts: tuple[str, ...] = ("SUB1",),
                 app_id: str = APP, has_version: bool = True,
                 other_versions: list[tuple[str, str]] | None = None, creates_version: bool = True) -> None:
        self.state = state
        self.app_id = app_id
        # False: App Store Connect has no VERSION yet (POST /appStoreVersions adds it,
        # unless `creates_version` is False and the new version never reads back).
        self.has_version = has_version
        self.creates_version = creates_version
        # The app's other IOS versions as (versionString, appStoreState); the first is V0.
        self.other_versions = [("1.0", "READY_FOR_SALE")] if other_versions is None else other_versions
        self.attached = attached
        self.revert_on_read = revert_on_read  # this read of the selection, and later ones, show OLD
        self.release_type = release_type
        self.build_reads = 0
        self.listing = {**LISTING_VALUES, "whatsNew": REPO_WHATS_NEW}
        self.app_info = dict(APP_INFO_VALUES)
        if "listing" in differ:
            self.listing["promotionalText"] = "Last year's promo"
        if "app_info" in differ:
            self.app_info["subtitle"] = "Last year's subtitle"
        if "whats_new" in differ:
            self.listing["whatsNew"] = "Last year's notes"
        self.screenshots = {kind: list(sums) for kind, sums in (screenshots or {}).items()}
        if "screenshots" in differ:
            self.screenshots = {kind: ["stale"] for kind in self.screenshots}
        self.ready_tips = ready_tips
        self.drafts = list(drafts)
        # A READY_FOR_REVIEW version sits in its draft, as in App Store Connect.
        self.items: list[dict] = [version_item()] if state == "READY_FOR_REVIEW" else []
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, dict]] = []
        self.created_sets: list[str] = []

    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]

    def review_writes(self) -> list[tuple[str, str]]:
        return [call for call in self.writes() if call[1].startswith(REVIEW_WRITES)]

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        base, _, query = path.partition("?")
        params = urllib.parse.parse_qs(query)
        if base == "/apps":
            return {"data": [{"id": self.app_id, "attributes": {"bundleId": asc_release.DEFAULT_BUNDLE_ID}}]}
        if base == f"/apps/{self.app_id}/appStoreVersions":
            rows = [{"id": "V0" if i == 0 else f"V0-{i}", "attributes": {"versionString": number, "appStoreState": state}}
                    for i, (number, state) in enumerate(self.other_versions)]
            if self.has_version:
                attrs = {"versionString": VERSION, "appStoreState": self.state}
                if self.release_type:
                    attrs["releaseType"] = self.release_type
                rows.append({"id": "V1", "attributes": attrs})
            return {"data": rows}
        if base == "/builds/B9":
            return {"data": build_row("B9"), "included": [pre_row()]}
        if base == "/appStoreVersions/V1/build":
            self.build_reads += 1
            if self.revert_on_read is not None and self.build_reads >= self.revert_on_read:
                return {"data": {"type": "builds", "id": "OLD"}}
            return {"data": {"type": "builds", "id": self.attached} if self.attached else None}
        if base == f"/apps/{APP}/reviewSubmissions":
            return {"data": [{"id": draft} for draft in self.drafts]}
        if base.startswith("/reviewSubmissions/") and base.endswith("/items"):
            return {"data": self.items}
        if base == f"/apps/{APP}/appInfos":
            return {"data": [
                {"id": "INFO-LIVE", "attributes": {"state": "READY_FOR_DISTRIBUTION"}},
                {"id": "INFO-NEXT", "attributes": {"state": self.state}},
            ]}
        if base == "/appInfos/INFO-NEXT/appInfoLocalizations":
            return {"data": [{"id": "AIL", "attributes": {"locale": params["filter[locale]"][0], **self.app_info}}]}
        if base == "/appStoreVersions/V1/appStoreVersionLocalizations":
            return {"data": [{"id": "VL", "attributes": {"locale": params["filter[locale]"][0], **self.listing}}]}
        if base == "/appStoreVersions/V0/appStoreVersionLocalizations":
            return {"data": [{"id": "VL0", "attributes": {"locale": "en-US", "whatsNew": "First edition."}}]}
        if base == "/appStoreVersionLocalizations/VL/appScreenshotSets":
            return {"data": [{"id": f"SET-{kind}", "attributes": {"screenshotDisplayType": kind}}
                             for kind in self.screenshots]}
        if base.startswith("/appScreenshotSets/SET-") and base.endswith("/appScreenshots"):
            kind = base.split("/")[2][len("SET-"):]
            return {"data": [{"id": f"{kind}-{i}", "attributes": {
                "sourceFileChecksum": checksum, "assetDeliveryState": {"state": "COMPLETE"}}}
                for i, checksum in enumerate(self.screenshots[kind])]}
        if base.startswith("/appScreenshotSets/NEW-") and base.endswith("/appScreenshots"):
            return {"data": []}  # a set this run created starts empty
        if base == f"/apps/{APP}/subscriptionGroups":
            return {"data": []}
        if base == f"/apps/{APP}/inAppPurchasesV2":
            return {"data": [
                {"id": f"IAP-{tier}", "attributes": {
                    "name": tier, "productId": TIP + tier,
                    "state": "READY_TO_SUBMIT" if tier in self.ready_tips else "APPROVED"}}
                for tier in ("small", "medium", "large", "generous")
            ]}
        if path.startswith(f"{asc_release.ASC_V2_BASE}/inAppPurchases/IAP-"):
            tier = path.split("/inAppPurchases/IAP-")[1].split("/")[0]
            state = "PREPARE_FOR_SUBMISSION" if tier in self.ready_tips else "APPROVED"
            return {"data": [{"type": "inAppPurchaseVersions", "id": f"IAPV-{tier}",
                              "attributes": {"version": 1, "state": state}}]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body: dict) -> dict:
        self.calls.append(("POST", path))
        self.bodies.append(("POST", path, body))
        if path == "/appStoreVersions":
            if self.creates_version:
                self.has_version = True
                self.state = "PREPARE_FOR_SUBMISSION"
            return {"data": {"id": "V1"}}
        if path == "/reviewSubmissions":
            self.drafts.append("SUB-NEW")
            return {"data": {"id": "SUB-NEW"}}
        if path == "/appScreenshotSets":
            display = body["data"]["attributes"]["screenshotDisplayType"]
            self.created_sets.append(display)
            return {"data": {"id": f"NEW-{display}"}}
        if path == "/reviewSubmissionItems":
            self.items.append({"id": f"ITEM{len(self.items)}", "relationships": {
                name: rel for name, rel in body["data"]["relationships"].items() if name != "reviewSubmission"}})
        return {"data": {"id": "ITEM"}}

    def patch(self, path: str, body: dict) -> dict:
        self.calls.append(("PATCH", path))
        return {"data": body["data"]}

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path))
        if method == "PATCH" and path == "/appStoreVersions/V1/relationships/build":
            self.attached = body["data"]["id"]
        return None


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
            self.screenshot_sums[spec.display_type] = [hashlib.md5(raw).hexdigest() for raw in images]  # noqa: S324
            sets.append(spec._replace(directory=str(directory)))  # absolute: independent of ROOT
        # The optional iPhone Duo and Apple Watch folders stay empty here, whatever the repository holds.
        optional = tuple(spec._replace(directory=str(root / spec.display_type))
                         for spec in asc_release.OPTIONAL_SCREENSHOT_SETS)
        for patcher in (mock.patch.object(asc_release, "SCREENSHOT_SETS", tuple(sets)),
                        mock.patch.object(asc_release, "OPTIONAL_SCREENSHOT_SETS", optional),
                        mock.patch.object(asc_release, "WATCH_FOLDER", str(root / "watch"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake(self, **kwargs) -> FakeAsc:
        kwargs.setdefault("screenshots", self.screenshot_sums)
        return FakeAsc(**kwargs)

    def run_main(self, fake: FakeAsc, env_flags: dict[str, str] | None = None, *,
                 real_products: bool = False) -> tuple[int, mock.Mock]:
        products = mock.Mock(return_value=asc_release.ProductSubmitResult("SUB1", 0, False))
        argv = ["asc_release.py", "--version", VERSION, "--build-id", "B9", "--build-number", "8",
                "--metadata-dir", str(self.metadata)]
        env = {k: v for k, v in os.environ.items() if k not in FLAG_NAMES}
        env.update(env_flags or {"SUBMIT_IOS_REVIEW": "true"})
        patches = [mock.patch.object(asc_release, "client_from_environment", return_value=fake)]
        if not real_products:
            patches.append(mock.patch.object(asc_release, "submit_in_app_purchases", products))
        code = 0
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", argv), \
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
        calls = fake.calls
        select = calls.index(("PATCH", "/appStoreVersions/V1/relationships/build"))
        submit = calls.index(("PATCH", "/reviewSubmissions/SUB1"))
        reads = [i for i, c in enumerate(calls) if c == ("GET", "/appStoreVersions/V1/build")]
        build_checks = [i for i, c in enumerate(calls) if c[0] == "GET" and c[1].startswith("/builds/B9")]
        self.assertEqual(len(reads), 3, "selection is read before and after selecting, then rechecked")
        self.assertTrue(reads[0] < select < reads[1] < reads[2] < submit)
        self.assertEqual(len(build_checks), 2)
        self.assertTrue(build_checks[1] < reads[2] < submit, "build must be re-verified before submitting")
        first_review_write = calls.index(fake.review_writes()[0])
        self.assertTrue(reads[2] < first_review_write, "the recheck comes before any review-submission write")
        self.assertIn(("PATCH", "/appStoreVersions/V1"), calls)  # release type
        products.assert_called_once()

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
        recheck = [i for i, c in enumerate(fake.calls) if c == ("GET", "/appStoreVersions/V1/build")][-1]
        writes = fake.review_writes()
        self.assertEqual(writes, [("POST", "/reviewSubmissionItems"), ("POST", "/reviewSubmissionItems"),
                                  ("PATCH", "/reviewSubmissions/SUB1")])
        self.assertTrue(all(fake.calls.index(write) > recheck for write in writes))
        attached = [rel for item in fake.items for rel in item["relationships"]]
        self.assertEqual(attached, ["inAppPurchaseVersion", "appStoreVersion"])
        self.assertEqual(fake.items[0]["relationships"]["inAppPurchaseVersion"]["data"]["id"], "IAPV-medium")

    def test_no_review_write_when_the_recheck_fails(self) -> None:
        # Before, a Ready-to-Submit tip went to App Review on its own
        # (POST /inAppPurchaseSubmissions) before this recheck could stop the run.
        fake = self.fake(ready_tips=("medium",), revert_on_read=3)
        code, _ = self.run_main(fake, real_products=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.review_writes(), [])

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
        self.assertNotIn(("PATCH", "/appStoreVersions/V1/relationships/build"), fake.calls)

    def test_metadata_on_a_live_version_is_refused_without_writes(self) -> None:
        fake = self.fake(state="READY_FOR_SALE")
        code, _ = self.run_main(fake, {"UPLOAD_LISTING": "true"})
        self.assertEqual(code, 1)
        self.assertEqual(fake.writes(), [])


LIVE_SWITCHES = {"UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true", "SUBMIT_IOS_REVIEW": "true"}


class RerunTests(LiveFlowCase):
    """"Re-run failed jobs" after the first attempt got the version into a draft
    (READY_FOR_REVIEW) or into review (WAITING_FOR_REVIEW), with metadata switches on."""

    def test_rerun_in_a_draft_finishes_the_submission(self) -> None:
        fake = self.fake(state="READY_FOR_REVIEW", attached="B9", release_type="MANUAL")
        code, _ = self.run_main(fake, {**LIVE_SWITCHES, "UPLOAD_SCREENSHOTS": "true"}, real_products=True)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [("PATCH", "/reviewSubmissions/SUB1")])

    def test_rerun_in_a_draft_still_updates_changed_text(self) -> None:
        fake = self.fake(state="READY_FOR_REVIEW", attached="B9", differ=("listing", "app_info"))
        code, _ = self.run_main(fake, LIVE_SWITCHES, real_products=True)
        self.assertEqual(code, 0)
        writes = fake.writes()
        self.assertIn(("PATCH", "/appInfoLocalizations/AIL"), writes)
        self.assertIn(("PATCH", "/appStoreVersionLocalizations/VL"), writes)
        self.assertEqual(writes[-1], ("PATCH", "/reviewSubmissions/SUB1"))

    def test_rerun_in_a_draft_refuses_changed_screenshots(self) -> None:
        fake = self.fake(state="READY_FOR_REVIEW", attached="B9", differ=("screenshots",))
        code, _ = self.run_main(fake, {**LIVE_SWITCHES, "UPLOAD_SCREENSHOTS": "true"}, real_products=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.review_writes(), [])
        self.assertFalse([w for w in fake.writes() if "Screenshot" in w[1]])

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
        self.assertEqual(fake.writes(), [("POST", "/reviewSubmissions"), ("POST", "/reviewSubmissionItems"),
                                         ("PATCH", "/reviewSubmissions/SUB-NEW")])

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
    ), mock.patch.object(asc_release, "client_from_environment", return_value=fake), \
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
        self.assertEqual(fake.writes(), [("POST", "/appStoreVersions")])
        body = fake.bodies[0][2]["data"]
        self.assertEqual(body["type"], "appStoreVersions")
        self.assertEqual(body["attributes"], {"platform": "IOS", "versionString": VERSION})
        self.assertEqual(body["relationships"]["app"]["data"], {"type": "apps", "id": APP})
        self.assertEqual(fake.calls[-1][0], "GET", "the created version must be read back")
        self.assertIn(f"created App Store version {VERSION} (PREPARE_FOR_SUBMISSION)", output)

    def test_a_created_version_that_does_not_read_back_fails_naming_it(self) -> None:
        fake = FakeAsc(has_version=False, creates_version=False)
        code, output = self.create(fake)
        self.assertEqual(code, 1)
        self.assertIn(f"created App Store version {VERSION}, but reading it back found 0 records", output)
        self.assertIn("check it in App Store Connect before rerunning", output)
        self.assertNotIn("turn on IOS_CREATE_VERSION", output)
        self.assertEqual(fake.writes(), [("POST", "/appStoreVersions")])

    def test_a_created_version_that_reads_back_twice_fails_naming_it(self) -> None:
        class Doubled(FakeAsc):
            def post(self, path: str, body: dict) -> dict:
                result = super().post(path, body)
                if path == "/appStoreVersions":  # App Store Connect now lists two records for it
                    self.other_versions = [*self.other_versions, (VERSION, "PREPARE_FOR_SUBMISSION")]
                return result

        fake = Doubled(has_version=False)
        code, output = self.create(fake)
        self.assertEqual(code, 1)
        self.assertIn(f"created App Store version {VERSION}, but reading it back found 2 records", output)
        self.assertEqual(fake.writes(), [("POST", "/appStoreVersions")])

    def test_the_preflight_accepts_a_version_this_step_created(self) -> None:
        fake = FakeAsc(has_version=False)
        code, output = self.create(fake)
        self.assertEqual(code, 0, output)
        out = io.StringIO()
        with mock.patch.object(ios_release, "asc_client", return_value=(asc_release, fake)), \
                redirect_stdout(out), redirect_stderr(out):
            ios_release.main(["preflight", "--version", VERSION, "--whats-new", "--screenshots", "--submit"])
        self.assertIn("can take this release", out.getvalue())
        self.assertEqual(fake.writes(), [("POST", "/appStoreVersions")])

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
                self.assertEqual(fake.writes(), [("POST", "/appStoreVersions")])
        fake = FakeAsc(has_version=False, other_versions=[])
        code, output = self.create(fake)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [("POST", "/appStoreVersions")])

    def test_other_app_or_bad_version_fails_without_writing(self) -> None:
        fake = FakeAsc(app_id="1234567890", has_version=False)
        code, output = self.create(fake)
        self.assertEqual(code, 1)
        self.assertIn(f"expected PCM Notes ({APP})", output)
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


PILLOW = asc_release.try_import_pillow()


@functools.lru_cache(maxsize=None)
def creative_png(width: int, height: int, colour_type: int = 2) -> bytes:
    return make_png(width, height, colour_type)


def jpeg(width: int, height: int) -> bytes:
    """A JPEG header with a baseline start-of-frame segment (enough to read its size)."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x01\x11\x00" * 3
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def md5(raw: bytes) -> str:
    return hashlib.md5(raw).hexdigest()  # noqa: S324 - App Store Connect's sourceFileChecksum


def run_preflight(fake, *flags: str, creative_dir: Path | None = None) -> tuple[int, str]:
    """ios_release.py preflight against a fake; returns (exit code, output)."""
    out = io.StringIO()
    with mock.patch.object(ios_release, "asc_client", return_value=(asc_release, fake)), \
            mock.patch.object(asc_release, "DEFAULT_CREATIVE_DIR", creative_dir or asc_release.DEFAULT_CREATIVE_DIR), \
            redirect_stdout(out), redirect_stderr(out):
        try:
            ios_release.main(["preflight", "--version", VERSION, *flags])
        except SystemExit as exc:
            return (exc.code if isinstance(exc.code, int) else 1), out.getvalue()
    return 0, out.getvalue()


class CreativeGateTests(unittest.TestCase):
    """IOS_UPLOAD_CREATIVE_ASSETS follows every switch convention."""

    def test_creative_assets_are_their_own_switch(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_CREATIVE_ASSETS": "true"})
        self.assertTrue(result["creative"])
        self.assertTrue(result["asc"], "the store job runs for creative assets alone")
        self.assertFalse(result["build"])
        for key in OPS:
            self.assertFalse(result[key], key)
        self.assertFalse(gates({**ALL_ON, "IOS_UPLOAD_CREATIVE_ASSETS": "false"})["creative"])

    def test_it_needs_the_master_switch_and_validation_runs_never_use_it(self) -> None:
        result = gates({"IOS_UPLOAD_CREATIVE_ASSETS": "true"})
        self.assertFalse(result["creative"])
        self.assertFalse(result["asc"])
        self.assertTrue(any("IOS_UPLOAD_CREATIVE_ASSETS" in note for note in result["notes"]))
        manual = gates({**ALL_ON, "IOS_UPLOAD_CREATIVE_ASSETS": "true"}, event="workflow_dispatch", ref="main")
        self.assertFalse(manual["creative"])

    def test_invalid_values_fail_even_when_the_master_is_off(self) -> None:
        for env in ({"IOS_UPLOAD_CREATIVE_ASSETS": "maybe"},
                    {"IOS_RELEASE_AUTOMATION": "false", "IOS_UPLOAD_CREATIVE_ASSETS": "2"}):
            with self.subTest(env=env), self.assertRaisesRegex(ios_release.ReleaseError, "IOS_UPLOAD_CREATIVE_ASSETS"):
                gates(env)

    def test_every_ios_switch_is_printed_and_documented(self) -> None:
        example = (Path(ios_release.ROOT) / "store" / "gates.env.example").read_text()
        self.assertEqual(dict(ios_release.OPERATION_SWITCHES).get("IOS_UPLOAD_CREATIVE_ASSETS"), "creative")
        for name, _ in ios_release.OPERATION_SWITCHES:
            with self.subTest(name=name):
                self.assertIn(name, print_gates.GATES)
                self.assertIn(f"\n{name}=false\n", example)

    def test_preflight_reads_the_switch(self) -> None:
        # Both images approved and placed: the preflight checks their Asset Library state without writing.
        with tempfile.TemporaryDirectory() as tmp:
            creative, library, placements = Path(tmp), [], {}
            for image_id, folder, raw, placement in (
                    ("HDR", "header", creative_png(3840, 1646), "PRODUCT_PAGE_HEADER_ASSET"),
                    ("SRCH", "search-results", creative_png(1920, 1280), "APP_STORE_SEARCH_RESULTS_ASSET")):
                (creative / folder).mkdir()
                (creative / folder / f"{folder}.png").write_bytes(raw)
                library.append({"id": image_id, "attributes": {"fileName": f"{folder}.png", "fileSize": len(raw),
                                                               "state": "APPROVED"}})
                placements[f"PL-{image_id}"] = {"type": placement, "image": image_id, "loc": "VL"}
            fake = CreativeFake(library=library, placements=placements)
            code, output = run_preflight(fake, "--creative", creative_dir=creative)
            self.assertEqual(code, 0, output)
            # The images go on the version's localization, so the preflight checks it, and their library state.
            self.assertTrue([c for c in fake.calls if "appStoreVersionLocalizations" in c[1]])
            self.assertIn(("GET", f"/apps/{APP}/assetLibrary"), fake.calls)
            self.assertEqual(fake.writes(), [])
            # Without --creative (the switch off) the Asset Library is never read.
            fake = CreativeFake(library=library, placements=placements)
            code, output = run_preflight(fake, "--screenshots", creative_dir=creative)
            self.assertEqual(code, 0, output)
            self.assertFalse([c for c in fake.calls if "asset" in c[1].lower()])

    def test_print_gates_lists_it(self) -> None:
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("IOS_", "ANDROID_", "STORE_"))}
        for value, shown in (("true", "IOS_UPLOAD_CREATIVE_ASSETS=true\n"),
                             ("maybe", "IOS_UPLOAD_CREATIVE_ASSETS=INVALID 'maybe'")):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {**clean, "IOS_UPLOAD_CREATIVE_ASSETS": value}, clear=True), \
                    redirect_stdout(out):
                print_gates.main()
            self.assertIn(shown, out.getvalue())


class ScreenshotAlphaGuardTests(unittest.TestCase):
    """The last guard: an upload copy that still stores alpha never reaches App Store Connect."""

    def test_alpha_is_detected_the_way_app_store_connect_refuses_it(self) -> None:
        cases = {
            "rgb": (make_png(2, 2, 2), False),
            "grey": (make_png(2, 2, 0), False),
            "palette": (make_png(2, 2, 3), False),
            "grey + alpha": (make_png(2, 2, 4), True),
            "rgba": (make_png(2, 2, 6), True),
            "rgb + tRNS": (make_png(2, 2, 2, chunks=((b"tRNS", b"\x00\x00\x00\x00\x00\x00"),)), True),
            "palette + tRNS": (make_png(2, 2, 3, chunks=((b"tRNS", b"\x00"),)), True),
        }
        for label, (raw, alpha) in cases.items():
            with self.subTest(label):
                self.assertEqual(asc_release.png_stores_alpha(asc_release.png_chunks(raw)), alpha)

    def test_truncated_or_corrupt_pngs_are_refused(self) -> None:
        good = make_png(8, 8)
        ihdr_end = 8 + 12 + 13
        idat = good.index(b"IDAT") - 4
        corrupt_crc = bytearray(good)
        corrupt_crc[idat + 8] ^= 0xFF  # first byte of the IDAT data; its CRC no longer matches
        wrong_crc = bytearray(good)
        wrong_crc[ihdr_end - 4] ^= 0xFF  # the IHDR chunk's stored CRC; its data is intact
        cases = {
            "signature and IHDR only": good[:ihdr_end],
            "cut in the image data": good[: idat + 10],
            "no IEND": good[:-12],
            "IEND without its CRC": good[:-4],
            "bad CRC": bytes(corrupt_crc),
            "CRC that does not match intact data": bytes(wrong_crc),
            "no IDAT": good[:ihdr_end] + good[-12:],
            "image data is not zlib": make_png(8, 8)[:idat] + self.chunk(b"IDAT", b"not zlib") + good[-12:],
            "image data for another size": make_png(8, 8)[:idat] + make_png(4, 4)[make_png(4, 4).index(b"IDAT") - 4:],
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                asc_release.png_chunks(raw)
        self.assertEqual([kind for kind, _ in asc_release.png_chunks(good)], [b"IHDR", b"IDAT", b"IEND"])
        # Lenient reading (the colour chunks of an RGB copy) only needs the header.
        self.assertEqual(asc_release.png_chunks(good[:ihdr_end], strict=False)[0][0], b"IHDR")

    @staticmethod
    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    def test_staged_files_with_alpha_or_not_png_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "01-home.png"
            for raw in (make_png(4, 4, 6), make_png(4, 4, 4), make_png(4, 4, 2, chunks=((b"tRNS", b"\x00" * 6),)),
                        b"\x89PNG\r\n\x1a\n" + b"screenshot" * 4, b"GIF89a", make_png(4, 4)[:-12]):
                path.write_bytes(raw)
                with self.subTest(raw=raw[:30]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    asc_release.staged_png(path, '6.7" iPhone')
            path.write_bytes(make_png(4, 4, 2))
            self.assertEqual(asc_release.staged_png(path, '6.7" iPhone'), (4, 4))

    def test_committed_screenshots_that_are_not_complete_pngs_fail_before_any_call(self) -> None:
        # Checked with the other local inputs (the dry run before the build), not only at upload.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for spec in asc_release.SCREENSHOT_SETS:
                (root / spec.directory).mkdir(parents=True)
                (root / spec.directory / "01.png").write_bytes(png_bytes(*sorted(spec.sizes)[0]))
            quiet(asc_release.validate_screenshots, root)
            target = root / asc_release.SCREENSHOT_SETS[0].directory / "01.png"
            good = target.read_bytes()
            idat = good.index(b"IDAT") - 4
            small = make_png(4, 4)
            cases = {
                "no IEND": good[:-12],
                "cut short": good[:200],
                "image data for another size": good[:idat] + small[small.index(b"IDAT") - 4:],
            }
            for label, raw in cases.items():
                target.write_bytes(raw)
                with self.subTest(label), self.assertRaises(SystemExit):
                    quiet(asc_release.validate_screenshots, root)


class CapturingScreenshotClient(ScreenshotClient):
    """ScreenshotClient that keeps the bytes of every file it receives, by file name."""

    def __init__(self, existing: list[bytes], **kwargs) -> None:
        super().__init__(existing, **kwargs)
        self.sent: dict[str, bytes] = {}
        self.reserving = ""

    def post(self, path: str, body: dict) -> dict:
        self.reserving = body["data"]["attributes"]["fileName"]
        return super().post(path, body)

    def upload_bytes(self, url: str, data: bytes, headers: dict, *, method: str = "PUT") -> None:
        super().upload_bytes(url, data, headers, method=method)
        self.sent[self.reserving] = data


class ScreenshotCopyTests(unittest.TestCase):
    """Screenshots go up as copies made in a temporary directory; the committed files are only read."""

    SPEC = asc_release.ScreenshotSet("APP_IPHONE_67", "shots", frozenset({(1290, 2796)}))

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "shots").mkdir()

    def without_pillow(self):
        patcher = mock.patch.object(asc_release, "try_import_pillow", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_screenshot_without_alpha_goes_up_byte_for_byte(self) -> None:
        self.without_pillow()  # an RGB file needs no Pillow
        raw = png_bytes(1290, 2796)
        (self.root / "shots" / "01.png").write_bytes(raw)
        client = CapturingScreenshotClient([])
        self.assertTrue(quiet(asc_release.upload_screenshot_set, client, "LOC", self.SPEC, self.root))
        self.assertEqual(client.sent, {"01.png": raw})

    def test_upload_without_pillow_refuses_screenshots_that_store_alpha(self) -> None:
        self.without_pillow()
        cases = {
            "rgba": make_png(1290, 2796, 6),
            "grey + alpha": make_png(1290, 2796, 4),
            "rgb + tRNS": make_png(1290, 2796, 2, chunks=((b"tRNS", b"\x00" * 6),)),
        }
        (self.root / "shots" / "01-rgb.png").write_bytes(png_bytes(1290, 2796))
        for label, raw in cases.items():
            (self.root / "shots" / "02-alpha.png").write_bytes(raw)
            client = CapturingScreenshotClient([b"live"])
            stderr = io.StringIO()
            with self.subTest(label), self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                    redirect_stderr(stderr):
                asc_release.upload_screenshot_set(client, "LOC", self.SPEC, self.root)
            self.assertIn("02-alpha.png", stderr.getvalue())
            self.assertIn("Pillow", stderr.getvalue())
            self.assertFalse([c for c in client.calls if c[0] != "GET"], label)
        # An RGB file is copied as it is.
        (self.root / "shots" / "02-alpha.png").write_bytes(png_bytes(1290, 2796) + b"2")
        client = CapturingScreenshotClient([])
        quiet(asc_release.upload_screenshot_set, client, "LOC", self.SPEC, self.root)
        self.assertEqual(sorted(client.sent), ["01-rgb.png", "02-alpha.png"])

    def test_a_set_app_store_connect_holds_as_committed_needs_no_copies(self) -> None:
        # The committed mockups store alpha and App Store Connect holds them as they are.
        self.without_pillow()
        raw = make_png(1290, 2796, 6)
        (self.root / "shots" / "01.png").write_bytes(raw)
        for frozen in (None, "READY_FOR_REVIEW"):
            client = CapturingScreenshotClient([raw])
            with self.subTest(frozen=frozen):
                self.assertFalse(quiet(asc_release.upload_screenshot_set, client, "LOC", self.SPEC, self.root,
                                       frozen_state=frozen))
                self.assertFalse([c for c in client.calls if c[0] != "GET"])

    def test_every_set_is_checked_before_any_screenshot_is_written(self) -> None:
        # The first set could be replaced, but the second cannot be copied (alpha, no Pillow):
        # the run stops before the first set is touched.
        self.without_pillow()
        sets = []
        for spec in asc_release.SCREENSHOT_SETS:
            directory = self.root / spec.display_type
            directory.mkdir()
            colour = 2 if spec is asc_release.SCREENSHOT_SETS[0] else 6
            (directory / "01.png").write_bytes(make_png(*sorted(spec.sizes)[0], colour))
            sets.append(spec._replace(directory=str(directory)))
        fake = FakeAsc(screenshots={spec.display_type: ["stale"] for spec in sets})
        with mock.patch.object(asc_release, "SCREENSHOT_SETS", tuple(sets)), \
                mock.patch.object(asc_release, "OPTIONAL_SCREENSHOT_SETS", ()), \
                mock.patch.object(asc_release, "WATCH_FOLDER", str(self.root / "watch")), \
                self.assertRaises(SystemExit):
            quiet(asc_release.upload_screenshots, fake, version_id="V1")
        self.assertEqual(fake.writes(), [])

    def test_a_copy_that_still_stores_alpha_or_is_not_a_complete_png_never_goes_up(self) -> None:
        # The last guard on the upload path: should flattening ever leave alpha in a copy, or
        # write an incomplete PNG, the run stops before any screenshot is written. Pillow's
        # part is stood in for, so this runs without Pillow too.
        (self.root / "shots" / "01.png").write_bytes(make_png(1290, 2796, 6))
        cases = {
            "unchanged RGBA": (make_png(1290, 2796, 6), "stores an alpha channel"),
            "grey + alpha": (make_png(1290, 2796, 4), "stores an alpha channel"),
            "rgb + tRNS": (make_png(1290, 2796, 2, chunks=((b"tRNS", b"\x00" * 6),)), "stores an alpha channel"),
            "no IEND": (png_bytes(1290, 2796)[:-12], "not a readable PNG"),
        }
        for label, (flattened, message) in cases.items():
            client = CapturingScreenshotClient([b"live"])
            stderr = io.StringIO()
            with self.subTest(label), \
                    mock.patch.object(asc_release, "try_import_pillow", return_value=object()), \
                    mock.patch.object(asc_release, "prepare_asc_png", return_value=flattened), \
                    redirect_stdout(io.StringIO()), redirect_stderr(stderr), self.assertRaises(SystemExit):
                asc_release.upload_screenshot_set(client, "LOC", self.SPEC, self.root)
            self.assertIn("01.png", stderr.getvalue(), label)
            self.assertIn(message, stderr.getvalue(), label)
            self.assertFalse([c for c in client.calls if c[0] != "GET"], label)
            self.assertEqual(client.sent, {}, label)


@unittest.skipIf(PILLOW is None and not os.environ.get("STORE_TESTS_NEED_PILLOW"), "Pillow not installed")
class RgbCopyTests(unittest.TestCase):
    """With Pillow, a screenshot that stores alpha goes up as an RGB copy without alpha."""

    OPAQUE = (10, 20, 30, 255)
    HIDDEN_RED = (255, 0, 0, 0)  # fully transparent, with a colour that must never show
    HALF = (200, 100, 50, 128)
    SPEC = asc_release.ScreenshotSet("APP_IPHONE_67", "shots", frozenset({(1290, 2796)}))

    def setUp(self) -> None:
        if PILLOW is None:
            self.fail("Pillow is required here (STORE_TESTS_NEED_PILLOW is set)")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def frame(self, directory: Path, name: str, size: tuple[int, int], *, icc: bytes | None = None,
              chunks: tuple[tuple[bytes, bytes], ...] = ()) -> Path:
        from PIL import PngImagePlugin

        img = PILLOW.new("RGBA", size, self.OPAQUE)
        width, height = size
        # The left quarter is transparent; one translucent pixel top right.
        img.paste(self.HIDDEN_RED, (0, 0, width // 4, height))
        img.putpixel((width - 1, 0), self.HALF)
        info = PngImagePlugin.PngInfo()
        for kind, data in chunks:
            info.add(kind, data)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        img.save(path, pnginfo=info, **({"icc_profile": icc} if icc else {}))
        return path

    def rgb(self, raw: bytes, name: str):
        self.assertEqual(raw[25], 2, f"{name} is not an RGB PNG (colour type {raw[25]})")
        self.assertNotIn(b"tRNS", [kind for kind, _ in asc_release.png_chunks(raw)])
        img = PILLOW.open(io.BytesIO(raw))
        self.assertEqual(img.mode, "RGB")
        return img

    def upload(self, client: ScreenshotClient, **kwargs) -> bool:
        return quiet(asc_release.upload_screenshot_set, client, "LOC", self.SPEC, self.root, **kwargs)

    def test_screenshots_that_store_alpha_keep_their_pixels_over_black(self) -> None:
        source = self.frame(self.root / "shots", "01-a.png", (1290, 2796))
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        client = CapturingScreenshotClient([])

        self.assertTrue(self.upload(client))

        img = self.rgb(client.sent["01-a.png"], "01-a.png")
        self.assertEqual(img.size, (1290, 2796))
        self.assertEqual(img.getpixel((1289, 2795)), self.OPAQUE[:3], "opaque pixels are kept as they are")
        self.assertEqual(img.getpixel((0, 0)), (0, 0, 0), "transparent pixels are laid over black")
        # Translucent pixels are blended over black exactly once.
        for got, value in zip(img.getpixel((1289, 0)), self.HALF[:3]):
            self.assertAlmostEqual(got, value * 128 / 255, delta=1)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before, "the committed file is untouched")
        # App Store Connect records the copy's checksum.
        self.assertEqual(client.shots[-1]["attributes"]["sourceFileChecksum"], md5(client.sent["01-a.png"]))

    def test_a_set_app_store_connect_holds_as_rgb_copies_is_left_alone(self) -> None:
        self.frame(self.root / "shots", "01-a.png", (1290, 2796))
        first = CapturingScreenshotClient([])
        self.upload(first)
        copy = first.sent["01-a.png"]
        for frozen in (None, "READY_FOR_REVIEW", "WAITING_FOR_REVIEW"):
            client = CapturingScreenshotClient([copy])
            with self.subTest(frozen=frozen):
                self.assertFalse(self.upload(client, frozen_state=frozen))
                self.assertFalse([c for c in client.calls if c[0] != "GET"])

    def test_every_app_store_set_is_uploaded_without_alpha(self) -> None:
        sets, frames = [], {}
        for spec in asc_release.SCREENSHOT_SETS:
            directory = self.root / spec.display_type
            frames[spec.display_type] = self.frame(directory, "01.png", sorted(spec.sizes)[0])
            sets.append(spec._replace(directory=str(directory)))
        duo, = asc_release.OPTIONAL_SCREENSHOT_SETS
        frames[duo.display_type] = self.frame(self.root / "duo", "01-inner.png", (2007, 2853))
        frames["APP_WATCH_SERIES_10"] = self.frame(self.root / "watch", "01-w.png", (416, 496))
        uploads: list[tuple[str, str, bytes]] = []
        fake = FakeAsc(screenshots={})
        with mock.patch.object(asc_release, "SCREENSHOT_SETS", tuple(sets)), \
                mock.patch.object(asc_release, "OPTIONAL_SCREENSHOT_SETS",
                                  (duo._replace(directory=str(self.root / "duo")),)), \
                mock.patch.object(asc_release, "WATCH_FOLDER", str(self.root / "watch")), \
                mock.patch.object(asc_release, "upload_screenshot_file",
                                  lambda client, set_id, path: uploads.append((set_id, path.name, path.read_bytes()))), \
                mock.patch.object(asc_release, "wait_for_screenshot_processing", lambda client, set_id: None):
            quiet(asc_release.upload_screenshots, fake, version_id="V1")
        self.assertEqual([set_id for set_id, _, _ in uploads],
                         ["NEW-APP_IPHONE_67", "NEW-APP_IPAD_PRO_3GEN_129", "NEW-APP_IPHONE_DUO", "NEW-APP_WATCH_SERIES_10"])
        for set_id, name, raw in uploads:
            with self.subTest(set=set_id):
                self.rgb(raw, name)
        for path in frames.values():
            self.assertEqual(path.read_bytes()[25], 6, "the committed frames still store alpha")

    def test_the_colour_profile_is_kept(self) -> None:
        from PIL import ImageCms

        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        gamma = (b"gAMA", (45455).to_bytes(4, "big"))
        chroma = (b"cHRM", bytes(range(32)))
        self.frame(self.root / "shots", "01-a.png", (1290, 2796), icc=icc, chunks=(gamma, chroma))
        client = CapturingScreenshotClient([])

        self.upload(client)

        raw = client.sent["01-a.png"]
        self.assertEqual(self.rgb(raw, "01-a.png").info.get("icc_profile"), icc)
        kept = {kind: data for kind, data in asc_release.png_chunks(raw)}
        self.assertEqual(kept.get(b"gAMA"), gamma[1])
        self.assertEqual(kept.get(b"cHRM"), chroma[1])


class WatchScreenshotTests(unittest.TestCase):
    """Apple Watch frames are uploaded like the other sets, by the display type their size belongs to."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        sets, self.live = [], {}
        for spec in asc_release.SCREENSHOT_SETS:
            directory = self.root / spec.display_type
            directory.mkdir()
            raw = png_bytes(*sorted(spec.sizes)[0])
            (directory / "01-home.png").write_bytes(raw)
            self.live[spec.display_type] = [md5(raw)]
            sets.append(spec._replace(directory=str(directory)))
        self.duo = self.root / "iphone-duo"
        self.watch = self.root / "watch"
        self.watch.mkdir()
        self.watch_png = make_png(416, 496)
        (self.watch / "01-home-glance.png").write_bytes(self.watch_png)
        duo, = asc_release.OPTIONAL_SCREENSHOT_SETS
        for patcher in (mock.patch.object(asc_release, "SCREENSHOT_SETS", tuple(sets)),
                        mock.patch.object(asc_release, "OPTIONAL_SCREENSHOT_SETS",
                                          (duo._replace(directory=str(self.duo)),)),
                        mock.patch.object(asc_release, "WATCH_FOLDER", str(self.watch))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def uploading(self) -> list[tuple[str, str]]:
        """Stands in for the upload itself; returns the (set id, file name) of every file sent."""
        uploads: list[tuple[str, str]] = []
        for patcher in (mock.patch.object(asc_release, "upload_screenshot_file",
                                          lambda client, set_id, path: uploads.append((set_id, path.name))),
                        mock.patch.object(asc_release, "wait_for_screenshot_processing", lambda client, set_id: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return uploads

    def run_main(self, fake: FakeAsc, env: dict[str, str]) -> int:
        argv = ["asc_release.py", "--version", VERSION, "--build-id", "B9", "--build-number", "8"]
        full_env = {k: v for k, v in os.environ.items() if k not in FLAG_NAMES}
        full_env.update(env)
        products = mock.Mock(return_value=asc_release.ProductSubmitResult("SUB1", 0, False))
        with mock.patch.dict(os.environ, full_env, clear=True), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(asc_release, "client_from_environment", return_value=fake), \
                mock.patch.object(asc_release, "submit_in_app_purchases", products), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                asc_release.main()
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 1
        return 0

    def test_each_watch_size_maps_to_its_display_type(self) -> None:
        (self.watch / "02-ultra-3.png").write_bytes(make_png(422, 514))
        (self.watch / "03-ultra.png").write_bytes(make_png(410, 502))
        (self.watch / "04-series-7.png").write_bytes(make_png(396, 484))
        sets = asc_release.watch_screenshot_sets()
        self.assertEqual(
            [(spec.display_type, [p.name for p in pngs]) for spec, pngs in sets],
            [("APP_WATCH_SERIES_10", ["01-home-glance.png"]),
             ("APP_WATCH_ULTRA", ["02-ultra-3.png", "03-ultra.png"]),
             ("APP_WATCH_SERIES_7", ["04-series-7.png"])],
        )
        expected = {(416, 496): "APP_WATCH_SERIES_10", (422, 514): "APP_WATCH_ULTRA", (410, 502): "APP_WATCH_ULTRA",
                    (396, 484): "APP_WATCH_SERIES_7", (368, 448): "APP_WATCH_SERIES_4", (312, 390): "APP_WATCH_SERIES_3"}
        self.assertEqual(asc_release.WATCH_DISPLAY_TYPES, expected)
        # The Watch sets come after the iPhone and iPad sets (and the iPhone Duo set, when it has PNGs).
        self.duo.mkdir()
        (self.duo / "01-outer.png").write_bytes(make_png(1398, 2034))
        displays = [spec.display_type for spec, _ in asc_release.screenshot_sets()]
        self.assertEqual(displays, ["APP_IPHONE_67", "APP_IPAD_PRO_3GEN_129", "APP_IPHONE_DUO",
                                    "APP_WATCH_SERIES_10", "APP_WATCH_ULTRA", "APP_WATCH_SERIES_7"])

    def test_a_frame_that_is_no_watch_size_or_no_complete_png_fails_before_any_call(self) -> None:
        for name, raw in (("02-phone.png", make_png(1290, 2796)), ("02-cut.png", make_png(416, 496)[:-12])):
            (self.watch / name).write_bytes(raw)
            fake = FakeAsc(screenshots=self.live)
            with self.subTest(name=name):
                self.assertEqual(self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}), 1)
                self.assertEqual(fake.calls, [])
            (self.watch / name).unlink()

    def test_the_watch_set_is_uploaded_with_its_display_type(self) -> None:
        uploads = self.uploading()
        fake = FakeAsc(screenshots={})
        self.assertEqual(self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}), 0)
        self.assertEqual(fake.created_sets, ["APP_IPHONE_67", "APP_IPAD_PRO_3GEN_129", "APP_WATCH_SERIES_10"])
        self.assertEqual(uploads[-1], ("NEW-APP_WATCH_SERIES_10", "01-home-glance.png"))

    def test_the_iphone_duo_set_is_uploaded_when_its_folder_holds_pngs(self) -> None:
        self.duo.mkdir()
        (self.duo / "01-outer.png").write_bytes(make_png(1398, 2034))
        (self.duo / "02-inner.png").write_bytes(make_png(2007, 2853))
        uploads = self.uploading()
        live = {**self.live, "APP_WATCH_SERIES_10": [md5(self.watch_png)]}
        fake = FakeAsc(screenshots=live)
        self.assertEqual(self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}), 0)
        self.assertEqual(fake.created_sets, ["APP_IPHONE_DUO"])
        self.assertEqual(uploads, [("NEW-APP_IPHONE_DUO", "01-outer.png"), ("NEW-APP_IPHONE_DUO", "02-inner.png")])
        # A frame that is not an iPhone Duo size fails before any call.
        (self.duo / "03-phone.png").write_bytes(make_png(1290, 2796))
        fake = FakeAsc(screenshots=live)
        self.assertEqual(self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}), 1)
        self.assertEqual(fake.calls, [])

    def test_a_watch_set_is_replaced_only_when_it_differs(self) -> None:
        same = {**self.live, "APP_WATCH_SERIES_10": [md5(self.watch_png)]}
        fake = FakeAsc(screenshots=same)
        self.assertEqual(self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}), 0)
        self.assertEqual(fake.writes(), [])
        # A different live Watch set is replaced; the others are left alone.
        uploads = self.uploading()
        fake = FakeAsc(screenshots={**self.live, "APP_WATCH_SERIES_10": [md5(b"old")]})
        self.assertEqual(self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}), 0)
        self.assertEqual(uploads, [("SET-APP_WATCH_SERIES_10", "01-home-glance.png")])
        self.assertEqual([c for c in fake.writes() if c[0] == "DELETE"],
                         [("DELETE", "/appScreenshots/APP_WATCH_SERIES_10-0")])

    def test_a_locked_version_only_compares_the_watch_set(self) -> None:
        same = {**self.live, "APP_WATCH_SERIES_10": [md5(self.watch_png)]}
        env = {"UPLOAD_SCREENSHOTS": "true", "SUBMIT_IOS_REVIEW": "true"}
        fake = FakeAsc(state="READY_FOR_REVIEW", attached="B9", screenshots=same)
        self.assertEqual(self.run_main(fake, env), 0)
        self.assertFalse([c for c in fake.writes() if "creenshot" in c[1]])
        # A Watch set that differs while the version is in a review submission stops the run before any write.
        fake = FakeAsc(state="READY_FOR_REVIEW", attached="B9", screenshots=self.live)
        self.assertEqual(self.run_main(fake, env), 1)
        self.assertEqual(fake.writes(), [])

    def test_without_watch_frames_the_live_watch_sets_are_left_alone(self) -> None:
        (self.watch / "01-home-glance.png").unlink()
        fake = FakeAsc(screenshots={**self.live, "APP_WATCH_ULTRA": [md5(b"by hand")],
                                    "APP_IPHONE_DUO": [md5(b"by hand")]})
        self.assertEqual(self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}), 0)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(asc_release.watch_screenshot_sets(), [])


class CreativeFake(FakeAsc):
    """FakeAsc plus the Asset Library, image placements and any number of review drafts."""

    def __init__(self, *, library: list[dict] | None = None, placements: dict[str, dict] | None = None,
                 drafts: list[str] | None = None, draft_items: dict[str, list[dict]] | None = None,
                 processing: dict[str, list[str]] | None = None, **kwargs) -> None:
        super().__init__(drafts=tuple(drafts or ()), **kwargs)
        self.library = [json_copy(image) for image in library or []]
        # placement id -> {"type", "image", "loc", "group", "state"}
        self.placements = {pid: dict(p) for pid, p in (placements or {}).items()}
        self.items = {draft: list(rows) for draft, rows in (draft_items or {}).items()}
        # image id -> the states successive GETs of the image report while Apple processes it
        self.processing = {image_id: list(states) for image_id, states in (processing or {}).items()}
        self.submitted: list[str] = []
        self.placed: list[dict] = []
        self.counter = 0

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def image(self, image_id: str) -> dict:
        return next(image for image in self.library if image["id"] == image_id)

    def get(self, path: str) -> dict:
        parsed = urllib.parse.urlparse(path)
        query = urllib.parse.parse_qs(parsed.query)
        if path == f"/apps/{APP}/assetLibrary":
            self.calls.append(("GET", path))
            return {"data": {"type": "appAssetLibraries", "id": "LIB"}}
        if parsed.path == "/appAssetLibraries/LIB/images":
            self.calls.append(("GET", path))
            assert query.get("filter[category]") == ["CREATIVE_ASSETS"], path
            return {"data": [json_copy(image) for image in self.library]}
        if parsed.path.startswith("/appAssetLibraryImages/") and parsed.path.endswith("/relationships/placements"):
            self.calls.append(("GET", path))
            image_id = parsed.path.split("/")[2]
            return {"data": [{"type": "appAssetLibraryPlacements", "id": pid}
                             for pid, p in self.placements.items() if p["image"] == image_id]}
        if parsed.path.startswith("/appAssetLibraryImages/"):
            self.calls.append(("GET", path))
            image = self.image(parsed.path.split("/")[2])
            states = self.processing.get(image["id"])
            if states:
                image["attributes"]["state"] = states.pop(0)
                if image["attributes"]["state"] == "PREPARE_FOR_SUBMISSION":
                    image["attributes"]["imageAsset"] = {"width": 1, "height": 1}
            return {"data": json_copy(image)}
        if parsed.path == "/appStoreVersionLocalizations/VL/placements":
            self.calls.append(("GET", path))
            wanted = (query.get("filter[placementType]") or [""])[0]
            return {"data": [{"type": "appAssetLibraryPlacements", "id": pid,
                              "attributes": {"placementType": p["type"], "placementGroup": p.get("group", "DEFAULT_PROFILE"),
                                             "state": p.get("state", "ACTIVE")}}
                             for pid, p in self.placements.items() if p["loc"] == "VL" and p["type"] == wanted]}
        if path.startswith(f"/apps/{APP}/reviewSubmissions"):
            self.calls.append(("GET", path))
            assert query.get("filter[state]") == ["READY_FOR_REVIEW"], path
            return {"data": [{"type": "reviewSubmissions", "id": draft} for draft in self.drafts]}
        if parsed.path.startswith("/reviewSubmissions/") and parsed.path.endswith("/items"):
            self.calls.append(("GET", path))
            assert "appAssetLibraryImage" in (query.get("include") or [""])[0], path
            return {"data": [json_copy(item) for item in self.items.get(parsed.path.split("/")[2], [])]}
        return super().get(path)

    def post(self, path: str, body: dict) -> dict:
        data = body["data"]
        if path == "/appAssetLibraryImages":
            self.calls.append(("POST", path))
            image_id = self.next_id("IMG")
            attributes = data["attributes"]
            self.library.append({"id": image_id, "attributes": {**attributes, "state": "AWAITING_UPLOAD",
                                                                 "createdDate": "2026-10-21"}})
            ops = [{"url": f"https://upload.example/{image_id}", "offset": 0, "length": attributes["fileSize"],
                    "method": "PUT", "requestHeaders": [{"name": "Content-Type", "value": "image/png"}]}]
            return {"data": {"type": "appAssetLibraryImages", "id": image_id,
                             "attributes": {"state": "AWAITING_UPLOAD", "uploadOperations": ops}}}
        if path == "/reviewSubmissions":
            self.calls.append(("POST", path))
            draft = self.next_id("DRAFT")
            self.drafts.append(draft)
            self.items[draft] = []
            return {"data": {"type": "reviewSubmissions", "id": draft}}
        if path == "/reviewSubmissionItems":
            self.calls.append(("POST", path))
            rels = data["relationships"]
            draft = rels["reviewSubmission"]["data"]["id"]
            (name, related_rel), = [(k, v) for k, v in rels.items() if k != "reviewSubmission"]
            self.items.setdefault(draft, []).append(
                {"id": self.next_id("ITEM"), "relationships": {name: {"data": dict(related_rel["data"])}}})
            if name == "appAssetLibraryImage":
                self.image(related_rel["data"]["id"])["attributes"]["state"] = "READY_FOR_REVIEW"
            return {"data": {"type": "reviewSubmissionItems", "id": "ITEM"}}
        if path == "/appAssetLibraryPlacements":
            self.calls.append(("POST", path))
            self.placed.append(body)
            rels = data["relationships"]
            swap = (rels.get("placementToSwapOut") or {}).get("data")
            if swap:
                del self.placements[swap["id"]]
            self.placements[self.next_id("PL")] = {
                "type": data["attributes"]["placementType"], "image": rels["image"]["data"]["id"],
                "loc": rels["appStoreVersionLocalization"]["data"]["id"],
                "group": data["attributes"]["placementGroup"]}
            return {"data": {"type": "appAssetLibraryPlacements", "id": "PL"}}
        return super().post(path, body)

    def patch(self, path: str, body: dict) -> dict:
        if path.startswith("/appAssetLibraryImages/"):
            self.calls.append(("PATCH", path))
            image = self.image(path.split("/")[2])
            assert body["data"]["attributes"] == {"uploaded": True}
            image["attributes"]["state"] = "UPLOAD_COMPLETE"
            self.processing.setdefault(image["id"], ["UPLOAD_COMPLETE", "PREPARE_FOR_SUBMISSION"])
            return {"data": body["data"]}
        return super().patch(path, body)

    def request(self, method: str, path: str, body: dict | None = None):
        if method == "PATCH" and path.startswith("/reviewSubmissions/"):
            self.calls.append((method, path))
            draft = path.split("/")[2]
            assert body is not None and body["data"]["attributes"] == {"submitted": True}
            self.submitted.append(draft)
            self.drafts.remove(draft)
            for item in self.items.get(draft, []):
                image_id = related(item, "appAssetLibraryImage")
                if image_id:
                    self.image(image_id)["attributes"]["state"] = "WAITING_FOR_REVIEW"
            return None
        return super().request(method, path, body)

    def upload_bytes(self, url: str, data: bytes, headers: dict, *, method: str = "PUT") -> None:
        self.calls.append(("UPLOAD", url))
        image = self.image(url.rsplit("/", 1)[1])
        assert len(data) == image["attributes"]["fileSize"] and headers == {"Content-Type": "image/png"}


class CreativeAssetTests(unittest.TestCase):
    """Header and search results images: Asset Library upload, their own review, placement."""

    ENV = {"UPLOAD_CREATIVE_ASSETS": "true"}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "creative"
        self.header = self.dir / "header" / "header.png"
        self.search = self.dir / "search-results" / "search.png"
        for path, raw in ((self.header, creative_png(3840, 1646)), (self.search, creative_png(1920, 1280))):
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
        sleep = mock.patch.object(asc_release.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def image(self, image_id: str, path: Path, state: str, created: str = "2026-10-20") -> dict:
        return {"id": image_id, "attributes": {"fileName": path.name, "fileSize": path.stat().st_size,
                                               "state": state, "createdDate": created,
                                               "imageAsset": {"width": 1, "height": 1}}}

    def run_main(self, fake: FakeAsc, env: dict[str, str] | None = None, *extra: str,
                 creative_dir: Path | None = None) -> int:
        argv = ["asc_release.py", "--version", VERSION, "--creative-dir", str(creative_dir or self.dir), *extra]
        full_env = {k: v for k, v in os.environ.items() if k not in FLAG_NAMES}
        full_env.update(env if env is not None else self.ENV)

        def products(client, app_id, *, submission_id=None, **_):
            return asc_release.ProductSubmitResult(submission_id or "", 0, False)

        with mock.patch.dict(os.environ, full_env, clear=True), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(asc_release, "client_from_environment", return_value=fake), \
                mock.patch.object(asc_release, "submit_in_app_purchases", side_effect=products), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                asc_release.main()
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 1
        return 0

    def submit(self, fake: FakeAsc) -> int:
        return self.run_main(fake, {**self.ENV, "SUBMIT_IOS_REVIEW": "true"},
                             "--build-id", "B9", "--build-number", "8")

    def image_review_drafts(self, fake: CreativeFake) -> set[str]:
        return {draft for draft, items in fake.items.items() if any(related(i, "appAssetLibraryImage") for i in items)}

    def test_new_images_are_uploaded_once_and_reviewed_on_their_own(self) -> None:
        fake = CreativeFake()
        self.assertEqual(self.run_main(fake), 0)
        posts = [c for c in fake.writes() if c == ("POST", "/appAssetLibraryImages")]
        self.assertEqual(len(posts), 2)
        self.assertEqual([c[0] for c in fake.writes() if c[1].startswith(("https://upload", "/appAssetLibraryImages/"))],
                         ["UPLOAD", "PATCH", "UPLOAD", "PATCH"])
        uploaded = {image["attributes"]["fileName"]: image["attributes"] for image in fake.library}
        self.assertEqual(set(uploaded), {"header.png", "search.png"})
        self.assertEqual({a["category"] for a in uploaded.values()}, {"CREATIVE_ASSETS"})
        self.assertEqual(uploaded["header.png"]["fileSize"], self.header.stat().st_size)
        # One submission of their own, holding exactly the two images, submitted.
        self.assertEqual(len(fake.submitted), 1)
        held = [related(item, "appAssetLibraryImage") for item in fake.items[fake.submitted[0]]]
        self.assertEqual(sorted(held), sorted(image["id"] for image in fake.library))
        self.assertEqual(fake.placed, [], "images are placed only once Apple approves them")
        # The next run finds them in review: nothing is uploaded, reviewed or placed again.
        before = len(fake.writes())
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual(len(fake.writes()), before)

    def test_approved_and_placed_images_are_recognised(self) -> None:
        library = [self.image("HDR", self.header, "APPROVED"), self.image("SRCH", self.search, "APPROVED")]
        placements = {"PL-H": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "HDR", "loc": "VL"},
                      "PL-S": {"type": "APP_STORE_SEARCH_RESULTS_ASSET", "image": "SRCH", "loc": "VL"}}
        fake = CreativeFake(library=library, placements=placements)
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn(("GET", "/appAssetLibraryImages/HDR/relationships/placements?limit=50"), fake.calls)

    def test_approved_images_are_placed_on_the_version(self) -> None:
        fake = CreativeFake(library=[self.image("H", self.header, "APPROVED"), self.image("S", self.search, "APPROVED")],
                            # Placed on another version's localization only.
                            placements={"PL-OLD": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": "VL-OLD"}})
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual(fake.placed, [
            {"data": {"type": "appAssetLibraryPlacements",
                      "attributes": {"placementType": placement, "placementGroup": "DEFAULT_PROFILE"},
                      "relationships": {"image": {"data": {"type": "appAssetLibraryImages", "id": image_id}},
                                        "appStoreVersionLocalization": {"data": {"type": "appStoreVersionLocalizations",
                                                                                 "id": "VL"}}}}}
            for placement, image_id in (("PRODUCT_PAGE_HEADER_ASSET", "H"), ("APP_STORE_SEARCH_RESULTS_ASSET", "S"))
        ])
        self.assertNotIn(("POST", "/appAssetLibraryImages"), fake.calls)
        self.assertEqual(fake.submitted, [])
        # A second run finds both placed and writes nothing.
        before = len(fake.writes())
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual(len(fake.writes()), before)

    def test_another_image_in_the_slot_is_swapped_out_in_one_request(self) -> None:
        fake = CreativeFake(library=[self.image("H", self.header, "APPROVED"), self.image("S", self.search, "IN_REVIEW")],
                            placements={"PL-OLD": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "OLDIMG", "loc": "VL"}})
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual(len(fake.placed), 1)
        rels = fake.placed[0]["data"]["relationships"]
        self.assertEqual(rels["placementToSwapOut"], {"data": {"type": "appAssetLibraryPlacements", "id": "PL-OLD"}})
        self.assertEqual(rels["image"]["data"]["id"], "H")
        self.assertNotIn("PL-OLD", fake.placements)

    def test_an_interrupted_upload_is_uploaded_again(self) -> None:
        stale = self.image("STALE", self.header, "AWAITING_UPLOAD", created="2026-10-21")
        fake = CreativeFake(library=[stale, self.image("S", self.search, "APPROVED")])
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual([c for c in fake.writes() if c == ("POST", "/appAssetLibraryImages")],
                         [("POST", "/appAssetLibraryImages")])
        reviewed = {related(item, "appAssetLibraryImage") for item in fake.items[fake.submitted[0]]}
        self.assertNotIn("STALE", reviewed)
        self.assertEqual(len(reviewed), 1)
        # Failed and archived images do not count either.
        for state in ("FAILED", "ARCHIVED"):
            fake = CreativeFake(library=[self.image("OLD", self.header, state), self.image("S", self.search, "APPROVED")])
            with self.subTest(state=state):
                self.assertEqual(self.run_main(fake), 0)
                self.assertIn(("POST", "/appAssetLibraryImages"), fake.calls)

    def test_an_image_is_matched_by_file_name_and_size(self) -> None:
        # A library image with the header's file name but another size came from another file:
        # the header is uploaded as a new image, and the other one is never placed.
        other = self.image("OTHER", self.header, "APPROVED")
        other["attributes"]["fileSize"] += 1
        fake = CreativeFake(library=[other, self.image("S", self.search, "IN_REVIEW")])
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual([c for c in fake.writes() if c == ("POST", "/appAssetLibraryImages")],
                         [("POST", "/appAssetLibraryImages")])
        uploaded = [image for image in fake.library if image["id"] not in {"OTHER", "S"}]
        self.assertEqual([image["attributes"]["fileSize"] for image in uploaded], [self.header.stat().st_size])
        self.assertEqual(fake.placed, [])
        self.assertEqual([related(i, "appAssetLibraryImage") for i in fake.items[fake.submitted[0]]],
                         [uploaded[0]["id"]])

    def test_an_uploaded_image_still_processing_is_reviewed_once_ready(self) -> None:
        fake = CreativeFake(library=[self.image("H", self.header, "UPLOAD_COMPLETE"), self.image("S", self.search, "APPROVED")],
                            processing={"H": ["UPLOAD_COMPLETE", "COMPLETE", "PREPARE_FOR_SUBMISSION"]})
        self.assertEqual(self.run_main(fake), 0)
        self.assertNotIn(("POST", "/appAssetLibraryImages"), fake.calls)
        self.assertEqual(fake.processing["H"], [], "the run waits until Apple has processed the image")
        self.assertEqual([related(i, "appAssetLibraryImage") for i in fake.items[fake.submitted[0]]], ["H"])

    def test_processing_failure_or_timeout_stops_before_review(self) -> None:
        fake = CreativeFake(library=[self.image("H", self.header, "UPLOAD_COMPLETE")], processing={"H": ["FAILED"]})
        self.assertEqual(self.run_main(fake), 1)
        self.assertEqual(fake.submitted, [])
        self.assertNotIn(("POST", "/reviewSubmissionItems"), fake.calls)
        fake = CreativeFake(library=[self.image("H", self.header, "UPLOAD_COMPLETE")],
                            processing={"H": ["UPLOAD_COMPLETE"] * 5})
        with mock.patch.object(asc_release, "CREATIVE_PROCESSING_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.run_main(fake), 1)
        self.assertNotIn(("POST", "/reviewSubmissionItems"), fake.calls)

    def test_images_in_review_are_neither_uploaded_nor_placed(self) -> None:
        for state in ("WAITING_FOR_REVIEW", "IN_REVIEW", "ACCEPTED"):
            fake = CreativeFake(library=[self.image("H", self.header, state), self.image("S", self.search, state)])
            with self.subTest(state=state):
                self.assertEqual(self.run_main(fake), 0)
                self.assertEqual(fake.writes(), [])

    def test_a_rejected_image_stops_the_run_before_any_write(self) -> None:
        rejected = self.image("H", self.header, "REJECTED")
        rejected["attributes"]["stateDetails"] = [{"code": "IMAGE_QUALITY"}]
        fake = CreativeFake(library=[rejected, self.image("S", self.search, "PREPARE_FOR_SUBMISSION")])
        self.assertEqual(self.run_main(fake), 1)
        self.assertEqual(fake.writes(), [])

    def test_a_locked_version_takes_no_new_images(self) -> None:
        for state, attached in (("WAITING_FOR_REVIEW", "B9"), ("READY_FOR_REVIEW", "B9")):
            for library in ([], [self.image("H", self.header, "PREPARE_FOR_SUBMISSION")]):
                fake = CreativeFake(state=state, attached=attached, library=library)
                with self.subTest(state=state, library=bool(library)):
                    self.assertEqual(self.run_main(fake), 1)
                    self.assertEqual(fake.writes(), [])

    def test_an_approved_image_on_a_locked_version_waits_without_failing(self) -> None:
        # Approval can land between a submit and a re-run of the store job.
        fake = CreativeFake(state="WAITING_FOR_REVIEW", attached="B9",
                            library=[self.image("H", self.header, "APPROVED"), self.image("S", self.search, "IN_REVIEW")])
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual(fake.writes(), [])

    def test_images_never_go_into_the_versions_draft(self) -> None:
        # The version's draft is picked (and reserved) before any write; the
        # empty draft goes to the version and the images get their own submission.
        fake = CreativeFake(drafts=["EMPTY"], draft_items={"EMPTY": []})
        self.assertEqual(self.submit(fake), 0)
        self.assertEqual(len(fake.submitted), 2)
        self.assertEqual(fake.submitted[-1], "EMPTY", "the version went out in the reserved draft")
        self.assertEqual([related(i, "appStoreVersion") for i in fake.items["EMPTY"]], ["V1"])
        self.assertEqual(self.image_review_drafts(fake), {fake.submitted[0]})
        self.assertNotIn("EMPTY", self.image_review_drafts(fake))

    def test_a_creative_draft_is_never_used_for_the_version(self) -> None:
        library = [self.image("H", self.header, "WAITING_FOR_REVIEW"), self.image("S", self.search, "APPROVED")]
        placements = {"P": {"type": "APP_STORE_SEARCH_RESULTS_ASSET", "image": "S", "loc": "VL"}}
        # A draft holding an image (left by hand or another tool) is not the version's.
        fake = CreativeFake(library=library, placements=placements, drafts=["IMAGES"],
                            draft_items={"IMAGES": [image_item("OTHER")]})
        self.assertEqual(self.submit(fake), 0)
        self.assertEqual(fake.items["IMAGES"], [image_item("OTHER")])
        self.assertEqual(len(fake.submitted), 1)
        self.assertNotEqual(fake.submitted[0], "IMAGES")
        self.assertIn(("POST", "/reviewSubmissions"), fake.calls)

    def test_a_ready_for_review_version_its_draft_does_not_hold_stops_before_any_write(self) -> None:
        # Images a locked version can take (both in review), so only the draft check can stop the run.
        library = [self.image("H", self.header, "IN_REVIEW"), self.image("S", self.search, "IN_REVIEW")]
        # The only draft holding the version also holds an image, so it is not the version's draft.
        for drafts, items in (([], {}), (["MIXED"], {"MIXED": [version_item(), image_item("X")]})):
            fake = CreativeFake(state="READY_FOR_REVIEW", library=library, drafts=drafts, draft_items=items)
            with self.subTest(drafts=drafts):
                self.assertEqual(self.submit(fake), 1)
                self.assertEqual(fake.writes(), [])

    def test_the_draft_holding_this_version_is_reserved_over_others(self) -> None:
        fake = CreativeFake(state="READY_FOR_REVIEW", drafts=["EMPTY", "MINE"],
                            draft_items={"EMPTY": [], "MINE": [version_item()]},
                            library=[self.image("H", self.header, "APPROVED"), self.image("S", self.search, "IN_REVIEW")],
                            placements={"P": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": "VL"}})
        self.assertEqual(self.submit(fake), 0)
        self.assertEqual(fake.submitted, ["MINE"])
        self.assertEqual(fake.items["EMPTY"], [])

    def test_a_rerun_finishes_the_draft_an_interrupted_review_left(self) -> None:
        library = [self.image("H", self.header, "READY_FOR_REVIEW"), self.image("S", self.search, "PREPARE_FOR_SUBMISSION")]
        # A draft already holding some of the images is finished before an empty one.
        fake = CreativeFake(library=library, drafts=["EMPTY", "PART"],
                            draft_items={"EMPTY": [], "PART": [image_item("H")]})
        self.assertEqual(self.run_main(fake), 0)
        self.assertNotIn(("POST", "/reviewSubmissions"), fake.calls)
        self.assertEqual(fake.submitted, ["PART"])
        self.assertEqual(sorted(related(i, "appAssetLibraryImage") for i in fake.items["PART"]), ["H", "S"])
        self.assertEqual(fake.items["EMPTY"], [])
        # Else an empty draft (a run stopped before it added the images) is used.
        fake = CreativeFake(library=[self.image("S", self.search, "PREPARE_FOR_SUBMISSION")],
                            drafts=["EMPTY"], draft_items={"EMPTY": []})
        self.assertEqual(self.run_main(fake), 0)
        self.assertNotIn(("POST", "/reviewSubmissions"), fake.calls)
        self.assertEqual(fake.submitted, ["EMPTY"])
        # A draft holding anything else is left alone and a new one is created.
        fake = CreativeFake(library=[self.image("S", self.search, "PREPARE_FOR_SUBMISSION")],
                            drafts=["OTHER"], draft_items={"OTHER": [version_item("V0")]})
        self.assertEqual(self.run_main(fake), 0)
        self.assertIn(("POST", "/reviewSubmissions"), fake.calls)
        self.assertEqual(fake.items["OTHER"], [version_item("V0")])
        self.assertNotIn("OTHER", fake.submitted)

    def test_a_creative_submission_holding_other_items_is_not_submitted(self) -> None:
        class Crowded(CreativeFake):
            def post(self, path: str, body: dict) -> dict:
                result = super().post(path, body)
                if path == "/reviewSubmissions":
                    # Something else lands in the new draft before it is submitted.
                    self.items[result["data"]["id"]].append(version_item("V0"))
                return result

        fake = Crowded(library=[self.image("S", self.search, "PREPARE_FOR_SUBMISSION")])
        self.assertEqual(self.run_main(fake), 1)
        self.assertEqual(fake.submitted, [])

    def test_images_apple_would_refuse_fail_before_any_call(self) -> None:
        cases = [
            (self.header, creative_png(3840, 2160), "header.png"),
            (self.header, creative_png(3840, 1646, 6), "header.png"),
            (self.header, jpeg(3840, 1646), "header.jpg"),
            (self.header, jpeg(3840, 1646), "header.png"),
            (self.search, creative_png(3000, 2001), "search.png"),
            (self.search, creative_png(1800, 1200), "search.png"),
            (self.search, creative_png(3900, 2600), "search.png"),
            (self.search, creative_png(1920, 1280, 4), "search.png"),
            (self.search, make_png(1920, 1280, 2, chunks=((b"tRNS", b"\x00" * 6),)), "search.png"),
            (self.search, jpeg(4000, 2666), "search.jpg"),
            # The search slot takes both formats, so only the name check stops these.
            (self.search, jpeg(1920, 1280), "search.png"),
            (self.search, creative_png(1920, 1280), "search.jpg"),
            (self.search, b"GIF89a", "search.png"),
            # Truncated or corrupt files with the right header fail too.
            (self.header, creative_png(3840, 1646)[:-12], "header.png"),
            (self.header, creative_png(3840, 1646)[:8 + 25], "header.png"),
            (self.search, creative_png(1920, 1280)[: len(creative_png(1920, 1280)) // 2], "search.png"),
        ]
        for path, raw, name in cases:
            original = path.read_bytes()
            path.unlink()
            (path.parent / name).write_bytes(raw)
            fake = CreativeFake()
            with self.subTest(slot=path.parent.name, name=name, raw=raw[:26]):
                self.assertEqual(self.run_main(fake), 1)
                self.assertEqual(fake.calls, [])
            (path.parent / name).unlink()
            path.write_bytes(original)

    def test_sizes_and_formats_apple_takes_pass(self) -> None:
        for name, raw in (("search.jpg", jpeg(1920, 1280)), ("search.jpeg", jpeg(3840, 2560)),
                          ("search.png", creative_png(3840, 2560)), ("search.png", creative_png(3000, 2000))):
            self.search.unlink()
            (self.search.parent / name).write_bytes(raw)
            with self.subTest(name=name, raw=raw[:26]):
                sources = quiet(asc_release.creative_sources, self.dir)
                self.assertEqual([(p, path.name) for p, _, path in sources],
                                 [("PRODUCT_PAGE_HEADER_ASSET", "header.png"), ("APP_STORE_SEARCH_RESULTS_ASSET", name)])
            (self.search.parent / name).unlink()
            self.search.write_bytes(creative_png(1920, 1280))

    def test_a_slot_takes_one_image_and_at_least_one_slot_needs_one(self) -> None:
        (self.header.parent / "header-2.png").write_bytes(creative_png(3840, 1646))
        fake = CreativeFake()
        self.assertEqual(self.run_main(fake), 1)
        self.assertEqual(fake.calls, [])
        (self.header.parent / "header-2.png").unlink()
        # Other files (notes, .DS_Store) do not count; an empty slot is left alone.
        (self.header.parent / "README.md").write_text("notes")
        self.header.unlink()
        self.assertEqual([p for p, _, _ in quiet(asc_release.creative_sources, self.dir)],
                         ["APP_STORE_SEARCH_RESULTS_ASSET"])
        self.search.unlink()
        fake = CreativeFake()
        self.assertEqual(self.run_main(fake), 1)
        self.assertEqual(fake.calls, [])

    def preflight(self, fake: FakeAsc, *, submit: bool = False) -> tuple[int, str]:
        return run_preflight(fake, "--creative", *(["--submit"] if submit else []), creative_dir=self.dir)

    def test_the_preflight_stops_before_the_build_when_the_version_cannot_take_the_images(self) -> None:
        # In a review submission the version takes no new image: found before the build, not after.
        for library in ([], [self.image("H", self.header, "PREPARE_FOR_SUBMISSION")]):
            fake = CreativeFake(state="READY_FOR_REVIEW", library=library,
                                drafts=["MINE"], draft_items={"MINE": [version_item()]})
            with self.subTest(library=bool(library)):
                self.assertEqual(self.preflight(fake)[0], 1)
            self.assertEqual(fake.writes(), [])
        # A rejected image stops it in any state.
        rejected = [self.image("H", self.header, "REJECTED"), self.image("S", self.search, "APPROVED")]
        self.assertEqual(self.preflight(CreativeFake(library=rejected))[0], 1)

    def test_the_preflight_passes_what_the_store_step_can_do(self) -> None:
        cases = [
            # An editable version takes new images (uploaded by the store step, not here).
            ("PREPARE_FOR_SUBMISSION", []),
            # Images in review or approved wait for a later run without failing.
            ("READY_FOR_REVIEW", [self.image("H", self.header, "IN_REVIEW"), self.image("S", self.search, "APPROVED")]),
        ]
        for state, library in cases:
            fake = CreativeFake(state=state, library=library, drafts=["MINE"], draft_items={"MINE": [version_item()]})
            with self.subTest(state=state):
                code, output = self.preflight(fake, submit=True)
                self.assertEqual(code, 0, output)
                self.assertIn(f"exists ({state})", output)
                self.assertEqual(fake.writes(), [])

    def test_the_preflight_checks_a_ready_for_review_versions_draft(self) -> None:
        library = [self.image("H", self.header, "APPROVED"), self.image("S", self.search, "IN_REVIEW")]
        placements = {"P": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": "VL"}}
        for drafts, items in (([], {}), (["MIXED"], {"MIXED": [version_item(), image_item("X")]})):
            fake = CreativeFake(state="READY_FOR_REVIEW", library=library, placements=placements,
                                drafts=drafts, draft_items=items)
            with self.subTest(drafts=drafts):
                self.assertEqual(self.preflight(fake, submit=True)[0], 1)
            self.assertEqual(fake.writes(), [])
            # Without review submission the draft does not matter.
            code, output = self.preflight(fake, submit=False)
            self.assertEqual(code, 0, output)
            self.assertIn("exists (READY_FOR_REVIEW)", output)

    def test_dry_run_checks_the_images_without_calls(self) -> None:
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k not in FLAG_NAMES}
        with mock.patch.dict(os.environ, {**env, **self.ENV}, clear=True), \
                mock.patch.object(sys, "argv", ["asc_release.py", "--dry-run", "--creative-dir", str(self.dir)]), \
                mock.patch.object(asc_release, "client_from_environment") as client, \
                redirect_stdout(out), redirect_stderr(out):
            asc_release.main()
        client.assert_not_called()
        self.assertIn("creative=True", out.getvalue())
        self.header.write_bytes(creative_png(3840, 1646, 6))
        with mock.patch.dict(os.environ, {**env, **self.ENV}, clear=True), \
                mock.patch.object(sys, "argv", ["asc_release.py", "--dry-run", "--creative-dir", str(self.dir)]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            asc_release.main()


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
        self.assertIn("IOS_BUILD_POLL_SECONDS=45", output)
        self.assertIn("IOS_BUILD_WAIT_MINUTES=120 (default)", output)
        self.assertIn("every store operation is off", output)

    def test_invalid_values_are_shown_as_invalid(self) -> None:
        # Before, "maybe" was echoed like a value and an invalid master switch
        # printed "off"; the release run rejects both.
        output = self.printed({"IOS_RELEASE_AUTOMATION": "maybe", "IOS_SUBMIT_REVIEW": "enabled",
                               "IOS_BUILD_WAIT_MINUTES": "999"})
        self.assertIn("IOS_RELEASE_AUTOMATION=INVALID 'maybe'", output)
        self.assertIn("IOS_SUBMIT_REVIEW=INVALID 'enabled'", output)
        self.assertIn("IOS_BUILD_WAIT_MINUTES=INVALID '999'", output)
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
