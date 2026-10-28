#!/usr/bin/env python3
"""Verify every admin route is actually registered, and that the
target-range editor works.

A route whose decorator was lost is invisible until a page 500s. That
happened here: a helper inserted above the admin_ignored function
consumed its @app.route decorator, so the nav's url_for('admin_ignored')
raised BuildError and EVERY page that renders the nav failed - not just
the broken one.

    ./.venv/bin/python tests/test_admin_routes.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS - {name}")
    else:
        FAIL += 1
        print(f"FAIL - {name}" + (f"  [{detail}]" if detail else ""))


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(REPO, "app.py")).read()

print("=== admin routes ===")
print()

# --- 1. Every def that the nav or a template links to must have a route.
print("-- the navigation endpoints --")
nav_start = src.find("tabs = [")
nav = src[nav_start:nav_start + 500]
endpoints = re.findall(r'\("(\w+)",\s*"[^"]*",\s*"(\w+)"\)', nav)
for tid, ep in endpoints:
    check(f"the nav endpoint {ep!r} has a route",
          f"url_for('{ep}')" in src or f"def {ep}(" in src)

# --- 2. Every function referenced by url_for must be a registered route.
print()
print("-- every url_for target is a route --")
# Collect the endpoint names of the registered routes.
route_funcs = set()
for m in re.finditer(r'@app\.route\([^)]*\)\s*\n(?:@[^\n]*\n)*def\s+(\w+)\(', src):
    route_funcs.add(m.group(1))
for m in re.finditer(r'url_for\(["\'](\w+)["\']', src):
    ep = m.group(1)
    check(f"url_for({ep!r}) resolves",
          ep in route_funcs or ep in ("index", "login", "logout"),
          f"{ep} is not a registered route")

# --- 2b. Every route must point at a FUNCTION THAT IS ABOUT THAT ROUTE.
#
#      A helper inserted between a route decorator and the function it
#      belonged to silently re-routes the endpoint: /snooze ended up
#      calling _reevaluate_device, and /admin/ignored ended up calling
#      _thresholds_by_device. Both 500'd, and the first took the whole
#      navigation with it because every page renders the nav.
#
#      So: the function a route names must not be a helper (an underscore
#      prefix means it is internal), and a route must not have two
#      functions attached to it.
print()
print("-- every route points at the right function --")
route_map = {}
for m in re.finditer(r'@app\.route\("([^"]+)"[^\n]*\n(?:@[^\n]*\n)*def (\w+)\(', src):
    route_map.setdefault(m.group(1), []).append(m.group(2))
for route, fns in sorted(route_map.items()):
    # GET and POST on the same path are legitimate (admin_config).
    if len(fns) > 1:
        check(f"{route} has one function per method",
              sorted(fns) == ["admin_config", "admin_config_post"],
              str(fns))
    for fn in fns:
        check(f"{route} -> {fn} is a real view, not a helper",
              not fn.startswith("_"),
              f"{fn} is an internal helper")

#      A STACKED duplicate decorator is the same class of error: Flask
#      raises "View function mapping is overwriting an existing endpoint"
#      at import time, so the whole app fails to start.
print()
print("-- no stacked or duplicated route decorators --")
for m in re.finditer(r'(@app\.route\("[^"]+"[^\n]*\n(?:@[^\n]*\n)*def \w+\()', src):
    block = src[m.start():m.end()]
    n = block.count('@app.route(')
    if n > 1:
        check("no stacked route decorators", False, block[:120])
        break
else:
    check("no stacked route decorators", True)

# Every FUNCTION name may back only one rule (Flask requires unique
# endpoints).
fn_map = {}
for m in re.finditer(r'@app\.route\("([^"]+)"[^\n]*\n(?:@[^\n]*\n)*def (\w+)\(', src):
    fn_map.setdefault(m.group(2), []).append(m.group(1))
for fn, rules in fn_map.items():
    if len(rules) > 1:
        check(f"{fn} backs one rule", False, str(rules))

# --- 3. The admin pages exist.
print()
print("-- the admin pages --")
for path, fn in (("/admin/ignored", "admin_ignored"),
                 ("/admin/ranges", "admin_ranges"),
                 ("/admin/degraded", "admin_degraded"),
                 ("/admin/config", "admin_config")):
    check(f"{path} is registered",
          f'@app.route("{path}"' in src and f"def {fn}(" in src)

check("the /snooze route reaches set_snooze",
      "set_snooze" in route_map.get("/snooze/<device_id>/<int:hours>", []))
check("the /unsnooze route reaches unsnooze",
      "unsnooze" in route_map.get("/unsnooze/<device_id>", []))
check("the /admin/ignored route reaches admin_ignored",
      "admin_ignored" in route_map.get("/admin/ignored", []))
check("the /admin/ranges route reaches admin_ranges",
      "admin_ranges" in route_map.get("/admin/ranges", []))

# --- 4. The target-range editor is reachable from the ignore list.
check("the ignore list links to the target ranges",
      'href="/admin/ranges"' in src)
check("the target ranges page links back",
      'href="/admin/ignored"' in src)
check("the two pages share a sub-tab strip",
      src.count('class="admin-subtabs"') >= 2)

# --- 5. The ranges editor is admin-only and audited.
print()
print("-- the ranges editor --")
i = src.find("def admin_ranges")
if i > 0:
    j = src.find("\n@app.route", i + 10)
    body = src[i:j if j > 0 else i + 6000]
    check("the ranges page requires the admin role",
          'role") != "admin"' in body)
    check("saving a range is audited",
          "RANGE_SAVED" in body)
    check("removing a range is audited",
          "RANGE_REMOVED" in body)
    check("a saved range clears the stale alert for that channel",
          "OUT_OF_RANGE" in body and "resolved = 1" in body)
    check("the channel picker is built from the channels that exist",
          "_sensor_channels" in body)

# --- 6. The wallboard respects a snooze.
print()
print("-- the wallboard respects snoozes --")
i = src.find("def api_wallboard")
if i > 0:
    j = src.find("\n@app.route", i + 10)
    body = src[i:j if j > 0 else i + 4000]
    check("the wallboard query filters on snooze_until",
          "snooze_until" in body)
    check("the wallboard joins the devices table",
          "LEFT JOIN devices" in body)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
