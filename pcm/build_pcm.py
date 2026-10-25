"""Build the KiCad PCM archive.

    python pcm/build_pcm.py   ->  dist/fab-release-<version>-pcm.zip
    Install: KiCad > Plugin and Content Manager > Install from File...

Layout per KiCad PCM: metadata.json at the root, code under plugins/, 64x64
resources/icon.png. plugins/ gets the toolbar action, release.py, scripts/,
skills/ (the Claude review) and the vendored prefab_gate/, so the installed package needs nothing
from this repo. To update the team: bump "version" in metadata.json, rebuild,
reinstall from file.

It also writes dist/kicad-submission/<identifier>/ (metadata.json + icon.png) for a merge
request to gitlab.com/kicad/addons/metadata, once the zip is attached to the GitHub release
v<version>; download_url assumes that.
"""
import hashlib
import json
import struct
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The logo, per KiCad's icon guidelines (dev-docs.kicad.org/en/rules-guidelines/icon-design):
# flat, KiCad palette only, drawn on the 24 px grid (same geometry as pcm/logo.svg). A
# soldermask board with a copper track routed as a tick at 45 degrees, a via at each end.
MASK, GOLD = (0x00, 0x64, 0x00), (0xF2, 0x91, 0x00)  # PCB Dark Green, Gold
HOLE = {False: (0xB9, 0xB9, 0xB9), True: (0xE0, 0xE0, 0xE0)}  # Accent Grey, light/dark theme
EDGE = (0xDE, 0xD3, 0xDD)  # dark theme Primary Gray: keeps the board apart from a dark toolbar
TICK = ((6.5, 12.5), (10.5, 16.5), (17.5, 9.5))  # 3 px track centred on half pixels


def _seg_dist(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _logo(x, y, dark):
    """RGBA of the logo at (x, y) on the 24 px grid."""
    q = 4  # board corner radius; the board fills 1..23
    cx, cy = min(max(x, 1 + q), 23 - q), min(max(y, 1 + q), 23 - q)
    edge = q - ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5  # distance in from the board edge
    if edge < 0:
        return (0, 0, 0, 0)
    if dark and edge < 1:
        return (*EDGE, 255)
    for vx, vy in (TICK[0], TICK[-1]):
        r = ((x - vx) ** 2 + (y - vy) ** 2) ** 0.5
        if r < 1:
            return (*HOLE[dark], 255)
        if r < 2.5:
            return (*GOLD, 255)
    if min(_seg_dist((x, y), a, b) for a, b in zip(TICK, TICK[1:])) < 1.5:
        return (*GOLD, 255)
    return (*MASK, 255)


def png(size, dark=False, ss=4):
    """The logo as RGBA PNG bytes, ss x ss supersampled. Stdlib only: KiCad's python has no Pillow."""
    def pixel(x, y):
        s = [_logo((x + (i + 0.5) / ss) * 24 / size, (y + (j + 0.5) / ss) * 24 / size, dark)
             for i in range(ss) for j in range(ss)]
        a = sum(c[3] for c in s)
        return (*(sum(c[k] * c[3] for c in s) // a if a else 0 for k in range(3)), a // len(s))
    rows = b"".join(b"\0" + bytes(c for x in range(size) for c in pixel(x, y)) for y in range(size))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _files(root):
    return [p for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]


def build(out_dir, root=ROOT):
    """Write the archive into out_dir and return its path."""
    meta = json.loads((root / "pcm" / "metadata.json").read_text(encoding="utf-8"))
    archive = Path(out_dir) / f"fab-release-{meta['versions'][0]['version']}-pcm.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(meta, indent=4))
        z.writestr("plugins/VERSION", meta["versions"][0]["version"])  # dialog title, manifest
        z.writestr("resources/icon.png", png(64))
        for size in (24, 48):  # toolbar + HiDPI; no 32: KiCad then draws the button too big
            z.writestr(f"plugins/icon{size}.png", png(size))
            z.writestr(f"plugins/icon{size}_dark.png", png(size, dark=True))
        z.writestr("plugins/icon64.png", png(64))  # dialogs' window icon
        z.write(root / "release.py", "plugins/release.py")
        for src, dest in ((root / "pcm" / "plugin", "plugins"), (root / "scripts", "plugins/scripts"),
                          (root / "fab", "plugins/fab"), (root / "prefab_gate", "plugins/prefab_gate"),
                          (root / "skills", "plugins/skills")):
            for p in _files(src):
                z.write(p, f"{dest}/{p.relative_to(src).as_posix()}")
        z.write(root / "LICENSE", "plugins/LICENSE")
    return archive


def submission(archive, out_dir, root=ROOT):
    """The repository entry for KiCad's add-on index: our metadata plus where the zip is
    published, its SHA-256 and sizes, and the 64x64 icon. Returns its folder."""
    meta = json.loads((root / "pcm" / "metadata.json").read_text(encoding="utf-8"))
    (v,) = meta["versions"]
    data = Path(archive).read_bytes()
    with zipfile.ZipFile(archive) as z:
        install = sum(i.file_size for i in z.infolist())
    v.update(download_url=f"{meta['resources']['homepage']}/releases/download/v{v['version']}/"
                          f"{Path(archive).name}",
             download_sha256=hashlib.sha256(data).hexdigest(),
             download_size=len(data), install_size=install)
    folder = Path(out_dir) / "kicad-submission" / meta["identifier"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "metadata.json").write_text(json.dumps(meta, indent=4, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
    (folder / "icon.png").write_bytes(png(64))
    return folder


if __name__ == "__main__":
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    archive = build(out)
    print(f"Built {archive}\nInstall: KiCad > Plugin and Content Manager > Install from File...")
    print(f"KiCad add-on submission: {submission(archive, out)}")
