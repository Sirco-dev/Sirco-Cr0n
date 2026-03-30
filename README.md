# Sircron

## Sirco Studio

`./sirco-studio` and `python3 sirco_studio.py` now open a GUI by default.
The app keeps each ISO project in one folder, gives you a left-hand workflow
sidebar, launches the chroot in a real terminal, shows real ISO details like
boot configs and manifests, and lets you pick squashfs compression before the
final build.

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
python3 sirco_studio.py
```

CLI quick start:

```bash
./sirco-studio init my-iso --iso based-on/sirco-cron.iso
./sirco-studio unpack my-iso
./sirco-studio shell my-iso
./sirco-studio build my-iso --output my-iso/out/custom.iso
```

Notes:

- Running with no arguments opens the GUI; passing a subcommand keeps the CLI flow.
- `shell` uses a chroot with bind mounts, so it will re-run itself with `sudo`.
- The GUI shell button launches that same chroot flow in an external terminal emulator.
- The ISO details page shows detected GRUB/ISOLINUX config files plus manifest and size metadata.
- Compression choices are `xz`, `gzip`, and `lz4`, with size vs. speed tradeoffs explained in the app.
- `build` reuses the source ISO boot metadata via `xorriso`, then refreshes the
  squashfs, manifests, size files, and `md5sum.txt`.
- The current implementation is aimed at Ubuntu-style live/server ISOs.
