"""Client for deepiri-platform plaky-bridge (headless browser, 100% background, no GUI).

Real Plaky API (X-API-Key, /v1/public) is read-only for users — no invite/kick.
This module calls the platform bridge when PLAKY_BRIDGE_URL is configured,
otherwise falls back to informative error via plaky.py.
"""
import os
from typing import Any, Dict

import requests

PLAKY_BRIDGE_URL = (os.getenv("PLAKY_BRIDGE_URL") or os.getenv("PLATFORM_PLAKY_BRIDGE_URL") or "").strip()
# Bridge is exposed via api-gateway: http://plaky-bridge:5009/plaky/... or via gateway /api/plaky
# Inside norozo, use direct service URL; on VPS, gateway proxies.
INTERNAL_SECRET = os.getenv("INTERNAL_SERVICE_SECRET") or os.getenv("PLAKY_BRIDGE_SECRET") or ""


def _bridge_headers() -> Dict[str, str]:
    h: Dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    if INTERNAL_SECRET:
        h["x-internal-secret"] = INTERNAL_SECRET
        h["x-api-key"] = INTERNAL_SECRET
    return h


def bridge_status() -> Dict[str, Any]:
    if not PLAKY_BRIDGE_URL:
        return {"ok": False, "configured": False, "message": "PLAKY_BRIDGE_URL not configured — bridge disabled. Set PLAKY_BRIDGE_URL=http://plaky-bridge:5009 (internal) or https://platform.deepiri.com/api/plaky (prod)."}
    try:
        # status is public
        base = PLAKY_BRIDGE_URL.rstrip("/")
        # handle both /plaky and bare host
        url = f"{base}/status" if not base.endswith("/plaky") else f"{base}/status"
        # if base is gateway url ending /api/plaky, status is /api/plaky/status -> which proxies to /plaky/status
        # also try /health
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            # try health
            r2 = requests.get(f"{base}/health", timeout=8)
            if r2.status_code == 200:
                return {"ok": True, "configured": True, "status": r2.json()}
            return {"ok": False, "status": r.status_code, "message": r.text[:500]}
        return {"ok": True, "configured": True, **r.json()}
    except Exception as e:
        return {"ok": False, "configured": True, "message": f"Bridge unreachable: {e}"}


def invite_via_bridge(email: str, role: str = "MEMBER") -> Dict[str, Any]:
    if not PLAKY_BRIDGE_URL:
        return {"ok": False, "status": 501, "message": "PLAKY_BRIDGE_URL not set — headless bridge disabled. Add PLAKY_EMAIL/PLAKY_PASSWORD to platform and set PLAKY_BRIDGE_URL in norozo.", "via": "none"}
    base = PLAKY_BRIDGE_URL.rstrip("/")
    url = f"{base}/invite" if base.endswith("/plaky") else f"{base}/plaky/invite"
    # normalize: if PLAKY_BRIDGE_URL is http://plaky-bridge:5009 then endpoint is /plaky/invite
    if base.endswith(":5009") and "/plaky" not in base:
        url = f"{base}/plaky/invite"
    # if base is http://plaky-bridge:5009/plaky then already handled
    try:
        r = requests.post(url, headers=_bridge_headers(), json={"email": email, "role": role}, timeout=60)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
        if r.status_code in (200, 201):
            return {"ok": True, "status": r.status_code, **data}
        return {"ok": False, "status": r.status_code, "message": data.get("error") or data.get("message") or r.text[:500], **data}
    except Exception as e:
        return {"ok": False, "status": 500, "message": f"Bridge invite failed: {e}"}


def kick_via_bridge(email: str) -> Dict[str, Any]:
    if not PLAKY_BRIDGE_URL:
        return {"ok": False, "status": 501, "message": "PLAKY_BRIDGE_URL not set — headless bridge disabled.", "via": "none"}
    base = PLAKY_BRIDGE_URL.rstrip("/")
    url = f"{base}/kick" if base.endswith("/plaky") else f"{base}/plaky/kick"
    if base.endswith(":5009") and "/plaky" not in base:
        url = f"{base}/plaky/kick"
    try:
        r = requests.post(url, headers=_bridge_headers(), json={"email": email}, timeout=60)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
        if r.status_code in (200, 201):
            return {"ok": True, "status": r.status_code, **data}
        return {"ok": False, "status": r.status_code, "message": data.get("error") or data.get("message") or r.text[:500], **data}
    except Exception as e:
        return {"ok": False, "status": 500, "message": f"Bridge kick failed: {e}"}
