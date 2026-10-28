#!/usr/bin/env python3
"""Upload the generated dashboards to Grafana.

The service-account token is read from a file OUTSIDE the repo and is never
printed, logged or written anywhere. Point GRAFANA_TOKEN_FILE at it:

    GRAFANA_TOKEN_FILE=~/grafana_info.txt ./.venv/bin/python grafana/push_dashboards.py

The file just has to contain the glsa_ token somewhere; everything else in it
is ignored.
"""
import json
import os
import re
import subprocess
import sys

OUT = os.path.dirname(os.path.abspath(__file__))
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://10.10.17.11:3000")
FOLDER = "PsygaBio Watchdog"
DEFAULT_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(OUT)), "grafana_innfluxdb_info.txt")


def token():
    path = os.path.expanduser(os.environ.get("GRAFANA_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    if not os.path.exists(path):
        sys.exit(f"token file not found: {path}  (set GRAFANA_TOKEN_FILE)")
    m = re.search(r"glsa_[A-Za-z0-9_]+", open(path).read())
    if not m:
        sys.exit(f"no glsa_ token found in {path}")
    return m.group(0)


def api(tok, method, path, body=None):
    cmd = ["curl", "-s", "-m", "30", "-X", method,
           "-H", f"Authorization: Bearer {tok}",
           "-H", "Content-Type: application/json",
           f"{GRAFANA_URL}{path}"]
    if body is not None:
        cmd += ["--data-binary", json.dumps(body)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"_raw": p.stdout[:300]}


def ensure_folder(tok):
    for f in api(tok, "GET", "/api/folders") or []:
        if isinstance(f, dict) and f.get("title") == FOLDER:
            return f["uid"]
    r = api(tok, "POST", "/api/folders", {"title": FOLDER})
    return r.get("uid")


def main():
    tok = token()
    uid = ensure_folder(tok)
    if not uid:
        sys.exit("could not create/find the Grafana folder")
    print(f"  folder '{FOLDER}' uid={uid}")
    for name in ("overview", "wallboard", "device"):
        path = os.path.join(OUT, f"dashboard-{name}.json")
        dash = json.load(open(path))
        res = api(tok, "POST", "/api/dashboards/db",
                  {"dashboard": dash, "folderUid": uid, "overwrite": True,
                   "message": "provisioned from grafana/build_dashboards.py"})
        status = res.get("status", res.get("message", res))
        print(f"  {name:<10} -> {status}   {GRAFANA_URL}{res.get('url','')}")


if __name__ == "__main__":
    main()
