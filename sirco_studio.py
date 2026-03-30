#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
from hashlib import md5
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, NoReturn, TypedDict, cast


PROJECT_FILE = "studio.json"
PROJECT_VERSION = 1
SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_COMPRESSION = "gzip"
DEFAULT_BLOCK_SIZE = 131072

CompressionKey = Literal["xz", "gzip", "lz4"]
WorkflowStepKey = Literal["overview", "project", "unpack", "shell", "details", "compression", "build"]


class CompressionChoice(TypedDict):
    title: str
    summary: str
    details: str


class ProjectPaths(TypedDict):
    project: Path
    config: Path
    base_iso: Path
    iso: Path
    rootfs: Path
    out: Path
    tmp: Path


class StoredStudioProject(TypedDict):
    project_version: int
    name: str
    base_iso: str
    volume_id: str | None
    rootfs_squashfs: str | None
    squashfs_compression: str | None
    squashfs_block_size: int | None


class StudioProject(StoredStudioProject):
    _paths: ProjectPaths


class SquashfsDetails(TypedDict):
    compression: str
    block_size: int


UiSuccessCallback = Callable[[], None]
UiLogEvent = tuple[Literal["log"], str]
UiFailedToStartEvent = tuple[Literal["failed_to_start"], str, str]
UiFinishedEvent = tuple[Literal["finished"], int, str, str | None, UiSuccessCallback | None]
UiEvent = UiLogEvent | UiFailedToStartEvent | UiFinishedEvent

COMPRESSION_CHOICES: dict[CompressionKey, CompressionChoice] = {
    "xz": {
        "title": "XZ",
        "summary": "Smallest files, slowest rebuilds",
        "details": "Use when download size matters most. It compresses the hardest and usually takes the longest.",
    },
    "gzip": {
        "title": "GZIP",
        "summary": "Balanced size and speed",
        "details": "Good default for everyday work. It keeps sizes reasonable without dragging rebuild times too much.",
    },
    "lz4": {
        "title": "LZ4",
        "summary": "Fastest rebuilds, biggest files",
        "details": "Best when you want quick iteration while testing. Expect a larger ISO in exchange for speed.",
    },
}

WORKFLOW_STEPS: list[tuple[WorkflowStepKey, str, str]] = [
    ("overview", "Overview", "See the whole workflow at a glance."),
    ("project", "Project Setup", "Import a base ISO or open an existing studio."),
    ("unpack", "Unpack ISO", "Extract the ISO tree and editable rootfs."),
    ("shell", "Edit Shell", "Open the chroot in a real terminal window."),
    ("details", "ISO Details", "Volume label, boot config, and manifest files."),
    ("compression", "Compression", "Choose how the rebuilt squashfs trades size for speed."),
    ("build", "Build ISO", "Repack the rootfs, refresh metadata, and make a bootable ISO."),
]


def info(message: str) -> None:
    print(message)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def die(message: str, code: int = 1) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if capture:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=check,
        text=True,
    )


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def require_commands(*names: str) -> None:
    missing = [name for name in names if not command_exists(name)]
    if missing:
        die(f"missing required commands: {', '.join(missing)}")


def project_paths(project_dir: Path) -> ProjectPaths:
    return {
        "project": project_dir,
        "config": project_dir / PROJECT_FILE,
        "base_iso": project_dir / "base.iso",
        "iso": project_dir / "iso",
        "rootfs": project_dir / "rootfs",
        "out": project_dir / "out",
        "tmp": project_dir / "tmp",
    }


def require_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field} in {PROJECT_FILE}")
    return value


def require_optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid {field} in {PROJECT_FILE}")
    return value


def require_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"invalid {field} in {PROJECT_FILE}")
    return value


def read_project(project_dir: Path) -> StudioProject:
    config_path = project_dir / PROJECT_FILE
    if not config_path.exists():
        raise ValueError(f"{project_dir} is not a Sirco Studio project ({PROJECT_FILE} missing)")
    with config_path.open() as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} does not contain a valid JSON object")
    data = cast(dict[str, object], raw)
    project_version = data.get("project_version")
    if project_version != PROJECT_VERSION:
        raise ValueError(
            f"unsupported project version {project_version}, "
            f"expected {PROJECT_VERSION}"
        )
    return {
        "project_version": PROJECT_VERSION,
        "name": require_str(data.get("name"), "name"),
        "base_iso": require_str(data.get("base_iso"), "base_iso"),
        "volume_id": require_optional_str(data.get("volume_id"), "volume_id"),
        "rootfs_squashfs": require_optional_str(data.get("rootfs_squashfs"), "rootfs_squashfs"),
        "squashfs_compression": require_optional_str(
            data.get("squashfs_compression"), "squashfs_compression"
        ),
        "squashfs_block_size": require_optional_int(
            data.get("squashfs_block_size"), "squashfs_block_size"
        ),
        "_paths": project_paths(project_dir),
    }


def load_project(project_dir: Path) -> StudioProject:
    try:
        return read_project(project_dir)
    except ValueError as exc:
        die(str(exc))


def save_project(project: StudioProject) -> None:
    data: StoredStudioProject = {
        "project_version": project["project_version"],
        "name": project["name"],
        "base_iso": project["base_iso"],
        "volume_id": project["volume_id"],
        "rootfs_squashfs": project["rootfs_squashfs"],
        "squashfs_compression": project["squashfs_compression"],
        "squashfs_block_size": project["squashfs_block_size"],
    }
    config_path = project["_paths"]["config"]
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def path_state(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_dir():
        try:
            return "ready" if any(path.iterdir()) else "empty"
        except OSError:
            return "ready"
    return "ready"


def human_exists(path: Path) -> str:
    return "yes" if path.exists() else "no"


def format_project_status(project: StudioProject) -> str:
    paths = project["_paths"]
    return "\n".join(
        [
            f"Project          : {paths['project']}",
            f"Base ISO         : {paths['base_iso']} ({human_exists(paths['base_iso'])})",
            f"ISO tree         : {paths['iso']} ({path_state(paths['iso'])})",
            f"Rootfs           : {paths['rootfs']} ({path_state(paths['rootfs'])})",
            f"Output dir       : {paths['out']} ({human_exists(paths['out'])})",
            f"Volume ID        : {project['volume_id'] or '(unknown)'}",
            f"Rootfs squashfs  : {project['rootfs_squashfs'] or '(not detected yet)'}",
            f"Squashfs settings: "
            f"{project['squashfs_compression'] or '?'} / "
            f"{project['squashfs_block_size'] or '?'}",
        ]
    )


def related_manifest_remove_paths(iso_dir: Path, rootfs_relpath: str) -> list[Path]:
    rel = Path(rootfs_relpath)
    stem = rel.stem
    remove_paths: list[Path] = []

    stem_candidate = iso_dir / rel.parent / f"{stem}.manifest-remove"
    common_candidate = iso_dir / "casper" / "filesystem.manifest-remove"

    if stem_candidate.exists():
        remove_paths.append(stem_candidate)
    if common_candidate.exists() and common_candidate not in remove_paths:
        remove_paths.append(common_candidate)
    return remove_paths


def detect_boot_config_paths(iso_dir: Path) -> list[Path]:
    patterns = [
        "boot/grub/*.cfg",
        "isolinux/*.cfg",
        "EFI/BOOT/*.cfg",
    ]
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(sorted(iso_dir.glob(pattern)))

    priority = [
        iso_dir / "boot/grub/grub.cfg",
        iso_dir / "boot/grub/loopback.cfg",
        iso_dir / "isolinux/txt.cfg",
        iso_dir / "isolinux/isolinux.cfg",
    ]
    ordered: list[Path] = []
    seen: set[Path] = set()
    for path in priority + matches:
        if not path.exists() or path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return ordered


def read_text_preview(path: Path, *, line_limit: int = 80) -> str:
    if not path.exists():
        return f"{path} is missing."
    lines = path.read_text(errors="ignore").splitlines()
    if not lines:
        return f"{path}\n\n(file is empty)"
    clipped = lines[:line_limit]
    body = "\n".join(clipped)
    if len(lines) > line_limit:
        body += f"\n\n... trimmed {len(lines) - line_limit} more lines ..."
    return f"{path}\n\n{body}"


def project_details_report(project: StudioProject) -> str:
    paths = project["_paths"]
    iso_dir = paths["iso"]
    rootfs_relpath = project["rootfs_squashfs"]
    manifest_paths: list[Path] = []
    size_paths: list[Path] = []
    remove_paths: list[Path] = []
    if iso_dir.exists() and rootfs_relpath:
        manifest_paths, size_paths = related_metadata_paths(iso_dir, rootfs_relpath)
        remove_paths = related_manifest_remove_paths(iso_dir, rootfs_relpath)

    boot_paths = detect_boot_config_paths(iso_dir) if iso_dir.exists() else []
    lines = [
        f"Volume label     : {project['volume_id'] or '(not set)'}",
        f"Base ISO         : {paths['base_iso']}",
        f"ISO tree         : {iso_dir} ({path_state(iso_dir)})",
        f"Rootfs           : {paths['rootfs']} ({path_state(paths['rootfs'])})",
        f"Rootfs squashfs  : {rootfs_relpath or '(unknown until unpack)'}",
        f"Compression      : {project['squashfs_compression'] or DEFAULT_COMPRESSION}",
        f"Block size       : {project['squashfs_block_size'] or DEFAULT_BLOCK_SIZE}",
        "",
        "Boot config files:",
    ]
    if boot_paths:
        lines.extend(f"  - {path}" for path in boot_paths)
    else:
        lines.append("  - unpack the ISO to detect GRUB/ISOLINUX config files")

    lines.extend(["", "Metadata files:"])
    if manifest_paths:
        lines.extend(f"  - {path}" for path in manifest_paths)
    else:
        lines.append("  - no filesystem.manifest found yet")
    if remove_paths:
        lines.extend(f"  - {path}" for path in remove_paths)
    if size_paths:
        lines.extend(f"  - {path}" for path in size_paths)
    else:
        lines.append("  - no filesystem.size found yet")

    return "\n".join(lines)


def default_output_path(project: StudioProject) -> Path:
    return project["_paths"]["out"] / f"{project['name']}.iso"


def try_hardlink_or_copy(src: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dest)
        return "copy"


def inspect_volume_id(iso_path: Path) -> str | None:
    result = run(["xorriso", "-indev", str(iso_path), "-pvd_info"], capture=True)
    match = re.search(r"^Volume Id\s+:\s*(.+)$", result.stdout, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def boot_replay_args(iso_path: Path) -> list[str]:
    result = run(
        ["xorriso", "-indev", str(iso_path), "-report_el_torito", "as_mkisofs"],
        capture=True,
    )
    text = " ".join(line.strip() for line in result.stdout.splitlines() if line.strip())
    if not text:
        die("could not read boot metadata from base ISO")
    return shlex.split(text)


def maybe_clear(path: Path, force: bool) -> None:
    if not path.exists():
        return
    if not force:
        die(f"{path} already exists, rerun with --force to replace it")
    shutil.rmtree(path)


def detect_rootfs_squashfs(iso_dir: Path) -> str:
    install_sources = iso_dir / "casper" / "install-sources.yaml"
    if install_sources.exists():
        lines = install_sources.read_text().splitlines()
        candidates: list[str] = []
        for line in lines:
            match = re.match(r"^\s*path:\s*(\S+\.squashfs)\s*$", line)
            if not match:
                continue
            rel = match.group(1)
            if ".installer" in rel or ".generic" in rel:
                continue
            candidates.append(rel)
        if candidates:
            return f"casper/{candidates[0]}"

    filesystem = iso_dir / "casper" / "filesystem.squashfs"
    if filesystem.exists():
        return "casper/filesystem.squashfs"

    squashfs_files = sorted((iso_dir / "casper").glob("*.squashfs"))
    filtered = [
        path
        for path in squashfs_files
        if ".installer" not in path.name and ".generic" not in path.name
    ]
    if not filtered:
        die("could not find an editable rootfs squashfs inside iso/casper")
    largest = max(filtered, key=lambda path: path.stat().st_size)
    return str(largest.relative_to(iso_dir))


def parse_squashfs_details(squashfs_path: Path) -> SquashfsDetails:
    result = run(["unsquashfs", "-s", str(squashfs_path)], capture=True)
    compression = DEFAULT_COMPRESSION
    block_size = DEFAULT_BLOCK_SIZE
    for line in result.stdout.splitlines():
        if line.startswith("Compression "):
            compression = line.split()[-1].strip()
        elif line.startswith("Block size "):
            value = line.split()[-1].strip()
            block_size = int(value)
    return {"compression": compression, "block_size": block_size}


def parse_installed_packages(status_path: Path) -> list[tuple[str, str]]:
    if not status_path.exists():
        warn(f"{status_path} missing, skipping manifest update")
        return []

    packages: list[tuple[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in status_path.read_text(errors="ignore").splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            if current.get("Status") == "install ok installed":
                name = current.get("Package")
                version = current.get("Version")
                if name and version:
                    packages.append((name, version))
            current = {}
            continue
        if line[0].isspace():
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key] = value.strip()
    if current.get("Status") == "install ok installed":
        name = current.get("Package")
        version = current.get("Version")
        if name and version:
            packages.append((name, version))
    return sorted(packages)


def related_metadata_paths(iso_dir: Path, rootfs_relpath: str) -> tuple[list[Path], list[Path]]:
    rel = Path(rootfs_relpath)
    stem = rel.stem
    manifest_paths: list[Path] = []
    size_paths: list[Path] = []

    manifest_candidate = iso_dir / rel.parent / f"{stem}.manifest"
    size_candidate = iso_dir / rel.parent / f"{stem}.size"
    if manifest_candidate.exists():
        manifest_paths.append(manifest_candidate)
    if size_candidate.exists():
        size_paths.append(size_candidate)

    common_manifest = iso_dir / "casper" / "filesystem.manifest"
    common_size = iso_dir / "casper" / "filesystem.size"
    if common_manifest.exists() and common_manifest not in manifest_paths:
        manifest_paths.append(common_manifest)
    if common_size.exists() and common_size not in size_paths:
        size_paths.append(common_size)
    return manifest_paths, size_paths


def write_manifests(rootfs_dir: Path, manifest_paths: Iterable[Path]) -> None:
    packages = parse_installed_packages(rootfs_dir / "var/lib/dpkg/status")
    if not packages:
        return
    payload = "".join(f"{name}\t{version}\n" for name, version in packages)
    for path in manifest_paths:
        path.write_text(payload)


def rootfs_size_bytes(rootfs_dir: Path) -> int:
    result = run(["du", "-sx", "--block-size=1", str(rootfs_dir)], capture=True)
    return int(result.stdout.split()[0])


def write_sizes(rootfs_dir: Path, size_paths: Iterable[Path]) -> None:
    size = rootfs_size_bytes(rootfs_dir)
    payload = f"{size}\n"
    for path in size_paths:
        path.write_text(payload)


def update_md5sum(iso_dir: Path) -> None:
    entries: list[str] = []
    for path in sorted(iso_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "md5sum.txt":
            continue
        rel = f"./{path.relative_to(iso_dir).as_posix()}"
        digest = md5()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append(f"{digest.hexdigest()}  {rel}\n")
    (iso_dir / "md5sum.txt").write_text("".join(entries))


def write_shell_rc(project: StudioProject) -> Path:
    rootfs_dir = project["_paths"]["rootfs"]
    rc_path = rootfs_dir / "root/.sirco-shellrc"
    banner = textwrap.dedent(
        f"""
        clear
        cat <<'EOF'
        Sirco Studio shell
        Project : {project['name']}
        Rootfs  : {rootfs_dir}

        Helper commands:
          studio-help   show this message
          studio-root   cd /
          studio-exit   leave the ISO shell
        EOF
        studio-help() {{
          cat <<'EOF'
        studio-help   show this message
        studio-root   cd /
        studio-exit   exit the shell
        EOF
        }}
        studio-root() {{
          cd /
        }}
        studio-exit() {{
          exit
        }}
        export PS1='[sirco-iso \\u@\\h:\\w]# '
        """
    ).lstrip()
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    rc_path.write_text(banner)
    return rc_path


def bind_mounts(rootfs_dir: Path) -> list[Path]:
    mount_pairs = [
        (Path("/dev"), rootfs_dir / "dev"),
        (Path("/dev/pts"), rootfs_dir / "dev/pts"),
        (Path("/proc"), rootfs_dir / "proc"),
        (Path("/sys"), rootfs_dir / "sys"),
        (Path("/run"), rootfs_dir / "run"),
    ]
    mounted: list[Path] = []
    for source, target in mount_pairs:
        target.mkdir(parents=True, exist_ok=True)
        run(["mount", "--bind", str(source), str(target)])
        mounted.append(target)
    return mounted


def unmount_all(paths: Iterable[Path]) -> None:
    for path in reversed(list(paths)):
        run(["umount", "-lf", str(path)], check=False)


def ensure_rootfs_ready(project: StudioProject) -> None:
    rootfs_dir = project["_paths"]["rootfs"]
    if not rootfs_dir.exists():
        die("rootfs is not unpacked yet; run unpack first")
    if not any(rootfs_dir.iterdir()):
        die("rootfs exists but is empty; run unpack again")


def terminal_launcher(command: list[str]) -> list[str] | None:
    payload = (
        f"{shlex.join(command)}; status=$?; printf '\\n'; "
        "read -r -p 'Press Enter to close this shell...' _; exit $status"
    )
    candidates = [
        ["x-terminal-emulator", "-e", "bash", "-lc", payload],
        ["gnome-terminal", "--", "bash", "-lc", payload],
        ["konsole", "-e", "bash", "-lc", payload],
        ["xterm", "-e", "bash", "-lc", payload],
    ]
    xfce_payload = f"bash -lc {shlex.quote(payload)}"
    candidates.append(["xfce4-terminal", "--command", xfce_payload])

    for candidate in candidates:
        if command_exists(candidate[0]):
            return candidate
    return None


def launch_shell_terminal(project_dir: Path) -> None:
    command = [sys.executable, str(SCRIPT_PATH), "shell", str(project_dir)]
    launcher = terminal_launcher(command)
    if launcher is None:
        raise RuntimeError(
            "No supported terminal emulator was found. Try running "
            f"`python3 {SCRIPT_PATH.name} shell {project_dir}` manually."
        )
    subprocess.Popen(launcher, start_new_session=True)


def cmd_init(args: argparse.Namespace) -> None:
    require_commands("xorriso")
    project_dir = args.project.resolve()
    iso_path = args.iso.resolve()
    if not iso_path.exists():
        die(f"base ISO not found: {iso_path}")
    if project_dir.exists() and any(project_dir.iterdir()) and not args.force:
        die(f"{project_dir} is not empty; rerun with --force to reuse it")
    project_dir.mkdir(parents=True, exist_ok=True)

    paths = project_paths(project_dir)
    for path in (paths["iso"], paths["rootfs"], paths["out"], paths["tmp"]):
        path.mkdir(parents=True, exist_ok=True)

    if paths["base_iso"].exists():
        if args.force:
            paths["base_iso"].unlink()
        else:
            die(f"{paths['base_iso']} already exists")
    mode = try_hardlink_or_copy(iso_path, paths["base_iso"])
    volume_id = inspect_volume_id(paths["base_iso"])

    project: StudioProject = {
        "project_version": PROJECT_VERSION,
        "name": project_dir.name,
        "base_iso": "base.iso",
        "volume_id": volume_id,
        "rootfs_squashfs": None,
        "squashfs_compression": None,
        "squashfs_block_size": None,
        "_paths": paths,
    }
    save_project(project)
    info(f"Initialized {project_dir} using a {mode} to {iso_path.name}")
    info(f"Next: python3 {Path(__file__).name} unpack {project_dir}")


def cmd_unpack(args: argparse.Namespace) -> None:
    require_commands("xorriso", "unsquashfs")
    project = load_project(args.project.resolve())
    paths = project["_paths"]
    maybe_clear(paths["iso"], args.force)
    maybe_clear(paths["rootfs"], args.force)
    paths["iso"].mkdir(parents=True, exist_ok=True)
    paths["rootfs"].mkdir(parents=True, exist_ok=True)

    info("Extracting ISO tree...")
    run(
        [
            "xorriso",
            "-osirrox",
            "on",
            "-indev",
            str(paths["base_iso"]),
            "-extract",
            "/",
            str(paths["iso"]),
        ]
    )

    rootfs_relpath = detect_rootfs_squashfs(paths["iso"])
    squashfs_path = paths["iso"] / rootfs_relpath
    details = parse_squashfs_details(squashfs_path)

    info(f"Unpacking editable rootfs from {rootfs_relpath}...")
    run(["unsquashfs", "-f", "-d", str(paths["rootfs"]), str(squashfs_path)])

    project["rootfs_squashfs"] = rootfs_relpath
    project["squashfs_compression"] = details["compression"]
    project["squashfs_block_size"] = details["block_size"]
    save_project(project)
    info("Project unpacked.")
    info(f"Open the editing shell with: python3 {Path(__file__).name} shell {paths['project']}")


def cmd_status(args: argparse.Namespace) -> None:
    project = load_project(args.project.resolve())
    print(format_project_status(project))


def cmd_shell(args: argparse.Namespace) -> None:
    project_dir = args.project.resolve()
    if os.geteuid() != 0:
        if not command_exists("sudo"):
            die("shell needs root privileges for bind mounts and chroot, but sudo is unavailable")
        os.execvp(
            "sudo",
            [
                "sudo",
                sys.executable,
                str(SCRIPT_PATH),
                "_shell-root",
                str(project_dir),
            ],
        )
    cmd_shell_root(args)


def cmd_shell_root(args: argparse.Namespace) -> None:
    project = load_project(args.project.resolve())
    ensure_rootfs_ready(project)
    rootfs_dir = project["_paths"]["rootfs"]
    rc_path = write_shell_rc(project)
    mounted: list[Path] = []
    try:
        mounted = bind_mounts(rootfs_dir)
        chroot_cmd = [
            "chroot",
            str(rootfs_dir),
            "/usr/bin/env",
            "-i",
            "HOME=/root",
            f"TERM={os.environ.get('TERM', 'xterm-256color')}",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "/bin/bash",
            "--noprofile",
            "--rcfile",
            "/root/.sirco-shellrc",
            "-i",
        ]
        run(chroot_cmd, check=False)
    finally:
        if rc_path.exists():
            rc_path.unlink()
        unmount_all(mounted)


def cmd_build(args: argparse.Namespace) -> None:
    require_commands("mksquashfs", "xorriso")
    project = load_project(args.project.resolve())
    ensure_rootfs_ready(project)
    paths = project["_paths"]
    iso_dir = paths["iso"]
    rootfs_dir = paths["rootfs"]
    if not iso_dir.exists():
        die("iso tree is not unpacked yet; run unpack first")

    rootfs_relpath = project["rootfs_squashfs"] or detect_rootfs_squashfs(iso_dir)
    squashfs_path = iso_dir / rootfs_relpath
    if not squashfs_path.parent.exists():
        squashfs_path.parent.mkdir(parents=True, exist_ok=True)

    compression = project["squashfs_compression"] or DEFAULT_COMPRESSION
    block_size = int(project["squashfs_block_size"] or DEFAULT_BLOCK_SIZE)

    info(f"Repacking {rootfs_relpath}...")
    run(
        [
            "mksquashfs",
            str(rootfs_dir),
            str(squashfs_path),
            "-noappend",
            "-comp",
            str(compression),
            "-b",
            str(block_size),
        ]
    )

    manifest_paths, size_paths = related_metadata_paths(iso_dir, rootfs_relpath)
    if manifest_paths:
        info("Updating manifest files...")
        write_manifests(rootfs_dir, manifest_paths)
    if size_paths:
        info("Updating size files...")
        write_sizes(rootfs_dir, size_paths)

    info("Refreshing md5sum.txt...")
    update_md5sum(iso_dir)

    output_path = args.output.resolve() if args.output else default_output_path(project)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    boot_args = boot_replay_args(paths["base_iso"])
    volume_id = project["volume_id"]
    info(f"Building ISO -> {output_path}")
    run(
        [
            "xorriso",
            "-as",
            "mkisofs",
            "-r",
            "-J",
            "-joliet-long",
            "-l",
            "-iso-level",
            "3",
            *(["-V", str(volume_id)] if volume_id else []),
            *boot_args,
            "-o",
            str(output_path),
            str(iso_dir),
        ]
    )
    info("Build complete.")


def cmd_clean(args: argparse.Namespace) -> None:
    project = load_project(args.project.resolve())
    paths = project["_paths"]
    removed = False
    for path in (paths["iso"], paths["rootfs"]):
        if path.exists():
            shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=True)
            removed = True
    if removed:
        info("Removed unpacked ISO tree and rootfs.")
    else:
        info("Nothing to clean.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sirco_studio",
        description=(
            "One-folder ISO studio for Ubuntu-style live/server images. "
            "Run without arguments to open the GUI."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a new one-folder ISO project")
    init_parser.add_argument("project", type=Path, help="project directory to create")
    init_parser.add_argument("--iso", required=True, type=Path, help="source ISO to import")
    init_parser.add_argument("--force", action="store_true", help="reuse an existing project directory")
    init_parser.set_defaults(func=cmd_init)

    unpack_parser = subparsers.add_parser("unpack", help="extract the ISO tree and editable rootfs")
    unpack_parser.add_argument("project", type=Path, help="project directory")
    unpack_parser.add_argument("--force", action="store_true", help="replace existing iso/rootfs folders")
    unpack_parser.set_defaults(func=cmd_unpack)

    status_parser = subparsers.add_parser("status", help="show project status")
    status_parser.add_argument("project", type=Path, help="project directory")
    status_parser.set_defaults(func=cmd_status)

    shell_parser = subparsers.add_parser("shell", help="open a nicer chroot shell for the rootfs")
    shell_parser.add_argument("project", type=Path, help="project directory")
    shell_parser.set_defaults(func=cmd_shell)

    build_parser_cmd = subparsers.add_parser("build", help="repack the rootfs and build a new ISO")
    build_parser_cmd.add_argument("project", type=Path, help="project directory")
    build_parser_cmd.add_argument("--output", type=Path, help="output ISO path")
    build_parser_cmd.set_defaults(func=cmd_build)

    clean_parser = subparsers.add_parser("clean", help="remove unpacked iso/rootfs contents")
    clean_parser.add_argument("project", type=Path, help="project directory")
    clean_parser.set_defaults(func=cmd_clean)
    return parser


def launch_gui(initial_project: Path | None = None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, font as tkfont, messagebox, scrolledtext
    except ImportError as exc:
        die(f"the GUI needs tkinter/python3-tk: {exc}")

    BG = "#f3eee5"
    PANEL = "#fffaf2"
    PANEL_ALT = "#f0e8dc"
    SIDEBAR = "#19303a"
    SIDEBAR_MUTED = "#294652"
    SIDEBAR_TEXT = "#eef5f3"
    SIDEBAR_TEXT_MUTED = "#c7d8d5"
    TEXT = "#21323a"
    MUTED = "#58707c"
    ACCENT = "#d56a41"
    ACCENT_DARK = "#bf5630"
    SECONDARY = "#2f7f72"
    BORDER = "#d6cab8"
    GOOD = "#2f7f72"

    class SircoStudioApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title("Sirco Studio")
            self.root.geometry("1380x880")
            self.root.minsize(1180, 760)
            self.root.configure(bg=BG)

            self.page_frames: dict[WorkflowStepKey, tk.Frame] = {}
            self.sidebar_buttons: dict[WorkflowStepKey, tk.Button] = {}
            self.action_buttons: list[tk.Button] = []
            self.queue: queue.Queue[UiEvent] = queue.Queue()
            self.busy = False
            self.current_project: StudioProject | None = None
            self.current_project_dir: Path | None = None
            self.current_page = WORKFLOW_STEPS[0][0]
            self.log_widget: scrolledtext.ScrolledText | None = None
            self.boot_preview_widget: scrolledtext.ScrolledText | None = None
            self.metadata_preview_widget: scrolledtext.ScrolledText | None = None

            self.project_dir_var = tk.StringVar(value=str(initial_project or Path.cwd()))
            self.iso_var = tk.StringVar(value="")
            self.output_var = tk.StringVar(value="")
            self.volume_id_var = tk.StringVar(value="")
            self.compression_var = tk.StringVar(value=DEFAULT_COMPRESSION)
            self.hero_title_var = tk.StringVar(value=WORKFLOW_STEPS[0][1])
            self.hero_subtitle_var = tk.StringVar(value=WORKFLOW_STEPS[0][2])
            self.status_banner_var = tk.StringVar(
                value="Start by choosing a project folder, then import or open an ISO studio."
            )
            self.sidebar_status_var = tk.StringVar(value="Project: none\nCompression: gzip\nRootfs: waiting")
            self.project_summary_var = tk.StringVar(value="No project loaded yet.")
            self.build_summary_var = tk.StringVar(
                value="Build becomes available after a project has been unpacked."
            )
            self.details_summary_var = tk.StringVar(
                value="Load a project to inspect its boot config, manifests, and ISO metadata."
            )
            self.compression_hint_var = tk.StringVar(
                value=self.describe_compression(DEFAULT_COMPRESSION)
            )
            self.log_title_var = tk.StringVar(value="Workflow log")

            self.fonts = {
                "display": tkfont.Font(family="Georgia", size=28, weight="bold"),
                "title": tkfont.Font(family="Georgia", size=18, weight="bold"),
                "heading": tkfont.Font(family="Helvetica", size=16, weight="bold"),
                "subheading": tkfont.Font(family="Helvetica", size=11, weight="bold"),
                "body": tkfont.Font(family="Helvetica", size=11),
                "small": tkfont.Font(family="Helvetica", size=10),
                "mono": tkfont.Font(family="Courier", size=10),
            }

            self.build_layout()
            self.root.after(120, self.process_events)

            if initial_project and (initial_project / PROJECT_FILE).exists():
                self.open_project(initial_project)
            else:
                self.refresh_banner()

        def build_layout(self) -> None:
            shell = tk.Frame(self.root, bg=BG)
            shell.pack(fill="both", expand=True)

            sidebar = tk.Frame(shell, bg=SIDEBAR, width=280)
            sidebar.pack(side="left", fill="y")
            sidebar.pack_propagate(False)

            main = tk.Frame(shell, bg=BG)
            main.pack(side="left", fill="both", expand=True)

            self.build_sidebar(sidebar)
            self.build_main(main)

        def build_sidebar(self, parent: tk.Frame) -> None:
            brand = tk.Frame(parent, bg=SIDEBAR)
            brand.pack(fill="x", padx=22, pady=(24, 18))

            tk.Label(
                brand,
                text="Sirco Studio",
                bg=SIDEBAR,
                fg=SIDEBAR_TEXT,
                font=self.fonts["display"],
                anchor="w",
            ).pack(fill="x")
            tk.Label(
                brand,
                text="One-folder ISO editing with a workflow that feels closer to an app than a pile of shell commands.",
                bg=SIDEBAR,
                fg=SIDEBAR_TEXT_MUTED,
                font=self.fonts["body"],
                justify="left",
                wraplength=220,
                anchor="w",
            ).pack(fill="x", pady=(8, 0))
            tk.Label(
                brand,
                text="Running as root ✓",
                bg=SIDEBAR,
                fg=GOOD,
                font=self.fonts["small"],
                anchor="w",
            ).pack(fill="x", pady=(8, 0))

            steps_frame = tk.Frame(parent, bg=SIDEBAR)
            steps_frame.pack(fill="x", padx=16, pady=(10, 18))

            for index, (key, title, subtitle) in enumerate(WORKFLOW_STEPS, start=1):
                button = tk.Button(
                    steps_frame,
                    text=f"{index}. {title}\n{subtitle}",
                    command=lambda page=key: self.show_page(page),
                    bg=SIDEBAR,
                    fg=SIDEBAR_TEXT,
                    activebackground=SIDEBAR_MUTED,
                    activeforeground=SIDEBAR_TEXT,
                    highlightthickness=0,
                    relief="flat",
                    bd=0,
                    justify="left",
                    anchor="w",
                    padx=16,
                    pady=14,
                    font=self.fonts["body"],
                    cursor="hand2",
                    wraplength=210,
                )
                button.pack(fill="x", pady=4)
                self.sidebar_buttons[key] = button

            status_card = tk.Frame(parent, bg=SIDEBAR_MUTED, highlightthickness=1, highlightbackground="#365867")
            status_card.pack(side="bottom", fill="x", padx=18, pady=18)

            tk.Label(
                status_card,
                text="Studio Status",
                bg=SIDEBAR_MUTED,
                fg=SIDEBAR_TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=14, pady=(14, 4))
            tk.Label(
                status_card,
                textvariable=self.sidebar_status_var,
                bg=SIDEBAR_MUTED,
                fg=SIDEBAR_TEXT_MUTED,
                font=self.fonts["small"],
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=14, pady=(0, 14))

        def build_main(self, parent: tk.Frame) -> None:
            # Navigation bar
            nav = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            nav.pack(fill="x", padx=22, pady=(22, 0))
            nav_inner = tk.Frame(nav, bg=PANEL)
            nav_inner.pack(fill="x", padx=24, pady=12)
            
            self.nav_back_button = self.make_button(
                nav_inner,
                text="← Back",
                command=self.handle_nav_back,
            )
            self.nav_back_button.pack(side="left", padx=(0, 10))
            
            self.nav_forward_button = self.make_button(
                nav_inner,
                text="Next →",
                command=self.handle_nav_forward,
            )
            self.nav_forward_button.pack(side="left")

            hero = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            hero.pack(fill="x", padx=22, pady=(14, 14))

            tk.Label(
                hero,
                textvariable=self.hero_title_var,
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["title"],
                anchor="w",
            ).pack(fill="x", padx=24, pady=(20, 4))
            tk.Label(
                hero,
                textvariable=self.hero_subtitle_var,
                bg=PANEL,
                fg=MUTED,
                font=self.fonts["body"],
                anchor="w",
                justify="left",
                wraplength=900,
            ).pack(fill="x", padx=24, pady=(0, 10))
            tk.Label(
                hero,
                textvariable=self.status_banner_var,
                bg=PANEL,
                fg=GOOD,
                font=self.fonts["small"],
                anchor="w",
                justify="left",
                wraplength=900,
            ).pack(fill="x", padx=24, pady=(0, 20))

            content = tk.Frame(parent, bg=BG)
            content.pack(fill="both", expand=True, padx=22)
            self.content = content

            log_card = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            log_card.pack(fill="x", padx=22, pady=(14, 22))

            tk.Label(
                log_card,
                textvariable=self.log_title_var,
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=18, pady=(16, 8))

            self.log_widget = scrolledtext.ScrolledText(
                log_card,
                height=12,
                bg="#fbf7f0",
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                wrap="word",
                font=self.fonts["mono"],
                padx=14,
                pady=12,
                yscrollcommand=lambda f, l: None,  # Enable scrollbar
            )
            self.log_widget.pack(fill="both", expand=True, padx=18, pady=(0, 16))
            self.log_widget.insert("end", "Sirco Studio ready.\n")
            self.log_widget.configure(state="disabled")

            self.build_overview_page(content)
            self.build_project_page(content)
            self.build_unpack_page(content)
            self.build_shell_page(content)
            self.build_details_page(content)
            self.build_compression_page(content)
            self.build_build_page(content)
            self.show_page(self.current_page)

        def make_page(self, key: WorkflowStepKey) -> tk.Frame:
            frame = tk.Frame(self.content, bg=BG)
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.page_frames[key] = frame
            return frame

        def make_card(self, parent: tk.Widget, *, alt: bool = False) -> tk.Frame:
            return tk.Frame(
                parent,
                bg=PANEL_ALT if alt else PANEL,
                highlightthickness=1,
                highlightbackground=BORDER,
                bd=0,
            )

        def make_button(
            self,
            parent: tk.Widget,
            *,
            text: str,
            command: Callable[[], None],
            primary: bool = False,
            danger: bool = False,
            width: int | None = None,
        ) -> tk.Button:
            if danger:
                bg = "#8f3c34"
                active = "#782f29"
            elif primary:
                bg = ACCENT
                active = ACCENT_DARK
            else:
                bg = SECONDARY
                active = "#256559"
            button_kwargs: dict[str, Any] = {
                "text": text,
                "command": command,
                "bg": bg,
                "fg": "white",
                "activebackground": active,
                "activeforeground": "white",
                "relief": "flat",
                "bd": 0,
                "padx": 14,
                "pady": 10,
                "cursor": "hand2",
                "font": self.fonts["body"],
            }
            if width is not None:
                button_kwargs["width"] = width
            button = tk.Button(parent, **button_kwargs)
            self.action_buttons.append(button)
            return button

        def make_input(self, parent: tk.Widget, variable: tk.StringVar) -> tk.Entry:
            return tk.Entry(
                parent,
                textvariable=variable,
                bg="white",
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=SECONDARY,
                font=self.fonts["body"],
            )

        def build_overview_page(self, parent: tk.Frame) -> None:
            page = self.make_page("overview")

            top = tk.Frame(page, bg=BG)
            top.pack(fill="both", expand=True)

            left = tk.Frame(top, bg=BG)
            left.pack(side="left", fill="both", expand=True)

            right = tk.Frame(top, bg=BG, width=350)
            right.pack(side="left", fill="y", padx=(16, 0))
            right.pack_propagate(False)

            intro = self.make_card(left)
            intro.pack(fill="x", pady=(0, 16))
            tk.Label(
                intro,
                text="A cleaner remastering flow",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["heading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 6))
            tk.Label(
                intro,
                text=(
                    "Use Sirco Studio like a guided rebuild bench: import an ISO, unpack it, "
                    "jump into the rootfs shell, choose your compression, and build a fresh image "
                    "without bouncing between random folders."
                ),
                bg=PANEL,
                fg=MUTED,
                font=self.fonts["body"],
                justify="left",
                wraplength=680,
                anchor="w",
            ).pack(fill="x", padx=20, pady=(0, 16))

            actions = self.make_card(left, alt=True)
            actions.pack(fill="x")
            tk.Label(
                actions,
                text="Quick actions",
                bg=PANEL_ALT,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 12))

            buttons = tk.Frame(actions, bg=PANEL_ALT)
            buttons.pack(fill="x", padx=20, pady=(0, 18))

            self.make_button(
                buttons,
                text="Open Existing Project",
                command=self.handle_open_existing_prompt,
                primary=True,
            ).pack(side="left", padx=(0, 10))
            self.make_button(
                buttons,
                text="Import New ISO",
                command=lambda: self.show_page("project"),
            ).pack(side="left", padx=(0, 10))
            self.make_button(
                buttons,
                text="ISO Details",
                command=lambda: self.show_page("details"),
            ).pack(side="left", padx=(0, 10))
            self.make_button(
                buttons,
                text="Build Screen",
                command=lambda: self.show_page("build"),
            ).pack(side="left")

            facts = self.make_card(right)
            facts.pack(fill="x")
            tk.Label(
                facts,
                text="Current project",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=18, pady=(18, 8))
            tk.Label(
                facts,
                textvariable=self.project_summary_var,
                bg=PANEL,
                fg=MUTED,
                font=self.fonts["small"],
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=18, pady=(0, 18))

            pipeline = self.make_card(right, alt=True)
            pipeline.pack(fill="x", pady=(16, 0))
            tk.Label(
                pipeline,
                text="Workflow",
                bg=PANEL_ALT,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=18, pady=(18, 8))
            tk.Label(
                pipeline,
                text="ISO -> unpack -> chroot shell -> choose compression -> build -> new ISO",
                bg=PANEL_ALT,
                fg=MUTED,
                font=self.fonts["body"],
                justify="left",
                wraplength=280,
                anchor="w",
            ).pack(fill="x", padx=18, pady=(0, 18))

        def build_project_page(self, parent: tk.Frame) -> None:
            page = self.make_page("project")

            card = self.make_card(page)
            card.pack(fill="x")

            tk.Label(
                card,
                text="Project folder",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 6))
            folder_row = tk.Frame(card, bg=PANEL)
            folder_row.pack(fill="x", padx=20)
            self.make_input(folder_row, self.project_dir_var).pack(side="left", fill="x", expand=True)
            self.make_button(folder_row, text="Browse", command=self.choose_project_dir).pack(side="left", padx=(10, 0))

            tk.Label(
                card,
                text="Base ISO",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 6))
            iso_row = tk.Frame(card, bg=PANEL)
            iso_row.pack(fill="x", padx=20)
            self.make_input(iso_row, self.iso_var).pack(side="left", fill="x", expand=True)
            self.make_button(iso_row, text="Browse ISO", command=self.choose_iso_file).pack(side="left", padx=(10, 0))

            actions = tk.Frame(card, bg=PANEL)
            actions.pack(fill="x", padx=20, pady=(20, 20))
            self.make_button(
                actions,
                text="Initialize Project",
                command=self.handle_init_project,
                primary=True,
            ).pack(side="left", padx=(0, 10))
            self.make_button(
                actions,
                text="Open Existing Project",
                command=self.handle_open_project_from_field,
            ).pack(side="left", padx=(0, 10))
            self.make_button(
                actions,
                text="Refresh Status",
                command=self.refresh_loaded_project,
            ).pack(side="left")

        def build_unpack_page(self, parent: tk.Frame) -> None:
            page = self.make_page("unpack")

            card = self.make_card(page)
            card.pack(fill="x")
            tk.Label(
                card,
                text="Unpack the editable workspace",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["heading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 6))
            tk.Label(
                card,
                text=(
                    "This extracts the ISO tree into `iso/`, finds the editable squashfs, and expands "
                    "it into `rootfs/` so the shell step has a full filesystem to work with."
                ),
                bg=PANEL,
                fg=MUTED,
                font=self.fonts["body"],
                justify="left",
                wraplength=900,
                anchor="w",
            ).pack(fill="x", padx=20, pady=(0, 14))

            self.make_button(
                card,
                text="Unpack ISO",
                command=self.handle_unpack_project,
                primary=True,
            ).pack(anchor="w", padx=20, pady=(0, 18))

            summary = self.make_card(page, alt=True)
            summary.pack(fill="x", pady=(16, 0))
            tk.Label(
                summary,
                text="Current status",
                bg=PANEL_ALT,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 8))
            tk.Label(
                summary,
                textvariable=self.project_summary_var,
                bg=PANEL_ALT,
                fg=MUTED,
                font=self.fonts["small"],
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=20, pady=(0, 18))

        def build_shell_page(self, parent: tk.Frame) -> None:
            page = self.make_page("shell")

            card = self.make_card(page)
            card.pack(fill="x")
            tk.Label(
                card,
                text="Open the editing shell in a real terminal",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["heading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 6))
            tk.Label(
                card,
                text=(
                    "The GUI launches your Sirco Studio chroot shell in an external terminal emulator. "
                    "That keeps the editing session interactive, preserves sudo prompts, and feels much "
                    "closer to Cubic's terminal tab."
                ),
                bg=PANEL,
                fg=MUTED,
                font=self.fonts["body"],
                justify="left",
                wraplength=900,
                anchor="w",
            ).pack(fill="x", padx=20, pady=(0, 14))
            tk.Label(
                card,
                text="Tip: once it opens, use `apt`, edit configs, or add scripts inside the chroot exactly like you would in Cubic.",
                bg=PANEL,
                fg=GOOD,
                font=self.fonts["small"],
                justify="left",
                wraplength=900,
                anchor="w",
            ).pack(fill="x", padx=20, pady=(0, 14))
            self.make_button(
                card,
                text="Open Editing Shell",
                command=self.handle_open_shell,
                primary=True,
            ).pack(anchor="w", padx=20, pady=(0, 18))

        def build_compression_page(self, parent: tk.Frame) -> None:
            page = self.make_page("compression")

            intro = self.make_card(page)
            intro.pack(fill="x")
            tk.Label(
                intro,
                text="Choose your squashfs compression",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["heading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 6))
            tk.Label(
                intro,
                text=(
                    "This choice controls how the rebuilt root filesystem is compressed at the end of the workflow. "
                    "Pick a mode based on whether you care more about build speed, download size, or a balance of both."
                ),
                bg=PANEL,
                fg=MUTED,
                font=self.fonts["body"],
                justify="left",
                wraplength=900,
                anchor="w",
            ).pack(fill="x", padx=20, pady=(0, 14))

            grid = tk.Frame(page, bg=BG)
            grid.pack(fill="x", pady=(16, 0))

            for key in ("xz", "gzip", "lz4"):
                card = self.make_card(grid, alt=(key == "gzip"))
                card.pack(fill="x", pady=8)

                tk.Label(
                    card,
                    text=COMPRESSION_CHOICES[key]["title"],
                    bg=PANEL_ALT if key == "gzip" else PANEL,
                    fg=TEXT,
                    font=self.fonts["subheading"],
                    anchor="w",
                ).pack(fill="x", padx=20, pady=(18, 4))
                tk.Label(
                    card,
                    text=COMPRESSION_CHOICES[key]["summary"],
                    bg=PANEL_ALT if key == "gzip" else PANEL,
                    fg=GOOD if key == "gzip" else MUTED,
                    font=self.fonts["body"],
                    anchor="w",
                    justify="left",
                ).pack(fill="x", padx=20)
                tk.Label(
                    card,
                    text=COMPRESSION_CHOICES[key]["details"],
                    bg=PANEL_ALT if key == "gzip" else PANEL,
                    fg=MUTED,
                    font=self.fonts["small"],
                    wraplength=900,
                    justify="left",
                    anchor="w",
                ).pack(fill="x", padx=20, pady=(6, 10))

                tk.Radiobutton(
                    card,
                    text=f"Use {COMPRESSION_CHOICES[key]['title']}",
                    variable=self.compression_var,
                    value=key,
                    command=self.handle_compression_change,
                    bg=PANEL_ALT if key == "gzip" else PANEL,
                    fg=TEXT,
                    activebackground=PANEL_ALT if key == "gzip" else PANEL,
                    activeforeground=TEXT,
                    selectcolor=BG,
                    font=self.fonts["body"],
                    anchor="w",
                ).pack(fill="x", padx=18, pady=(0, 16))

            hint = self.make_card(page)
            hint.pack(fill="x", pady=(16, 0))
            tk.Label(
                hint,
                textvariable=self.compression_hint_var,
                bg=PANEL,
                fg=GOOD,
                font=self.fonts["body"],
                justify="left",
                anchor="w",
                wraplength=900,
            ).pack(fill="x", padx=20, pady=18)

        def build_build_page(self, parent: tk.Frame) -> None:
            page = self.make_page("build")

            card = self.make_card(page)
            card.pack(fill="x")

            tk.Label(
                card,
                text="Output ISO",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 6))
            row = tk.Frame(card, bg=PANEL)
            row.pack(fill="x", padx=20)
            self.make_input(row, self.output_var).pack(side="left", fill="x", expand=True)
            self.make_button(row, text="Browse Output", command=self.choose_output_file).pack(side="left", padx=(10, 0))

            tk.Label(
                card,
                textvariable=self.build_summary_var,
                bg=PANEL,
                fg=MUTED,
                font=self.fonts["body"],
                justify="left",
                anchor="w",
                wraplength=900,
            ).pack(fill="x", padx=20, pady=(18, 10))

            actions = tk.Frame(card, bg=PANEL)
            actions.pack(fill="x", padx=20, pady=(0, 20))
            self.make_button(
                actions,
                text="Build ISO",
                command=self.handle_build_project,
                primary=True,
            ).pack(side="left", padx=(0, 10))
            self.make_button(
                actions,
                text="Clean Unpacked Data",
                command=self.handle_clean_project,
                danger=True,
            ).pack(side="left")

        def build_details_page(self, parent: tk.Frame) -> None:
            page = self.make_page("details")

            settings = self.make_card(page)
            settings.pack(fill="x")

            tk.Label(
                settings,
                text="ISO metadata and boot files",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["heading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 6))
            tk.Label(
                settings,
                text=(
                    "Use this page for real Cubic-style details: volume label, boot config discovery, "
                    "and the manifest files that get rebuilt with the ISO."
                ),
                bg=PANEL,
                fg=MUTED,
                font=self.fonts["body"],
                justify="left",
                anchor="w",
                wraplength=900,
            ).pack(fill="x", padx=20, pady=(0, 14))

            tk.Label(
                settings,
                text="Volume label",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(0, 6))
            label_row = tk.Frame(settings, bg=PANEL)
            label_row.pack(fill="x", padx=20)
            self.make_input(label_row, self.volume_id_var).pack(side="left", fill="x", expand=True)
            self.make_button(
                label_row,
                text="Save Label",
                command=self.handle_save_metadata,
                primary=True,
            ).pack(side="left", padx=(10, 0))
            self.make_button(
                label_row,
                text="Refresh Details",
                command=self.refresh_details_view,
            ).pack(side="left", padx=(10, 0))

            tk.Label(
                settings,
                text="The saved volume label is applied during the final `xorriso` build step.",
                bg=PANEL,
                fg=GOOD,
                font=self.fonts["small"],
                justify="left",
                anchor="w",
                wraplength=900,
            ).pack(fill="x", padx=20, pady=(10, 18))

            summary = self.make_card(page, alt=True)
            summary.pack(fill="x", pady=(16, 0))
            tk.Label(
                summary,
                textvariable=self.details_summary_var,
                bg=PANEL_ALT,
                fg=MUTED,
                font=self.fonts["small"],
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=20, pady=18)

            previews = tk.Frame(page, bg=BG)
            previews.pack(fill="both", expand=True, pady=(16, 0))

            left = self.make_card(previews)
            left.pack(side="left", fill="both", expand=True)

            right = self.make_card(previews, alt=True)
            right.pack(side="left", fill="both", expand=True, padx=(16, 0))

            tk.Label(
                left,
                text="Primary boot config preview",
                bg=PANEL,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 10))

            self.boot_preview_widget = scrolledtext.ScrolledText(
                left,
                bg="#fbf7f0",
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                wrap="none",
                font=self.fonts["mono"],
                padx=16,
                pady=14,
            )
            self.boot_preview_widget.pack(fill="both", expand=True, padx=20, pady=(0, 20))
            self.boot_preview_widget.configure(state="disabled")

            tk.Label(
                right,
                text="Detected metadata files",
                bg=PANEL_ALT,
                fg=TEXT,
                font=self.fonts["subheading"],
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 10))

            self.metadata_preview_widget = scrolledtext.ScrolledText(
                right,
                bg="#fbf7f0",
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                wrap="word",
                font=self.fonts["mono"],
                padx=16,
                pady=14,
            )
            self.metadata_preview_widget.pack(fill="both", expand=True, padx=20, pady=(0, 20))
            self.metadata_preview_widget.configure(state="disabled")

        def set_text_widget(self, widget: "scrolledtext.ScrolledText", content: str) -> None:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("end", content)
            widget.configure(state="disabled")

        def refresh_details_view(self) -> None:
            if self.boot_preview_widget is None or self.metadata_preview_widget is None:
                return
            project = self.current_project
            if project is None:
                self.details_summary_var.set(
                    "Load a project to inspect its boot config, manifests, and ISO metadata."
                )
                self.set_text_widget(self.boot_preview_widget, "No project loaded yet.")
                self.set_text_widget(self.metadata_preview_widget, "No project loaded yet.")
                return

            paths = project["_paths"]
            iso_dir = paths["iso"]
            boot_configs = detect_boot_config_paths(iso_dir) if iso_dir.exists() else []
            if boot_configs:
                boot_preview = read_text_preview(boot_configs[0], line_limit=120)
            else:
                boot_preview = (
                    "Unpack the ISO to detect a primary GRUB or ISOLINUX config file.\n\n"
                    "Expected locations include:\n"
                    "- boot/grub/grub.cfg\n"
                    "- boot/grub/loopback.cfg\n"
                    "- isolinux/txt.cfg"
                )

            self.details_summary_var.set(project_details_report(project))
            self.set_text_widget(self.boot_preview_widget, boot_preview)
            self.set_text_widget(self.metadata_preview_widget, project_details_report(project))

        def handle_nav_back(self) -> None:
            current_idx = next((i for i, (k, _, _) in enumerate(WORKFLOW_STEPS) if k == self.current_page), 0)
            if current_idx > 0:
                self.show_page(WORKFLOW_STEPS[current_idx - 1][0])

        def handle_nav_forward(self) -> None:
            current_idx = next((i for i, (k, _, _) in enumerate(WORKFLOW_STEPS) if k == self.current_page), 0)
            if current_idx < len(WORKFLOW_STEPS) - 1:
                self.show_page(WORKFLOW_STEPS[current_idx + 1][0])

        def show_page(self, key: WorkflowStepKey) -> None:
            self.current_page = key
            frame = self.page_frames[key]
            frame.tkraise()
            
            # Update nav buttons
            current_idx = next((i for i, (k, _, _) in enumerate(WORKFLOW_STEPS) if k == self.current_page), 0)
            self.nav_back_button.config(state="normal" if current_idx > 0 else "disabled")
            self.nav_forward_button.config(state="normal" if current_idx < len(WORKFLOW_STEPS) - 1 else "disabled")
            
            # Update sidebar buttons
            for step_key, title, subtitle in WORKFLOW_STEPS:
                button = self.sidebar_buttons[step_key]
                if step_key == key:
                    button.configure(bg=ACCENT, activebackground=ACCENT_DARK)
                else:
                    button.configure(bg=SIDEBAR, activebackground=SIDEBAR_MUTED)
                if step_key == key:
                    self.hero_title_var.set(title)
                    self.hero_subtitle_var.set(subtitle)

        def append_log(self, line: str) -> None:
            assert self.log_widget is not None
            self.log_widget.configure(state="normal")
            self.log_widget.insert("end", line.rstrip() + "\n")
            self.log_widget.see("end")
            self.log_widget.configure(state="disabled")

        def set_busy(self, busy: bool, label: str = "Workflow log") -> None:
            self.busy = busy
            self.log_title_var.set(label)
            state = "disabled" if busy else "normal"
            for button in self.action_buttons:
                button.configure(state=state)
            if not busy:
                self.refresh_banner()

        def get_loaded_project(self) -> tuple[StudioProject, Path] | None:
            project = self.current_project
            project_dir = self.current_project_dir
            if project is None or project_dir is None:
                messagebox.showerror("Project required", "Open or initialize a project first.")
                return None
            return project, project_dir

        def describe_compression(self, key: str) -> str:
            choice = COMPRESSION_CHOICES.get(key, COMPRESSION_CHOICES[DEFAULT_COMPRESSION])
            return (
                f"{choice['title']} selected. {choice['summary']}. "
                f"{choice['details']}"
            )

        def refresh_banner(self) -> None:
            if self.busy:
                return
            project = self.current_project
            if project is None:
                self.status_banner_var.set(
                    "Start by choosing a project folder, then import or open an ISO studio."
                )
                self.sidebar_status_var.set("Project: none\nCompression: gzip\nVolume: default\nRootfs: waiting")
                self.project_summary_var.set("No project loaded yet.")
                self.build_summary_var.set(
                    "Build becomes available after a project has been unpacked."
                )
                self.details_summary_var.set(
                    "Load a project to inspect its boot config, manifests, and ISO metadata."
                )
                self.refresh_details_view()
                return

            paths = project["_paths"]
            compression = project["squashfs_compression"] or DEFAULT_COMPRESSION
            volume_id = project["volume_id"] or "(default)"
            rootfs = path_state(paths["rootfs"])
            self.status_banner_var.set(
                f"Loaded {paths['project']}. Rootfs is {rootfs}. "
                f"Compression is {compression.upper()}. Volume label is {volume_id}."
            )
            self.sidebar_status_var.set(
                f"Project: {project['name']}\n"
                f"Compression: {compression.upper()}\n"
                f"Volume: {volume_id}\n"
                f"Rootfs: {rootfs}"
            )
            self.project_summary_var.set(format_project_status(project))
            self.build_summary_var.set(
                f"Current output: {self.output_var.get() or default_output_path(project)}\n"
                f"Compression: {compression.upper()} | Volume: {volume_id} | "
                f"Block size: {project['squashfs_block_size'] or DEFAULT_BLOCK_SIZE}"
            )
            self.refresh_details_view()

        def choose_project_dir(self) -> None:
            selected = filedialog.askdirectory(
                title="Choose a Sirco Studio project folder",
                initialdir=self.project_dir_var.get() or str(Path.cwd()),
            )
            if selected:
                self.project_dir_var.set(selected)

        def choose_iso_file(self) -> None:
            selected = filedialog.askopenfilename(
                title="Choose a base ISO",
                initialdir=str(Path.cwd()),
                filetypes=[("ISO files", "*.iso"), ("All files", "*")],
            )
            if selected:
                self.iso_var.set(selected)

        def choose_output_file(self) -> None:
            selected = filedialog.asksaveasfilename(
                title="Choose the rebuilt ISO output path",
                initialfile=Path(self.output_var.get()).name or "custom.iso",
                defaultextension=".iso",
                filetypes=[("ISO files", "*.iso"), ("All files", "*")],
            )
            if selected:
                self.output_var.set(selected)
                self.refresh_banner()

        def project_dir(self) -> Path | None:
            raw = self.project_dir_var.get().strip()
            if not raw:
                messagebox.showerror("Project folder needed", "Pick a project folder first.")
                return None
            return Path(raw).expanduser().resolve()

        def open_project(self, project_dir: Path) -> None:
            try:
                project = read_project(project_dir)
            except ValueError as exc:
                messagebox.showerror("Project error", str(exc))
                return
            self.current_project = project
            self.current_project_dir = project_dir
            self.project_dir_var.set(str(project_dir))
            self.iso_var.set(str(project["_paths"]["base_iso"]))
            self.output_var.set(str(default_output_path(project)))
            self.volume_id_var.set(project["volume_id"] or "")
            self.compression_var.set(project["squashfs_compression"] or DEFAULT_COMPRESSION)
            self.compression_hint_var.set(self.describe_compression(self.compression_var.get()))
            self.refresh_banner()
            self.append_log(f"Loaded project: {project_dir}")

        def refresh_loaded_project(self) -> None:
            project_dir = self.project_dir()
            if project_dir is None:
                return
            if not (project_dir / PROJECT_FILE).exists():
                messagebox.showerror("Not a project", f"{project_dir} does not contain {PROJECT_FILE}.")
                return
            self.open_project(project_dir)

        def handle_open_existing_prompt(self) -> None:
            selected = filedialog.askdirectory(
                title="Open an existing Sirco Studio project",
                initialdir=self.project_dir_var.get() or str(Path.cwd()),
            )
            if not selected:
                return
            self.project_dir_var.set(selected)
            self.handle_open_project_from_field()

        def handle_open_project_from_field(self) -> None:
            project_dir = self.project_dir()
            if project_dir is None:
                return
            self.open_project(project_dir)

        def run_cli_task(
            self,
            cli_args: list[str],
            *,
            label: str,
            success_message: str | None = None,
            on_success: Callable[[], None] | None = None,
        ) -> None:
            if self.busy:
                messagebox.showinfo("Busy", "A workflow step is already running.")
                return

            command = [sys.executable, str(SCRIPT_PATH), *cli_args]
            self.append_log(f"$ {shlex.join(command)}")
            self.set_busy(True, f"Running: {label}")
            self.status_banner_var.set(f"{label} is running...")

            def worker() -> None:
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=str(Path.cwd()),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                except Exception as exc:  # pragma: no cover - UI/runtime branch
                    self.queue.put(("failed_to_start", str(exc), label))
                    return

                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        self.queue.put(("log", line.rstrip()))
                except Exception as exc:
                    self.queue.put(("log", f"Error reading output: {exc}"))
                returncode = process.wait()
                self.queue.put(("finished", returncode, label, success_message, on_success))

            threading.Thread(target=worker, daemon=True).start()

        def process_events(self) -> None:
            try:
                max_iterations = 100  # Prevent hanging
                iterations = 0
                while iterations < max_iterations:
                    event = self.queue.get_nowait()
                    if event[0] == "log":
                        _, line = event
                        self.append_log(line)
                    elif event[0] == "failed_to_start":
                        _, error, label = event
                        self.set_busy(False)
                        messagebox.showerror("Failed to run command", f"{label} could not start:\n\n{error}")
                    elif event[0] == "finished":
                        _, returncode, label, success_message, on_success = event
                        self.set_busy(False)
                        if returncode == 0:
                            if on_success:
                                on_success()
                            if success_message:
                                self.append_log(success_message)
                        else:
                            messagebox.showerror(
                                "Command failed",
                                f"{label} exited with status {returncode}. Check the workflow log for details.",
                            )
            except queue.Empty:
                pass
            finally:
                self.root.after(120, self.process_events)

        def handle_init_project(self) -> None:
            project_dir = self.project_dir()
            if project_dir is None:
                return

            iso_path = self.iso_var.get().strip()
            if not iso_path:
                messagebox.showerror("Base ISO needed", "Choose a base ISO first.")
                return
            if not Path(iso_path).expanduser().exists():
                messagebox.showerror("ISO missing", f"Base ISO not found:\n\n{iso_path}")
                return

            cli_args = ["init", str(project_dir), "--iso", str(Path(iso_path).expanduser())]
            if project_dir.exists() and any(project_dir.iterdir()):
                overwrite = messagebox.askyesno(
                    "Reuse existing folder?",
                    "That folder already has files in it.\n\nReuse it with --force?",
                )
                if not overwrite:
                    return
                cli_args.append("--force")

            self.run_cli_task(
                cli_args,
                label="Initialize project",
                success_message="Project initialized.",
                on_success=lambda: self.open_project(project_dir),
            )

        def handle_unpack_project(self) -> None:
            loaded = self.get_loaded_project()
            if loaded is None:
                return
            project, project_dir = loaded

            paths = project["_paths"]
            cli_args = ["unpack", str(project_dir)]
            if path_state(paths["iso"]) == "ready" or path_state(paths["rootfs"]) == "ready":
                replace = messagebox.askyesno(
                    "Replace current unpacked data?",
                    "The project already has unpacked `iso/` or `rootfs/` content.\n\nReplace it with a fresh unpack?",
                )
                if not replace:
                    return
                cli_args.append("--force")

            self.run_cli_task(
                cli_args,
                label="Unpack project",
                success_message="Project unpacked.",
                on_success=lambda: self.open_project(project_dir),
            )

        def handle_open_shell(self) -> None:
            loaded = self.get_loaded_project()
            if loaded is None:
                return
            project, project_dir = loaded

            if path_state(project["_paths"]["rootfs"]) != "ready":
                messagebox.showerror(
                    "Rootfs not ready",
                    "Unpack the project first so the rootfs exists before opening the shell.",
                )
                return

            try:
                launch_shell_terminal(project_dir)
            except RuntimeError as exc:
                messagebox.showerror("Terminal not found", str(exc))
                return
            self.append_log(f"Launched editing shell for {project_dir}")

        def handle_save_metadata(self) -> None:
            loaded = self.get_loaded_project()
            if loaded is None:
                return
            project, _ = loaded

            volume_id = self.volume_id_var.get().strip() or None
            project["volume_id"] = volume_id
            self.current_project = project
            save_project(project)
            self.refresh_banner()
            self.append_log(
                f"Saved volume label: {volume_id if volume_id else '(use base ISO default)'}"
            )

        def handle_compression_change(self) -> None:
            choice = self.compression_var.get()
            self.compression_hint_var.set(self.describe_compression(choice))
            project = self.current_project
            if project is None:
                return

            project["squashfs_compression"] = choice
            if project["squashfs_block_size"] is None:
                project["squashfs_block_size"] = DEFAULT_BLOCK_SIZE
            self.current_project = project
            save_project(project)
            self.refresh_banner()
            self.append_log(f"Saved compression choice: {choice}")

        def handle_build_project(self) -> None:
            loaded = self.get_loaded_project()
            if loaded is None:
                return
            project, project_dir = loaded

            if path_state(project["_paths"]["rootfs"]) != "ready":
                messagebox.showerror(
                    "Nothing to build yet",
                    "Unpack the project first so there is an editable rootfs to repack.",
                )
                return

            output = self.output_var.get().strip()
            cli_args = ["build", str(project_dir)]
            if output:
                cli_args.extend(["--output", output])

            self.run_cli_task(
                cli_args,
                label="Build ISO",
                success_message="ISO build completed.",
                on_success=lambda: self.open_project(project_dir),
            )

        def handle_clean_project(self) -> None:
            loaded = self.get_loaded_project()
            if loaded is None:
                return
            _, project_dir = loaded

            confirmed = messagebox.askyesno(
                "Clean unpacked data?",
                "This removes the extracted `iso/` tree and `rootfs/` contents, but keeps the project and base ISO.\n\nContinue?",
            )
            if not confirmed:
                return

            self.run_cli_task(
                ["clean", str(project_dir)],
                label="Clean project",
                success_message="Unpacked ISO data removed.",
                on_success=lambda: self.open_project(project_dir),
            )

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        die(f"could not start the GUI: {exc}")

    SircoStudioApp(root)
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "_shell-root":
        hidden_parser = argparse.ArgumentParser(prog="sirco_studio _shell-root")
        hidden_parser.add_argument("project", type=Path, help="project directory")
        args = hidden_parser.parse_args(argv[1:])
        cmd_shell_root(args)
        return 0
    if not argv or argv == ["--gui"]:
        # Escalate to root for GUI if needed
        if os.geteuid() != 0:
            if not command_exists("sudo"):
                die("GUI needs root privileges for many operations. Please run with: sudo python3 sirco_studio.py")
            os.execvp(
                "sudo",
                [
                    "sudo",
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--gui",
                ],
            )
        project_dir = Path.cwd()
        return launch_gui(project_dir if (project_dir / PROJECT_FILE).exists() else None)
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
