#!/usr/bin/env python3
"""Build publisher/runbook.json from the campaign calendar (authoritative source).

Output: runbook.json with repo-relative asset paths, fixed-offset UTC slots,
and IG-ready captions (French + Derja + hashtags embedded).

Africa/Tunis = UTC+01:00 year-round (no DST since 2008-10-26, verified).
"""
import json
import sys
from pathlib import Path

CAMP = Path(r"D:\reel-factory\clients\shift-digital\campaign-30d")
CAL = CAMP / "calendar" / "calendar.json"
OUT = Path(r"D:\reel-factory\publisher\runbook.json")

OFFSET_HOURS = 1  # Africa/Tunis fixed offset


def hhmm_to_utc(hhmm, date_str):
    """'19:00' on '2026-10-19' in Tunis → '18:00' UTC (fixed -1h)."""
    h, m = map(int, hhmm.split(":"))
    h -= OFFSET_HOURS
    if h < 0:
        h += 24
        # date stays the same for our slots (11:30, 18:00 UTC)
    return f"{h:02d}:{m:02d}"


def main():
    cal = json.loads(CAL.read_text(encoding="utf-8"))
    days = cal["days"]
    period_start = min(d["date"] for d in days)
    items = []

    for d in days:
        day = d["day"]
        slug = d["slug"]
        kind_map = {"reel": "REELS", "graphic": "IMAGE", "carousel": "CAROUSEL"}
        kind = kind_map.get(d.get("type", ""), "UNKNOWN")

        caption_parts = [d.get("caption_fr", "")]
        if d.get("caption_derja"):
            caption_parts.append(d["caption_derja"])
        if d.get("hashtags"):
            caption_parts.append(" ".join(d["hashtags"]))
        caption = "\n\n".join(p for p in caption_parts if p.strip()).strip()

        assets = {}
        if kind == "REELS":
            assets["video"] = f"media/reels/D{day:02d}-{slug}/final.mp4"
            assets["cover"] = f"media/reels/D{day:02d}-{slug}/cover.jpg"
        elif kind == "IMAGE":
            assets["image"] = f"media/graphics/D{day:02d}-{slug}/01-cover.jpg"
        elif kind == "CAROUSEL":
            # slides will be enumerated by export_repo.py; placeholder here
            assets["images"] = f"media/graphics/D{day:02d}-{slug}/"

        items.append({
            "day": day,
            "slug": slug,
            "kind": kind,
            "date": d["date"],
            "time_tunis": d["time"],
            "time_utc": hhmm_to_utc(d["time"], d["date"]),
            "title": d["title"],
            "caption": caption,
            "alt_text": d.get("alt_text", ""),
            "dm_keyword": d.get("cta", ""),
            "hashtags": d.get("hashtags", []),
            "assets": assets,
            "status": "planned",
        })

    runbook = {
        "generated": "2026-10-17T18:20:00Z",
        "tz": "Africa/Tunis UTC+01:00 (fixed, no DST)",
        "period_start": period_start,
        "period_end": max(d["date"] for d in days),
        "items": items,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(runbook, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(items)} items)")
    for it in items:
        print(f"  D{it['day']:02d} {it['slug']:<28} {it['kind']:<9} "
              f"{it['date']} {it['time_tunis']}T -> {it['time_utc']}Z")


if __name__ == "__main__":
    main()
