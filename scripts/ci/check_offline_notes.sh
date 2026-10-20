#!/bin/bash
# ios.build.post_archive: the archive bundles every tracked note page.
#
# The inline check of PCM Notes' former ios-release.yml ("Verify the archive bundles every
# tracked note page"), unchanged: OfflineNotes/manifest.json in the archived app must count
# the chapters and pages git tracks for every subject, with one PDF per chapter. store-release
# runs it from the app root with APP_PATH (the archived .app) set and no secret in the
# environment.
set -euo pipefail
: "${APP_PATH:?APP_PATH (the archived app) is not set}"
python3 - "$APP_PATH/OfflineNotes" <<'PY'
import json, subprocess, sys
from pathlib import Path
notes = Path(sys.argv[1])
manifest = json.loads((notes / "manifest.json").read_text())
problems = []
for entry in manifest["subjects"]:
    slug = entry["slug"]
    # git knows every tracked page even where the checkout is sparse.
    tracked = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", f"HEAD:{slug}"], text=True
    ).split("\n")
    pages = [p for p in tracked if p.lower().endswith((".jpg", ".jpeg"))]
    chapters = {p.split("/")[0] for p in pages}
    pdfs = list((notes / slug).glob("*.pdf"))
    bundled = (entry["chapters"], entry["pages"], len(pdfs))
    if bundled != (len(chapters), len(pages), len(chapters)):
        problems.append(f"{slug}: bundled {bundled[0]} chapters, {bundled[1]} pages, "
                        f"{bundled[2]} PDFs; git tracks {len(chapters)} chapters, {len(pages)} pages")
if problems:
    sys.exit("error: " + "; ".join(problems))
print(f"bundled {manifest['chapterCount']} chapters and {manifest['pageCount']} pages, matching git")
PY
