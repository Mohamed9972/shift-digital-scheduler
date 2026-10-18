#!/usr/bin/env python3
"""Build repo-skeleton/ — the git-publishable tree for GitHub Actions.

Converts PNG slides to JPEG (IG requirement), copies reel finals + covers +
captions, generates media-manifest.json with SHA-256 hashes.

Run from D:\reel-factory:
    python publisher/export_repo.py
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\reel-factory")
CAMP = ROOT / "clients" / "shift-digital" / "campaign-30d"
OUT = ROOT / "publisher" / "repo-skeleton"
FFMPEG = ROOT / "node_modules" / "ffmpeg-static" / "ffmpeg.exe"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffmpeg_to_jpg(src, dst):
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(src), "-vf", "scale=1080:-1", "-q:v", "2", str(dst)],
        check=True
    )


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # 1. Copy publisher code
    shutil.copytree(ROOT / "publisher", OUT / "publisher",
                    ignore=shutil.ignore_patterns("repo-skeleton", "__pycache__",
                                                 "*.pyc", "state"))

    # 2. Copy workflow
    (OUT / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / ".github" / "workflows" / "instagram-publish.yml",
                 OUT / ".github" / "workflows" / "instagram-publish.yml")

    # 3. Copy reel finals + covers + captions
    reels_src = CAMP / "reels"
    reels_dst = OUT / "media" / "reels"
    reels_dst.mkdir(parents=True)
    manifest = {"reels": {}, "images": {}, "carousels": {}}

    for d in sorted(reels_src.iterdir()):
        if not d.is_dir():
            continue
        dst = reels_dst / d.name
        dst.mkdir()
        for f in ["final.mp4", "cover.jpg", "caption.txt", "captions.srt"]:
            src = d / f
            if src.exists():
                shutil.copy2(src, dst / f)
                manifest["reels"][f"{d.name}/{f}"] = {
                    "sha256": sha256(src), "size": src.stat().st_size
                }

    # 4. Convert graphics PNGs -> JPEGs
    gfx_src = CAMP / "graphics"
    gfx_dst = OUT / "media" / "graphics"
    runbook = json.loads((ROOT / "publisher" / "runbook.json").read_text(encoding="utf-8"))

    for item in runbook["items"]:
        if item["kind"] == "REELS":
            continue
        slug = item["slug"]
        day = item["day"]
        deck_name = f"D{day:02d}-{slug}"
        src_dir = gfx_src / deck_name
        if not src_dir.exists():
            print(f"WARN: deck dir missing: {src_dir}")
            continue
        dst_dir = gfx_dst / deck_name
        dst_dir.mkdir(parents=True)
        pngs = sorted(src_dir.glob("*.png"))
        jpgs = []
        for png in pngs:
            jpg = dst_dir / (png.stem + ".jpg")
            ffmpeg_to_jpg(png, jpg)
            jpgs.append(jpg.name)
            key = f"{deck_name}/{jpg.name}"
            if item["kind"] == "IMAGE":
                manifest["images"][key] = {"sha256": sha256(jpg), "size": jpg.stat().st_size}
            else:
                manifest["carousels"][key] = {"sha256": sha256(jpg), "size": jpg.stat().st_size}
        # Update runbook assets to point to .jpg files
        if item["kind"] == "IMAGE":
            item["assets"]["image"] = f"media/graphics/{deck_name}/{jpgs[0]}"
        elif item["kind"] == "CAROUSEL":
            item["assets"]["images"] = [f"media/graphics/{deck_name}/{j}" for j in jpgs]

    # 5. Copy calendar for audit
    cal_dst = OUT / "media" / "calendar"
    cal_dst.mkdir(parents=True)
    shutil.copy2(CAMP / "calendar" / "calendar.json", cal_dst / "calendar.json")

    # 6. Write updated runbook (with .jpg paths) into skeleton
    rb_out = OUT / "publisher" / "runbook.json"
    rb_out.write_text(json.dumps(runbook, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 7. Write media manifest
    (OUT / "media" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 8. Write .gitignore
    (OUT / ".gitignore").write_text(
        "publisher/state/\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )

    # Summary
    total = sum(v["size"] for v in manifest["reels"].values())
    total += sum(v["size"] for v in manifest["images"].values())
    total += sum(v["size"] for v in manifest["carousels"].values())
    print(f"repo-skeleton ready at {OUT}")
    print(f"  reels: {len(manifest['reels'])} files")
    print(f"  images: {len(manifest['images'])} files")
    print(f"  carousel slides: {len(manifest['carousels'])} files")
    print(f"  total media: {total / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
