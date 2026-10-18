#!/usr/bin/env python3
"""Graph API client for the SHIFT DIGITAL Instagram scheduler.

stdlib-only (urllib). Security rules enforced here:
- the access token is sent ONLY in the Authorization header, never in a URL;
- every string that leaves this module (logs, errors, receipts) is passed
  through redact(), which strips the token value and any access_token query
  parameter;
- no request returns before redaction, so a caller cannot log an unredacted
  failure without trying very hard.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"

USER_AGENT = "shift-digital-scheduler/1.0 (+github-actions)"

# Profile used by the dry-run "plan" dump. Never contains the token.
_session = {"token": None, "api_version": "v25.0", "ig_user_id": None}


class GraphError(RuntimeError):
    """Raised with a REDACTED human-readable message."""

    def __init__(self, message, kind="graph", retriable=False):
        super().__init__(message)
        self.kind = kind
        self.retriable = retriable


def configure(token, ig_user_id, api_version="v25.0"):
    _session["token"] = token
    _session["ig_user_id"] = ig_user_id
    _session["api_version"] = api_version


def redact(text, token=None):
    import re
    token = token or _session.get("token") or ""
    out = str(text)
    if token and len(token) >= 4:
        out = out.replace(token, "[REDACTED]")
    # access_token=x in any URL — strip the value regardless of what it is
    out = re.sub(r"access_token=[^&\s]+", "access_token=[REDACTED]", out)
    return out


def _headers():
    return {
        "Authorization": f"Bearer {_session['token']}",
        "User-Agent": USER_AGENT,
    }


def request(method, path, params=None, data=None, timeout=45):
    """Graph API call. path like '{0000}/media' or a full URL"""
    if not _session.get("token"):
        raise GraphError("token not configured", kind="config")
    url = f"{GRAPH}/{_session['api_version']}/{path}" if not path.startswith("http") else path
    base = dict(params or {})
    base.pop("access_token", None)          # header-only auth
    call = url
    if method == "GET" and base:
        call = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(base)
    payload = None
    if data is not None:
        payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(call, data=payload, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        retriable = 500 <= getattr(e, "code", 0) < 600
        raise GraphError(f"HTTP {e.code} on {path}: {redact(detail or e.reason)}",
                         "graph", retriable) from None
    except urllib.error.URLError as e:
        raise GraphError(f"network error on {path}: {redact(e.reason)}", "network", True) from None


def get(path, params=None):
    return request("GET", path, params=params)


def post(path, data=None, params=None):
    return request("POST", path, data=data, params=params)


def upload_video_bytes(container_id, file_path, file_size, retries=2):
    """Resumable rupload of LOCAL bytes. Returns True on success."""
    url = f"{RUPLOAD}/{_session['api_version']}/{container_id}"
    attempt = 0
    while True:
        attempt += 1
        if file_size < 64 * 1024 * 1024:
            with open(file_path, "rb") as fh:
                raw = fh.read()
        else:
            raw = None
        req = urllib.request.Request(
            url,
            data=raw,
            headers={
                "Authorization": f"OAuth {_session['token']}",
                "offset": "0",
                "file_size": str(file_size),
                "Content-Type": "application/octet-stream",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if body.get("success") is True:
                    return body
                raise GraphError(f"rupload refused: {redact(body)}", "upload", True)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if attempt <= retries and (500 <= e.code < 600 or e.code == 429):
                time.sleep(min(60, 5 * attempt))
                continue
            raise GraphError(f"rupload HTTP {e.code}: {redact(detail)}", "upload", False) from None
        except urllib.error.URLError as e:
            if attempt <= retries:
                time.sleep(min(60, 5 * attempt))
                continue
            raise GraphError(f"rupload network: {redact(e.reason)}", "upload", False) from None


# ---------------------------------------------------------------- read-only helpers

def me_probe(ig_user_id):
    """Validate token + IG user identity (read-only)."""
    return get(f"{ig_user_id}", {"fields": "id,username"})


def content_publishing_limit(ig_user_id):
    try:
        r = get(f"{ig_user_id}/content_publishing_limit",
                {"fields": "quota_total,quota_used,config_id"})
        total, used = r.get("quota_total", 100), r.get("quota_used", 0)
        return total, used, total - used
    except GraphError:
        return None


def recent_media(ig_user_id, limit=10):
    """Live media with captions for duplicate prevention."""
    r = get(f"{ig_user_id}/media",
            {"fields": "id,permalink,caption,media_product_type",
             "limit": limit})
    return r.get("data", [])


def container_status(container_id):
    r = get(f"{container_id}", {"fields": "status_code"})
    return r.get("status_code")


def poll_container(container_id, max_seconds=310, interval=60):
    """Poll once/minute <=5 min (per docs recommendation)."""
    deadline = time.time() + max_seconds
    last = None
    while time.time() < deadline:
        st = container_status(container_id)
        last = st
        if st == "FINISHED":
            return "FINISHED"
        if st in ("ERROR", "EXPIRED"):
            return st
        time.sleep(interval)
    return last or "UNKNOWN"


def permalink_of(media_id):
    r = get(f"{media_id}", {"fields": "permalink,media_product_type,media_type"})
    return {"permalink": r.get("permalink"), "media_type": r.get("media_type"),
            "product": r.get("media_product_type")}
