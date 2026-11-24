#!/usr/bin/env python3
"""Tests for the user accounts: the Admin Users page.

The accounts live in the DATABASE, not the config file, because the
config is mounted READ-ONLY in production: a user added through the
Admin Config page could not be saved at all - the write failed and the
change was lost on the next restart.

Credentials are operational state. They change as staff come and go, and
they must be changeable without editing a YAML file by hand.

    ./.venv/bin/python tests/test_user_management.py
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


def fn_body(name):
    i = src.find(f"def {name}(")
    if i < 0:
        return ""
    j = src.find("\ndef ", i + 10)
    return src[i:j if j > 0 else i + 6000]


print("=== the user accounts ===")
print()

# --- 1. The accounts live in the database.
print("-- where the accounts live --")
check("there is a users table",
      "CREATE TABLE IF NOT EXISTS users" in src)
check("the table has the columns the app needs",
      re.search(r"CREATE TABLE IF NOT EXISTS users.*?username.*?role.*?"
                r"password_hash.*?salt.*?disabled", src, re.S) is not None)
check("the schema version is bumped",
      re.search(r"schema_version','12'", src) is not None)
check("the config's users are the SEED",
      re.search(r'for _name, _u in \(config\.get\("auth"', src) is not None)

# --- 2. The login check reads the LIVE list.
print()
print("-- the login check --")
cc = fn_body("_check_credentials")
check("the login check reads the account list, not the config",
      "_all_accounts()" in cc
      and 'config.get("auth"' not in cc)
check("a disabled account cannot log in",
      "disabled" in cc)
check("the comparison is constant-time",
      "secrets.compare_digest" in cc)
check("an account with no password cannot log in",
      'not u.get("hash")' in cc)

# --- 3. The Users page.
print()
print("-- the Admin Users page --")
check("there is a Users route",
      '@app.route("/admin/users"' in src)
check("the page is admin-only",
      re.search(r'@app\.route\("/admin/users".*?def admin_users\(\):'
                r'.*?session\.get\("role"\) != "admin"', src, re.S) is not None)
check("it requires a login",
      re.search(r'@app\.route\("/admin/users"[\s\S]{0,120}@login_required', src)
      is not None)
check("the page is in the nav",
      '("users", "Users", "admin_users")' in src)
for action in ("add", "password", "role", "disable", "enable", "delete"):
    check(f"the page can {action}",
          f'value="{action}"' in src or f'action == "{action}"' in src)

# --- 4. The guards.
print()
print("-- the guards --")
check("the password must be at least 8 characters",
      "A password must be at least 8 characters." in src)
check("the username is restricted",
      "A username may only contain letters, digits" in src)
check("the role must be known",
      "Unknown role" in src and "USER_ROLES" in src)
check("the LAST admin cannot be deleted",
      "This is the last enabled admin account." in src)
check("the last admin cannot be demoted",
      re.search(r'action == "role".*?last enabled admin', src, re.S) is not None
      or re.search(r'is_last_admin', src) is not None)

# --- 5. The audit trail.
print()
print("-- the audit trail --")
for ev in ("USER_ADDED", "USER_PASSWORD_CHANGED", "USER_ROLE_CHANGED",
           "USER_DISABLED", "USER_ENABLED", "USER_DELETED"):
    check(f"{ev} is audited", ev in src)

# --- 6. The password is never stored in the clear.
print()
print("-- the password --")
check("the password is hashed before it is stored",
      "_hash_password(password, salt)" in src)
# The property that matters: the INSERT binds the HASH, not the
# plaintext. Rather than pattern-match the SQL across its split string
# literals, check the ACTUAL bound value: the third value in the INSERT
# tuple must be the hash expression.
_ins = re.search(r'INSERT INTO users.*?username, role or "operator",(.*?)salt, now',
                 src, re.S)
check("the plaintext is never written to the database",
      _ins is not None
      and "_hash_password(password, salt) if password else None" in _ins.group(1))

# The UPDATE must bind the hash too.
_upd = re.search(r'sets\.append\("password_hash = \?"\)(.*?)args\.append',
                 src, re.S)
check("a password change stores the new hash",
      _upd is not None
      and re.search(r'args\.append\(_hash_password\(password, salt\)\)', src)
      is not None)

check("the placeholder check handles both shapes",
      "_is_placeholder_password" in src
      and "u.get(\"salt\")" in fn_body("_is_placeholder_password"))

# ---------------------------------------------------------------------------
# 8. The behavioural proof: the plaintext is nowhere in the database file.
#
#    A pattern match on the SQL cannot prove this. Reading the actual
#    database file can: the plaintext must not appear anywhere in it,
#    in any table, including the WAL.
# ---------------------------------------------------------------------------
print()
print("-- the plaintext is nowhere in the database --")
try:
    import tempfile
    import sqlite3

    sys.path.insert(0, REPO)
    import app as _app

    tmpdir = tempfile.mkdtemp()
    tmpdb = os.path.join(tmpdir, "users_test.db")
    _app.DB_FILE = tmpdb
    _app.config.setdefault("auth", {})["users"] = {}
    _app.init_db()

    SECRET = "correct-horse-battery-staple"
    _app._save_user("testuser", password=SECRET, role="operator")

    conn = sqlite3.connect(tmpdb)
    row = conn.execute(
        "SELECT password_hash, salt FROM users WHERE username = 'testuser'"
    ).fetchone()
    conn.close()

    check("the account was created", row is not None)
    check("the stored value is a hash, not the password",
          row is not None and row[0] != SECRET and len(row[0]) == 64)
    check("the hash verifies the password",
          row is not None
          and _app._hash_password(SECRET, row[1]) == row[0])

    # The plaintext must not appear in the database file, or in the WAL.
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        try:
            with open(tmpdb + suffix, "rb") as f:
                blob += f.read()
        except OSError:
            pass
    check("the plaintext does not appear in the database file",
          SECRET.encode() not in blob)

    # The login check must accept it.
    ok, role = _app._check_credentials("testuser", SECRET)
    check("the new account can log in", ok and role == "operator")
    ok2, _ = _app._check_credentials("testuser", "wrong-password")
    check("a wrong password is refused", not ok2)

    # And a disabled account cannot.
    _app._save_user("testuser", disabled=True)
    ok3, _ = _app._check_credentials("testuser", SECRET)
    check("a disabled account cannot log in", not ok3)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
except Exception as e:
    check("the behavioural test ran", False, f"{type(e).__name__}: {e}")

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
