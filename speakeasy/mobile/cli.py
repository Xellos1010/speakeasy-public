"""`speakeasy` command line: serve | pair | devices | profile {show,export,import,sync,pair-peer,operator-overlay}."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Placeholder shown when nothing is configured; set server.public_url in your deployment profile
# (docs/PROFILE.md), or SPEAKEASY_MOBILE_BASE_URL, or let `tailscale status` supply the host.
DEFAULT_PUBLIC_URL = "https://speakeasy.example.invalid:8445"


def public_base_url() -> str:
    env = os.environ.get("SPEAKEASY_MOBILE_BASE_URL")
    if env:
        return env.rstrip("/")
    from speakeasy import deploy_profile
    configured = deploy_profile.load().get("server.public_url")
    if configured:
        return str(configured).rstrip("/")
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                             timeout=5, check=True).stdout
        dns = (json.loads(out).get("Self") or {}).get("DNSName", "").rstrip(".")
        if dns:
            return f"https://{dns}:8445"
    except Exception:
        pass
    return DEFAULT_PUBLIC_URL


def _cmd_serve(a):
    from speakeasy.mobile.server import serve
    serve(port=a.port, tailnet=not a.no_tailnet)


def _cmd_pair(a):
    from speakeasy.mobile.auth import DeviceRegistry
    reg = DeviceRegistry()
    res = reg.new_pairing_code(a.device, ttl_s=a.ttl)
    url = public_base_url()
    payload = json.dumps({"v": 1, "type": "speakeasy-pair", "url": url, "code": res["code"],
                          "device": a.device}, separators=(",", ":"))
    mins = max(1, round(a.ttl / 60))
    lines = [
        f"SpeakEasy pairing code for '{a.device}': {res['code']}  (one-time, expires in {mins} min)",
        f"Server: {url}",
        f"QR payload: {payload}",
    ]
    qr = None
    if shutil.which("qrencode"):
        try:
            qr = subprocess.run(["qrencode", "-t", "ANSIUTF8", payload], capture_output=True,
                                text=True, timeout=5).stdout
        except Exception:
            qr = None
    text = "\n".join(lines) + ("\n" + qr if qr else "") + "\n"
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("# SpeakEasy pairing (one-time code, not a credential)\n\n"
                    "Open the SpeakEasy Android app -> Pair, enter the server URL and the code "
                    "(or scan the QR payload). Make a new one with "
                    f"`speakeasy pair --device {a.device}`.\n\n```\n" + text + "```\n")
        os.chmod(out, 0o600)
        print(f"pairing code written to {out} (0600)")
    else:
        sys.stdout.write(text)


def _cmd_devices(a):
    from speakeasy.mobile.auth import DeviceRegistry
    reg = DeviceRegistry()
    if a.revoke:
        print("revoked" if reg.revoke(a.revoke) else "no such device")
        return
    print(json.dumps(reg.list_devices(), indent=2))


def _profile():
    from speakeasy.mobile.portable_profile import PortableProfile
    return PortableProfile()


def _cmd_profile(a):
    p = _profile()
    if a.pcmd == "show":
        print(json.dumps(p.summary(), indent=2))
    elif a.pcmd == "export":
        bundle = json.dumps(p.export_bundle(), indent=2)
        if a.out:
            fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(bundle)
            print(f"exported {p.version} events to {a.out}")
        else:
            print(bundle)
    elif a.pcmd == "import":
        print(json.dumps(p.import_bundle(json.loads(Path(a.file).read_text()))))
    elif a.pcmd == "pair-peer":
        from speakeasy.mobile.sync import pair_peer
        import socket
        print(json.dumps(pair_peer(a.peer, a.code, a.name or socket.gethostname(), url=a.url)))
    elif a.pcmd == "sync":
        from speakeasy.mobile.sync import sync
        print(json.dumps(sync(p, a.peer, url=a.url, push=not a.pull_only), indent=2))
    elif a.pcmd == "operator-overlay":
        from speakeasy.mobile.operator_overlay import write_overlay
        for path in write_overlay(p, versioned=True, beside=a.beside):
            print(path)


def _cmd_deploy_profile(a):
    """`speakeasy deployment-profile {path,check}`: where the deployment profile is read from, and
    whether it validates against speakeasy/profile.schema.json. Prints no profile contents."""
    from speakeasy import deploy_profile
    p = deploy_profile.profile_path()
    if a.dpcmd == "path":
        print(p if p else "(none: built-in defaults)")
        return
    if p is None:
        print("no deployment profile found; using built-in defaults")
        return
    try:
        deploy_profile.load(strict=True)
    except deploy_profile.ProfileError as e:
        print(f"INVALID {p}: {e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"OK {p}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="speakeasy")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the mobile API (127.0.0.1 + tailnet IP)")
    s.add_argument("--port", type=int, default=8812)
    s.add_argument("--no-tailnet", action="store_true", help="bind 127.0.0.1 only")
    s.set_defaults(fn=_cmd_serve)

    s = sub.add_parser("pair", help="issue a one-time 6-digit pairing code")
    s.add_argument("--device", required=True)
    s.add_argument("--ttl", type=int, default=600, help="seconds (default 600)")
    s.add_argument("--out", help="write the code to this file (0600) instead of stdout")
    s.set_defaults(fn=_cmd_pair)

    s = sub.add_parser("devices", help="list paired devices")
    s.add_argument("--revoke", metavar="DEVICE_ID")
    s.set_defaults(fn=_cmd_devices)

    s = sub.add_parser("deployment-profile", help="locate / validate the per-deployment profile (docs/PROFILE.md)")
    s.add_argument("dpcmd", choices=["path", "check"])
    s.set_defaults(fn=_cmd_deploy_profile)

    s = sub.add_parser("profile", help="portable operator profile")
    psub = s.add_subparsers(dest="pcmd", required=True)
    psub.add_parser("show")
    e = psub.add_parser("export"); e.add_argument("--out")
    i = psub.add_parser("import"); i.add_argument("file")
    pp = psub.add_parser("pair-peer"); pp.add_argument("--peer", required=True)
    pp.add_argument("--code", required=True); pp.add_argument("--url"); pp.add_argument("--name")
    sy = psub.add_parser("sync"); sy.add_argument("--peer", required=True); sy.add_argument("--url")
    sy.add_argument("--pull-only", action="store_true")
    ov = psub.add_parser("operator-overlay")
    ov.add_argument("--beside", action="store_true",
                    help="also write <id>.speakeasy-overlay.json next to the operator profile")
    s.set_defaults(fn=_cmd_profile)

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
