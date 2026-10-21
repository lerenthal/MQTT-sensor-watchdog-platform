# Pushing to GitHub

This repo was prepared locally without a configured remote. To publish
to https://github.com/lerenthal/MQTT-sensor-watchdog-platform, follow
one of the two flows below.

## Pre-flight

```bash
cd /opt/sensor-watchdog          # or wherever you cloned

# Sanity check: nothing sensitive inside
git log --stat -1
git grep -E "Dont4Get|token.*[0-9]{20,}|secret_key.*[^_]\\w{30,}" || \
  echo "no real secrets in tracked files"
```

If you do find real secrets, `git filter-repo` or `git filter-branch` can
rewrite history before push.

## Option A — Overwrite the existing remote `main` (destructive)

The remote at https://github.com/lerenthal/MQTT-sensor-watchdog-platform
already has one commit (the README-only initial). Use this option only
if that's fine to lose.

```bash
git remote add origin https://github.com/lerenthal/MQTT-sensor-watchdog-platform.git

# Verify you can authenticate
git ls-remote origin

# Force push your local main over the existing main
git push -f origin main
```

After push:

1. Visit https://github.com/lerenthal/MQTT-sensor-watchdog-platform
2. **Settings → Branches** → set `main` as the default branch if it isn't
3. **Settings → Security** → enable vulnerability alerts
4. **Settings → Secrets and variables → Actions** → set up any CI later
5. **Settings → Pages** → if you want docs on GitHub Pages

## Option B — Open a Pull Request (safer, recommended)

This keeps the existing remote `main` intact until you merge.

```bash
git remote add origin https://github.com/lerenthal/MQTT-sensor-watchdog-platform.git

# Verify
git ls-remote origin

# Push to a feature branch
git push -u origin main:snapshot-2026-10-21
```

Then in the GitHub UI:

1. Compare & pull request against `main`.
2. Use a title like `Draft: full sensor-watchdog v2026-10-21`.
3. Self-review the diff on the PR page.
4. Once comfortable, merge.

If the PR introduces no conflicts (it does — the README-only commit and
your current commit both introduce files), resolve by accepting the
local commit's version of each conflicting file in the GitHub UI.

## Option C — Add as a subtree

If the existing repo's README is intentional and you want to keep it,
add this repo's docs alongside:

```bash
# NOT recommended for this project. Use Option B.
```

## Authentication

If you have two-factor auth enabled, use a personal access token (PAT)
in place of a password:

- Classic: <https://github.com/settings/tokens> → Generate new token
  (classic) → scopes: `repo`, `workflow` (only if you later add CI).
- Fine-grained: same URL → Generate new token → repository access: this
  one; permissions: contents (read & write), pull requests (read & write).

On first push, Git will ask:

```
Username for 'https://github.com': lerenthal
Password for 'https://github.com@…': <paste PAT>
```

The PAT is stored in your OS keychain if you use a credential helper:

```bash
git config --global credential.helper store         # plaintext file (DEV ONLY)
git config --global credential.helper 'cache --timeout=3600'
git config --global credential.helper 'osxkeychain'  # macOS
git config --global credential.helper '/usr/bin/seahorse'   # GNOME (Linux)
```

For SSH (recommended for long-term use):

```bash
ssh-keygen -t ed25519 -C "your-email@example.com"
cat ~/.ssh/id_ed25519.pub     # paste into https://github.com/settings/keys

git remote set-url origin git@github.com:lerenthal/MQTT-sensor-watchdog-platform.git
git push -f origin main
```

## Post-push checklist

- [ ] Repository description is set (`🍄 MQTT Sensor Watchdog — independent watchdog + Telegram nagging for ESPHome sensors`)
- [ ] Homepage URL set (your live deployment, if any)
- [ ] Topics added: `mqtt`, `flask`, `iot`, `sensor-monitoring`, `telegram-bot`, `watchdog`
- [ ] About panel includes the GPL-3.0 license notice
- [ ] `Releases` → create a tag like `2026-10-21` from the initial commit, with the contents of `CHANGELOG.md` as the release notes
