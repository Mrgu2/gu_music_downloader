#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM_PREFIXES = (
    "/System/",
    "/usr/lib/",
    "/usr/libexec/",
)
HOMEBREW_PREFIXES = (
    "/opt/homebrew/",
    "/usr/local/",
)


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def list_dependencies(binary: Path) -> list[str]:
    output = run("otool", "-L", str(binary))
    lines = output.splitlines()[1:]
    dependencies: list[str] = []
    for line in lines:
        path = line.strip().split(" (compatibility version", 1)[0]
        if path:
            dependencies.append(path)
    return dependencies


def list_rpaths(binary: Path) -> list[str]:
    output = run("otool", "-l", str(binary))
    lines = output.splitlines()
    rpaths: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for candidate in lines[index + 1 : index + 6]:
            stripped = candidate.strip()
            if not stripped.startswith("path "):
                continue
            rpaths.append(stripped.split(" (offset ", 1)[0][5:])
            break
    return rpaths


def is_bundle_candidate(path: str) -> bool:
    return path.startswith(HOMEBREW_PREFIXES) and not path.startswith(SYSTEM_PREFIXES)


def resolve_special_path(base_binary: Path, ref: str) -> Path | None:
    if ref.startswith("@loader_path/") or ref.startswith("@executable_path/"):
        suffix = ref.split("/", 1)[1]
        candidate = (base_binary.parent / suffix).resolve()
        return candidate if candidate.exists() else None
    if ref.startswith("@rpath/"):
        suffix = ref[len("@rpath/") :]
        for rpath in list_rpaths(base_binary):
            if rpath.startswith("@loader_path/") or rpath.startswith("@executable_path/"):
                prefix = resolve_special_path(base_binary, rpath)
                if prefix is None:
                    continue
                candidate = (prefix / suffix).resolve()
            else:
                candidate = (Path(rpath) / suffix).resolve()
            if candidate.exists():
                return candidate
    return None


def rewrite_dependency(binary: Path, old: str, new: str) -> None:
    subprocess.run(
        ["install_name_tool", "-change", old, new, str(binary)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def rewrite_id(binary: Path, new_id: str) -> None:
    subprocess.run(
        ["install_name_tool", "-id", new_id, str(binary)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ad_hoc_sign(target: Path) -> None:
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(target)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def bundle_ffmpeg(app_path: Path, ffmpeg_source: Path) -> None:
    frameworks_dir = app_path / "Contents" / "Frameworks"
    bin_dir = frameworks_dir / "bin"
    libs_dir = frameworks_dir / "ffmpeg-libs"
    if bundled_ffmpeg := bin_dir / "ffmpeg":
        if bundled_ffmpeg.exists():
            bundled_ffmpeg.unlink()
    if libs_dir.exists():
        shutil.rmtree(libs_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    libs_dir.mkdir(parents=True, exist_ok=True)

    bundled_ffmpeg = bin_dir / "ffmpeg"
    shutil.copy2(ffmpeg_source, bundled_ffmpeg)
    bundled_ffmpeg.chmod(0o755)

    queue: list[tuple[Path, Path]] = [(bundled_ffmpeg, ffmpeg_source.resolve())]
    copied: dict[str, tuple[Path, Path]] = {}

    while queue:
        current_target, current_source = queue.pop(0)
        for dependency_ref in list_dependencies(current_source):
            if dependency_ref.startswith(SYSTEM_PREFIXES):
                continue
            if dependency_ref.startswith(HOMEBREW_PREFIXES):
                source = Path(dependency_ref).resolve()
            else:
                resolved = resolve_special_path(current_source, dependency_ref)
                if resolved is None:
                    continue
                source = resolved
            if not is_bundle_candidate(str(source)):
                continue
            if not source.exists():
                raise FileNotFoundError(f"缺少 ffmpeg 依赖：{source}")
            target = libs_dir / source.name
            source_key = str(source)
            if source_key not in copied:
                shutil.copy2(source, target)
                target.chmod(0o755)
                copied[source_key] = (target, source)
                queue.append((target, source))
            if current_target == bundled_ffmpeg:
                new_ref = f"@executable_path/../ffmpeg-libs/{target.name}"
            else:
                new_ref = f"@loader_path/{target.name}"
            rewrite_dependency(current_target, dependency_ref, new_ref)

    for target, _source in copied.values():
        rewrite_id(target, f"@loader_path/{target.name}")
        ad_hoc_sign(target)

    ad_hoc_sign(bundled_ffmpeg)
    subprocess.run(
        ["codesign", "--deep", "--force", "--sign", "-", str(app_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: bundle_macos_ffmpeg.py <app_path> <ffmpeg_source>")
    app_path = Path(sys.argv[1]).resolve()
    ffmpeg_source = Path(sys.argv[2]).resolve()
    if not app_path.exists():
        raise SystemExit(f"app bundle not found: {app_path}")
    if not ffmpeg_source.exists():
        raise SystemExit(f"ffmpeg source not found: {ffmpeg_source}")
    bundle_ffmpeg(app_path, ffmpeg_source)


if __name__ == "__main__":
    main()
