#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from hashlib import md5
from pathlib import Path
from typing import Iterable


PROJECT_FILE = "studio.json"
PROJECT_VERSION = 1


def info(message: str) -> None:
    print(message)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def die(message: str, code: int = 1) -> "NoReturn":
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


def project_paths(project_dir: Path) -> dict[str, Path]:
    return {
        "project": project_dir,
        "config": project_dir / PROJECT_FILE,
        "base_iso": project_dir / "base.iso",
        "iso": project_dir / "iso",
        "rootfs": project_dir / "rootfs",
        "out": project_dir / "out",
        "tmp": project_dir / "tmp",
    }


def load_project(project_dir: Path) -> dict:
    config_path = project_dir / PROJECT_FILE
    if not config_path.exists():
        die(f"{project_dir} is not a Sirco Studio project ({PROJECT_FILE} missing)")
    with config_path.open() as fh:
        data = json.load(fh)
    if data.get("project_version") != PROJECT_VERSION:
        die(
            f"unsupported project version {data.get('project_version')}, "
            f"expected {PROJECT_VERSION}"
        )
    data["_paths"] = project_paths(project_dir)
    return data


def save_project(project: dict) -> None:
    data = {k: v for k, v in project.items() if not k.startswith("_")}
    config_path = Path(project["_paths"]["config"])
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def human_exists(path: Path) -> str:
    return "yes" if path.exists() else "no"


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
    return __import__("shlex").split(text)


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


def parse_squashfs_details(squashfs_path: Path) -> dict[str, str | int]:
    result = run(["unsquashfs", "-s", str(squashfs_path)], capture=True)
    compression = "gzip"
    block_size = 131072
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


def write_shell_rc(project: dict) -> Path:
    rootfs_dir = Path(project["_paths"]["rootfs"])
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


def ensure_rootfs_ready(project: dict) -> None:
    rootfs_dir = Path(project["_paths"]["rootfs"])
    if not rootfs_dir.exists():
        die("rootfs is not unpacked yet; run unpack first")
    if not any(rootfs_dir.iterdir()):
        die("rootfs exists but is empty; run unpack again")


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
    for key in ("iso", "rootfs", "out", "tmp"):
        paths[key].mkdir(parents=True, exist_ok=True)

    if paths["base_iso"].exists():
        if args.force:
            paths["base_iso"].unlink()
        else:
            die(f"{paths['base_iso']} already exists")
    mode = try_hardlink_or_copy(iso_path, paths["base_iso"])
    volume_id = inspect_volume_id(paths["base_iso"])

    project = {
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
    maybe_clear(Path(paths["iso"]), args.force)
    maybe_clear(Path(paths["rootfs"]), args.force)
    Path(paths["iso"]).mkdir(parents=True, exist_ok=True)
    Path(paths["rootfs"]).mkdir(parents=True, exist_ok=True)

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

    rootfs_relpath = detect_rootfs_squashfs(Path(paths["iso"]))
    squashfs_path = Path(paths["iso"]) / rootfs_relpath
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
    paths = project["_paths"]
    print(f"Project          : {paths['project']}")
    print(f"Base ISO         : {paths['base_iso']} ({human_exists(Path(paths['base_iso']))})")
    print(f"ISO tree         : {paths['iso']} ({human_exists(Path(paths['iso']))})")
    print(f"Rootfs           : {paths['rootfs']} ({human_exists(Path(paths['rootfs']))})")
    print(f"Output dir       : {paths['out']} ({human_exists(Path(paths['out']))})")
    print(f"Volume ID        : {project.get('volume_id') or '(unknown)'}")
    print(f"Rootfs squashfs  : {project.get('rootfs_squashfs') or '(not detected yet)'}")
    print(
        f"Squashfs settings: "
        f"{project.get('squashfs_compression') or '?'} / "
        f"{project.get('squashfs_block_size') or '?'}"
    )


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
                str(Path(__file__).resolve()),
                "_shell-root",
                str(project_dir),
            ],
        )
    cmd_shell_root(args)


def cmd_shell_root(args: argparse.Namespace) -> None:
    project = load_project(args.project.resolve())
    ensure_rootfs_ready(project)
    rootfs_dir = Path(project["_paths"]["rootfs"])
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
    iso_dir = Path(paths["iso"])
    rootfs_dir = Path(paths["rootfs"])
    if not iso_dir.exists():
        die("iso tree is not unpacked yet; run unpack first")

    rootfs_relpath = project.get("rootfs_squashfs") or detect_rootfs_squashfs(iso_dir)
    squashfs_path = iso_dir / rootfs_relpath
    if not squashfs_path.parent.exists():
        squashfs_path.parent.mkdir(parents=True, exist_ok=True)

    compression = project.get("squashfs_compression") or "gzip"
    block_size = int(project.get("squashfs_block_size") or 131072)

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

    output_path = args.output.resolve() if args.output else Path(paths["out"]) / f"{project['name']}.iso"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    boot_args = boot_replay_args(Path(paths["base_iso"]))
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
    for key in ("iso", "rootfs"):
        path = Path(paths[key])
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
        description="One-folder, terminal-first ISO studio for Ubuntu-style live/server images.",
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


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "_shell-root":
        hidden_parser = argparse.ArgumentParser(prog="sirco_studio _shell-root")
        hidden_parser.add_argument("project", type=Path, help="project directory")
        args = hidden_parser.parse_args(argv[1:])
        cmd_shell_root(args)
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
