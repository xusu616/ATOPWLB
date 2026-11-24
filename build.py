"""Build an installable, reproducible module ZIP without Android SDK/Gradle."""

import hashlib
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
MODULE_FILES = (
    "module.prop",
    "customize.sh",
    "service.sh",
    "monitor.sh",
    "common.sh",
    "config.example.conf",
    "skip_mount",
    "META-INF/com/google/android/update-binary",
    "META-INF/com/google/android/updater-script",
    "README.md",
    "LICENSE",
    "LICENSE.magisk",
    "THIRD_PARTY_NOTICES.md",
)


def build() -> Path:
    properties = dict(
        line.split("=", 1)
        for line in (ROOT / "module.prop").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    output_dir = ROOT / "dist"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"{properties['id']}-{properties['version']}.zip"
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in MODULE_FILES:
            source = ROOT / filename
            # Shell scripts must be UTF-8 without BOM and with LF on Android.
            data = source.read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode("utf-8")
            info = zipfile.ZipInfo(filename, date_time=(2026, 11, 24, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            executable = filename.endswith(".sh") or filename.endswith("/update-binary")
            mode = 0o100755 if executable else 0o100644
            info.external_attr = mode << 16
            archive.writestr(info, data)

    with zipfile.ZipFile(output_path) as archive:
        invalid_member = archive.testzip()
        if invalid_member:
            raise RuntimeError(f"ZIP verification failed: {invalid_member}")
        if tuple(archive.namelist()) != MODULE_FILES:
            raise RuntimeError("Unexpected ZIP layout")
    checksum = hashlib.sha256(output_path.read_bytes()).hexdigest()
    checksum_path = output_path.with_suffix(".zip.sha256")
    checksum_path.write_text(f"{checksum}  {output_path.name}\n", encoding="ascii")
    print(output_path)
    print(f"SHA256: {checksum}")
    return output_path


if __name__ == "__main__":
    build()
