"""Install the approved pinned runtime inside .local; never open/register the app."""

import hashlib
import json
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    pin = json.loads((root / "src/project_log/libreoffice_runtime.json").read_text())
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This pinned installer supports macOS Apple Silicon only.")
    target = root / ".local/runtimes" / ("libreoffice-" + pin["version"])
    if target.exists():
        raise SystemExit("Runtime destination already exists; no overwrite performed.")
    download = root / ".local/runtimes/downloads" / pin["filename"]
    download.parent.mkdir(parents=True, exist_ok=True)
    if not download.exists():
        subprocess.run(
            [
                "/usr/bin/curl",
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--max-time",
                "180",
                "--output",
                str(download),
                pin["url"],
            ],
            check=True,
        )
    with download.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != pin["sha256"]:
        raise SystemExit("Official distribution SHA-256 mismatch; not mounted or executed.")
    with tempfile.TemporaryDirectory(prefix="lo-install-") as temporary:
        mount = Path(temporary) / "mount"
        subprocess.run(
            [
                "/usr/bin/hdiutil",
                "attach",
                "-readonly",
                "-nobrowse",
                "-noautoopen",
                "-mountpoint",
                str(mount),
                str(download),
            ],
            check=True,
        )
        try:
            bundle = mount / "LibreOffice.app"
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)], check=True
            )
            staging = Path(temporary) / "runtime"
            staging.mkdir()
            shutil.copytree(bundle, staging / "LibreOffice.app", symlinks=True)
            (staging / "install.json").write_text(json.dumps(pin, indent=2) + "\n")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), target)
        finally:
            subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], check=True)
    print("Installed pinned local runtime:", target)


if __name__ == "__main__":
    main()
