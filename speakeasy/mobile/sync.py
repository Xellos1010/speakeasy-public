"""Peer sync for the portable profile: `speakeasy profile sync --peer <tailnet-host>`.

Pull the peer's export, merge locally (union by event id), then push the merged local export
back to the peer's import. Both sides converge to the same event set, version and digest.
Peer tokens live in ``~/.speakeasy/mobile/peers.json`` (0600) and are never printed.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from pathlib import Path
from typing import Optional

from speakeasy.mobile.auth import default_mobile_dir
from speakeasy.mobile.fsutil import atomic_write_json, read_json


def peers_path(root: Optional[Path] = None) -> Path:
    return Path(root or default_mobile_dir()) / "peers.json"


def default_peer_url(peer: str) -> str:
    if peer.startswith("http://") or peer.startswith("https://"):
        return peer.rstrip("/")
    return f"http://{peer}:8812"


def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
        try:
            ctx.load_verify_locations("/etc/ssl/cert.pem")
        except OSError:
            pass
        return ctx


def _request(method: str, url: str, token: Optional[str] = None, body=None, timeout=60):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    kw = {"context": _ssl_ctx()} if url.startswith("https://") else {}
    with urllib.request.urlopen(req, timeout=timeout, **kw) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pair_peer(peer: str, code: str, device_name: str, url: Optional[str] = None,
              root: Optional[Path] = None, request=_request) -> dict:
    base = (url or default_peer_url(peer)).rstrip("/")
    res = request("POST", base + "/v1/pair", body={"code": code, "device_name": device_name})
    peers = read_json(peers_path(root), {})
    peers[peer] = {"base_url": base, "device_id": res["device_id"], "token": res["token"]}
    atomic_write_json(peers_path(root), peers)
    return {"peer": peer, "base_url": base, "device_id": res["device_id"]}


def sync(profile, peer: str, url: Optional[str] = None, root: Optional[Path] = None,
         push: bool = True, request=_request) -> dict:
    peers = read_json(peers_path(root), {})
    rec = peers.get(peer)
    if not rec:
        raise SystemExit(f"no token for peer {peer!r}: on {peer} run `speakeasy pair --device "
                         f"<this-host>`, then here `speakeasy profile pair-peer --peer {peer} --code <code>`")
    base = (url or rec["base_url"]).rstrip("/")
    token = rec["token"]
    remote = request("GET", base + "/v1/profile/export", token=token)
    pulled = profile.import_bundle(remote)
    pushed = None
    if push:
        pushed = request("POST", base + "/v1/profile/import", token=token,
                         body=profile.export_bundle(), timeout=120)
    return {"peer": peer, "pulled_added": pulled["added"],
            "pushed_added": (pushed or {}).get("added"),
            "local_version": profile.version, "local_digest": profile.derived()["digest"],
            "peer_version": (pushed or {}).get("profile_version")}
