#!/usr/bin/env python3
"""SHIFT DIGITAL Instagram scheduler - publish-next entry point.

Design docs: clients/shift-digital/campaign-30d/publishing/ (and publisher/README.md).
Reads:  publisher/runbook.json   (the plan, generated from the campaign calendar)
        publisher/state/published.json  (ledger - idempotency + receipts)
Writes: publisher/state/published.json  (receipts; workflow commits it)
Stdlib only. The access token arrives via env META_ACCESS_TOKEN and is only
used through publisher.graph (header-only, redacted).

Modes:
  auto        publish the next DUE item (default; needs armed scheduler)
  --slug X    force item X (needs armed scheduler unless --dry-run)
  --dry-run   full plan/validation, ZERO write calls to Graph
  --vet       opt-in eligibility probe: token+identity+quota+one throwaway
              container (never published; expires after 24h)

Tunisia: Africa/Tunis = UTC+01:00 year-round (no DST since 2008-10-26, verified
against IANA tzdb 2026d + timeanddate 2026). Slot conversion is fixed +1h.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
ROOT = Path(__file__).resolve().parent
RUNBOOK = ROOT / "runbook.json"
STATE = ROOT / "state" / "published.json"
TZ_OFFSET = dt.timedelta(hours=1)   # Africa/Tunis: fixed, no DST

IG_IMAGE_MAX_BYTES = 8 * 1024 * 1024
IG_VIDEO_MAX_BYTES = 300 * 1024 * 1024
CAPTION_MAX = 2200
HASHTAG_MAX = 30
WORST_DELAY = dt.timedelta(hours=1, minutes=40)  # hourly cron + jitter allowance


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def to_tunis(ts):
    return ts + TZ_OFFSET


def log(msg):
    from graph import redact
    print(redact(msg), flush=True)


def summary(msg):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")


def load_json(path, default):
    if not Path(path).exists():
        return default
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ validation

def validate_item(item, repo_root):
    """Return (ok, messages). Checks IG media specs per docs (v25.0)."""
    errs, warns = [], []
    media = item.get("assets", {})
    files = []
    if item["kind"] == "REELS":
        files = [("video", media.get("video"))]
        cover = media.get("cover")
        if cover:
            files.append(("cover", cover))
    elif item["kind"] == "IMAGE":
        files = [("image", media.get("image"))]
    elif item["kind"] == "CAROUSEL":
        files = [(f"image[{i}]", p) for i, p in enumerate(media.get("images", []))]
        if not 2 <= len(files) <= 10:
            errs.append(f"carousel children count = {len(files)} (needs 2..10)")
    for label, rel in files:
        if not rel:
            errs.append(f"{label}: missing asset path")
            continue
        p = repo_root / rel
        if not p.exists():
            errs.append(f"{label}: file not committed? {rel}")
            continue
        size = p.stat().st_size
        if label.startswith("image") or label == "cover":
            if size > IG_IMAGE_MAX_BYTES:
                errs.append(f"{label}: {size} bytes > 8MB IG image limit")
            head = p.read_bytes()[:3]
            if head != b"\xff\xd8\xff":
                errs.append(f"{label}: not a JPEG file")
        elif label == "video":
            if size > IG_VIDEO_MAX_BYTES:
                errs.append(f"{label}: {size} bytes > 300MB reels limit")
            head = p.read_bytes()[:12]
            if b"ftyp" not in head:
                errs.append(f"{label}: not an MP4 (missing ftyp)")
    cap = item.get("caption") or ""
    tags = re.findall(r"#[\wÀ-ÿ]+", cap)
    mentions = re.findall(r"@[\w.]+", cap)
    if item.get("hashtags") and any(h not in cap for h in item["hashtags"]):
        warns.append("runbook hashtags missing from caption body (import-time bug)")
    if len(cap) > CAPTION_MAX:
        errs.append(f"caption {len(cap)} chars > 2200")
    if len(tags) > HASHTAG_MAX:
        errs.append(f"{len(tags)} hashtags > 30")
    if not cap.strip():
        errs.append("empty caption")
    return errs, warns, cap


# ------------------------------------------------------------------ plan/time

def due_items(runbook, state, ts=None):
    """Items due for publishing now, in plan order (one per run)."""
    ts = ts or now_utc()
    tunis_now = to_tunis(ts)
    today = tunis_now.date().isoformat()
    out, missed = [], []
    for it in runbook["items"]:
        slug = it["slug"]
        rec = state.get("published", {}).get(slug)
        if rec and rec.get("published_at_utc"):
            continue
        if it["date"] == today:
            slot = _parse_utc(it["time_utc"], it["date"])
            if slot and slot <= ts:
                out.append((it, slot))
        elif it["date"] < today and it["date"] >= runbook.get("period_start", "2026-10-19"):
            missed.append(it["slug"])
    out.sort(key=lambda x: x[1])
    return out, missed


def _parse_utc(hhmm, date_str=None):
    h, m = hhmm.split(":")
    if date_str:
        d = dt.date.fromisoformat(date_str)
    else:
        d = dt.datetime.now(dt.timezone.utc).date()
    return dt.datetime.combine(d, dt.time(int(h), int(m)), tzinfo=dt.timezone.utc)


def raw_base_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    return f"https://raw.githubusercontent.com/{repo}/{ref}"


# ------------------------------------------------------------------ flows

def publish_reel(item, g, repo_root, dry):
    rel_video = item["assets"]["video"]
    video = repo_root / rel_video
    cover_rel = item["assets"].get("cover")
    fb_page_id = item.get("facebook_page_id")
    if dry:
        log(f"[reel] DRY would upload {rel_video} via rupload")
        return {"planned": True, "video": rel_video}
    size = video.stat().st_size
    log(f"[reel] container media_type=REELS upload_type=resumable size={size}")
    params = {"media_type": "REELS", "upload_type": "resumable",
              "caption": item["caption"], "share_to_feed": "true"}
    if cover_rel:
        params["cover_url"] = f"{raw_base_url()}/{cover_rel}"
    cont = _retry(lambda: g.post(f"{g._session['ig_user_id']}/media", data=params), 3)
    cid = cont["id"]
    uri = cont.get("uri") or ""
    log(f"[reel] container {cid} uri={uri[:60] if uri else '(default)'}")
    g.upload_video_bytes(cid, str(video), size)
    st = g.poll_container(cid)
    if st != "FINISHED":
        raise RuntimeError(f"reel container status = {st}")
    pub = _retry(lambda: g.post(f"{g._session['ig_user_id']}/media_publish",
                                data={"creation_id": cid}), 3)
    ig_media_id = pub["id"]
    log(f"[reel] Instagram published: {ig_media_id}")
    # Cross-post to Facebook if page_id configured
    fb_media_id = None
    if fb_page_id:
        fb_media_id = publish_to_facebook_reel(g, fb_page_id, str(video), item["caption"])
    return {"media_id": ig_media_id, "container_id": cid, "fb_media_id": fb_media_id}


def publish_to_facebook_reel(g, page_id, video_path, caption):
    """Publish the same video to Facebook Page as a Reel using the standard video API."""
    import os
    log(f"[fb] cross-posting to Page {page_id}")
    if not os.path.exists(video_path):
        log(f"[fb] video not found: {video_path}")
        return None
    size = os.path.getsize(video_path)
    # Use the standard video upload to Page
    # First, start a resumable upload session
    cont = _retry(lambda: g.post(f"{page_id}/videos",
        data={"upload_phase": "start", "file_size": str(size)}), 3)
    fb_video_id = cont.get("video_id") or cont.get("id")
    upload_url = cont.get("upload_url") or cont.get("uri")
    if not fb_video_id or not upload_url:
        log(f"[fb] failed to create upload session: {cont}")
        return None
    log(f"[fb] upload session {fb_video_id}")
    # Upload video bytes
    req_headers = {
        "Authorization": f"OAuth {g._session['token']}",
        "offset": "0",
        "file_size": str(size),
        "Content-Type": "application/octet-stream",
        "User-Agent": "shift-digital-scheduler/1.0"
    }
    data = open(video_path, "rb").read()
    req = urllib.request.Request(upload_url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8")
            log(f"[fb] upload response: {body[:200]}")
    except Exception as e:
        log(f"[fb] upload failed: {e}")
        return None
    # Finish upload and publish as Reel
    pub = _retry(lambda: g.post(f"{page_id}/videos",
        data={"upload_phase": "finish", "video_id": fb_video_id, "description": caption,
              "published": "true", "is_reel": "true"}), 3)
    fb_media_id = pub.get("id") or pub.get("video_id")
    log(f"[fb] Facebook Reel published: {fb_media_id}")
    return fb_media_id


def publish_image(item, g, repo_root, dry):
    rel = item["assets"]["image"]
    if dry:
        log(f"[image] DRY would create image container: {raw_base_url()}/{rel}")
        return {"planned": True, "image": rel}
    p = repo_root / rel
    log(f"[image] single image {rel} ({p.stat().st_size} bytes)")
    params = {"image_url": f"{raw_base_url()}/{rel}", "caption": item["caption"]}
    if item.get("alt_text"):
        params["alt_text"] = item["alt_text"][:1000]
    cont = _retry(lambda: g.post(f"{g._session['ig_user_id']}/media", data=params), 3)
    cid = cont["id"]
    st = g.poll_container(cid, max_seconds=200)
    if st != "FINISHED":
        raise RuntimeError(f"image container status = {st}")
    pub = _retry(lambda: g.post(f"{g._session['ig_user_id']}/media_publish",
                                data={"creation_id": cid}), 3)
    return {"media_id": pub["id"], "container_id": cid}


def publish_carousel(item, g, repo_root, dry):
    images = item["assets"]["images"]
    log(f"[carousel] {len(images)} children")
    if dry:
        for r in images:
            log(f"[carousel] DRY child: {raw_base_url()}/{r}")
        return {"planned": True, "children": len(images)}
    child_ids = []
    for i, rel in enumerate(images):
        params = {"image_url": f"{raw_base_url()}/{rel}", "is_carousel_item": "true"}
        if i == 0 and item.get("alt_text"):
            params["alt_text"] = item["alt_text"][:1000]
        cont = _retry(lambda: g.post(f"{g._session['ig_user_id']}/media", data=params), 3)
        child_ids.append(cont["id"])
        log(f"[carousel] child {i + 1}/{len(images)} container {cont['id']}")
    parent = _retry(lambda: g.post(f"{g._session['ig_user_id']}/media", data={
        "media_type": "CAROUSEL", "children": ",".join(child_ids),
        "caption": item["caption"], "share_to_feed": "true"}), 3)
    pid = parent["id"]
    st = g.poll_container(pid)
    if st != "FINISHED":
        raise RuntimeError(f"carousel container status = {st}")
    pub = _retry(lambda: g.post(f"{g._session['ig_user_id']}/media_publish",
                                data={"creation_id": pid}), 3)
    return {"media_id": pub["id"], "container_id": pid, "children": child_ids}


def _retry(fn, attempts):
    import time as _t
    delay = 5
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:   # GraphError retriables, network errors
            retriable = getattr(e, "retriable", True)
            last = e
            if i < attempts - 1 and retriable:
                _t.sleep(delay)
                delay = min(60, delay * 3)
                continue
            raise
    raise last


# ------------------------------------------------------------------ dedup

def duplicate_live(item, g, dry):
    """Abort if an existing live post carries this item's caption text."""
    if dry:
        return None
    try:
        posts = g.recent_media(g._session["ig_user_id"], limit=10)
    except Exception:
        return None
    probe = re.sub(r"\s+", " ", (item["caption"] or "")).strip()[:60]
    if not probe:
        return None
    for p in posts:
        cap = re.sub(r"\s+", " ", (p.get("caption") or "")).strip()
        if probe and probe in cap:
            return p
    return None


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "slug"])
    ap.add_argument("--slug", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vet", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="allow --slug to publish while scheduler is unarmed (per-post explicit approval)")
    args = ap.parse_args()

    armed = os.environ.get("SCHEDULER_ENABLED", "false").lower() == "true"
    repo_root = Path(os.environ.get("REPO_ROOT", ROOT.parent))
    runbook = load_json(RUNBOOK, {"items": []})
    state = load_json(STATE, {"published": {}})
    state.setdefault("published", {})

    token = os.environ.get("META_ACCESS_TOKEN") or os.environ.get("META_LONG_TOKEN")
    ig_user = os.environ.get("META_IG_ID")
    dry = args.dry_run
    vet = args.vet

    import graph as g
    if token and ig_user:
        g.configure(token, ig_user, os.environ.get("META_API_VERSION", "v25.0"))
        try:
            me = g.me_probe(g._session["ig_user_id"])
            log(f"[auth] token OK - IG user {me.get('username')} (id {me.get('id')})")
        except Exception as e:
            log(f"[auth] PROBE FAILED: {e}")
            if not dry:
                return 1
    elif not dry and not vet:
        log("[auth] missing META_ACCESS_TOKEN / META_IG_ID secrets")
        return 1

    # ---- vet mode: eligibility probe only (never publishes)
    if vet:
        if dry:
            log("[vet] --vet is already non-publishing; running as vet")
        total = g.content_publishing_limit(g._session["ig_user_id"])
        log(f"[vet] quota response: {total}")
        first = next((it for it in runbook["items"] if it["assets"].get("image")),
                     None)
        if first:
            rel = first["assets"]["image"]
            probe_url = f"{raw_base_url()}/{rel}"
            log(f"[vet] creating ONE throwaway image container from {probe_url}")
            try:
                cont = g.post(f"{g._session['ig_user_id']}/media",
                              data={"image_url": probe_url})
                st = g.poll_container(cont["id"], max_seconds=200, interval=30)
                log(f"[vet] throwaway container {cont['id']} status={st} "
                    "(left to expire in 24h; NOT published)")
                summary(f"Eligibility probe: container {st} — PPA/2FA/asset-fetch verified at container level.")
            except Exception as e:
                log(f"[vet] container probe FAILED: {e} "
                    "(PPA required? page 2FA incomplete? asset URL unreachable?)")
                return 1
        return 0

    # ---- item selection
    if args.slug or args.mode == "slug":
        it = next((x for x in runbook["items"] if x["slug"] == args.slug), None)
        if not it:
            log(f"slug {args.slug} not in runbook")
            return 1
        item = it
    else:
        due, missed = due_items(runbook, state)
        for m in missed:
            key = f"missed::{m}"
            if key not in state:
                state[key] = {"missed_at": now_utc().isoformat(), "slug": m}
                log(f"[plan] MISSED day item (not auto-posting late): {m}")
                summary(f"⚠ MISSED schedule: {m} — was never published; decide manually.")
        if state:
            save_state(state)
        if not due:
            log("[plan] nothing due in this window")
            return 0
        item = due[0][0]

    if item["slug"] in state.get("published", {}):
        rec = state["published"][item["slug"]]
        log(f"ALREADY published: {item['slug']} at {rec.get('published_at_utc')} "
            f"permalink={rec.get('permalink')}")
        summary(f"Idempotency guard: `{item['slug']}` already published → no action.")
        return 0

    if (not armed) and (not dry):
        if args.mode == "slug" and args.force:
            log("[arm] per-post force publish requested via --force (explicit approval)")
        else:
            log("Scheduler UNARMED (set SCHEDULER_ENABLED=true secret to arm). No publish. "
                "Run a --dry-run dispatch to see the plan.")
            summary("Scheduler is UNARMED — nothing published. Set secret "
                    "`SCHEDULER_ENABLED=true` only when you approve the calendar.")
            return 0

    errs, warns, caption = validate_item(item, repo_root)
    item["caption"] = caption
    for w in warns:
        log(f"[warn] {w}")
    if errs:
        for e in errs:
            log(f"[invalid] {e}")
        return 1

    dup = duplicate_live(item, g, dry)
    if dup:
        log(f"[dedup] live post {dup.get('id')} {dup.get('permalink')} already contains "
            "this caption - refusing to double-post")
        summary(f"Dup prevented for `{item['slug']}`: existing post {dup.get('permalink')}")
        return 0

    kind = item["kind"]
    flow = {"REELS": publish_reel, "IMAGE": publish_image,
            "CAROUSEL": publish_carousel}[kind]
    log(f"[plan] {item['day']} {item['slug']} kind={kind} tunis={item['time_tunis']} "
        f"utc={item['time_utc']} dry_run={dry}")
    if dry and kind not in ("REELS", "IMAGE", "CAROUSEL"):
        log(f"unknown kind {kind}")
        return 1

    quota = g.content_publishing_limit(g._session["ig_user_id"]) if not dry else None
    if quota and quota[2] < 1:
        log(f"[quota] exhausted: {quota}")
        return 1

    try:
        result = flow(item, g, repo_root, dry)
    except Exception as e:
        log(f"[FAIL] {kind} publish error: {e}")
        summary(f"❌ Publish failed for `{item['slug']}`: {e}")
        return 1

    if dry:
        log(f"[dry] plan complete for {item['slug']} — NO platform call made")
        summary(f"DRY-RUN OK for `{item['slug']}` ({kind}) — nothing published.")
        return 0

    try:
        perm = g.permalink_of(result["media_id"])
    except Exception:
        perm = {}
    rec = {"slug": item["slug"], "day": item["day"], "kind": kind,
           "media_id": result["media_id"],
           "container_id": result.get("container_id"),
           "fb_media_id": result.get("fb_media_id"),
           "permalink": perm.get("permalink"),
           "published_at_utc": now_utc().isoformat(),
           "published_at_tunis": to_tunis(now_utc()).isoformat(),
           "caption_head": item["caption"][:60]}
    state.setdefault("published", {})[item["slug"]] = rec
    save_state(state)
    log(f"[receipt] {item['slug']} media_id={rec['media_id']} "
        f"fb_media_id={rec.get('fb_media_id') or 'none'} "
        f"permalink={rec.get('permalink') or '(fetch pending)'}")
    summary(f"Published `{item['slug']}` -> IG `{rec['media_id']}` "
            f"FB `{rec.get('fb_media_id') or 'none'}` "
            f"{rec.get('permalink') or '(permalink pending)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
