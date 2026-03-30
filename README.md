# Sircron

## Sirco Studio

`./sirco-studio` is a lightweight, terminal-first Cubic-style workflow
that keeps each ISO project in one folder.

Project layout:

```text
my-iso/
  studio.json
  base.iso
  iso/
  rootfs/
  out/
  tmp/
```

Quick start:

```bash
./sirco-studio init my-iso --iso based-on/sirco-cron.iso
./sirco-studio unpack my-iso
./sirco-studio shell my-iso
./sirco-studio build my-iso --output my-iso/out/custom.iso
```

Notes:

- `shell` uses a chroot with bind mounts, so it will re-run itself with `sudo`.
- `build` reuses the source ISO boot metadata via `xorriso`, then refreshes the
  squashfs, manifests, size files, and `md5sum.txt`.
- The current implementation is aimed at Ubuntu-style live/server ISOs.
