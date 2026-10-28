#!/usr/bin/env python3
"""Set a password in a config file, as the salted hash the app expects.

The app upgrades a plaintext password to a salted SHA-256 hash on its
first start, by rewriting the config file. In production the config is
mounted READ-ONLY - a compromised process must not be able to rewrite the
MQTT password or the session key - so the upgrade cannot happen at
runtime.

This does the upgrade ahead of time, so the config on disk is already
in its final form and the app has nothing to write.

    ./tools/set_password.py config/config.yaml yaniv <password>
    ./tools/set_password.py config/config.yaml operator <password>

The password may be given as an argument or read from stdin, so it need
not appear in the shell history:

    ./tools/set_password.py config/config.yaml yaniv < password.txt
"""
import hashlib
import os
import secrets
import sys

import yaml


def hash_password(password, salt):
    """The same hash the app uses: sha256(salt + ':' + password)."""
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    path = sys.argv[1]
    username = sys.argv[2]

    if len(sys.argv) > 3:
        password = sys.argv[3]
    else:
        password = sys.stdin.read().strip()
    if not password:
        sys.exit("no password given")

    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    users = cfg.setdefault("auth", {}).setdefault("users", {})
    user = users.setdefault(username, {"role": "operator"})
    salt = secrets.token_hex(8)
    user["password_hash"] = {"salt": salt, "hash": hash_password(password, salt)}
    # A plaintext password must not survive: the file is mounted into the
    # container and is readable by anyone with shell access.
    user.pop("password", None)

    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)

    print(f"  set the password for {username!r} in {path}")
    print(f"  salt {salt}  (the plaintext is not stored)")


if __name__ == "__main__":
    main()
