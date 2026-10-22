# Contributing

Contributions are welcome. This project follows a few lightweight rules
so the codebase stays readable.

## Code of conduct

Be considerate. We don't have a long document here because the
expectation is small and the surface area is one team's facility
deployment. Disagreements happen; resolve them civilly.

## What we'd love help on

- Tests. There's no test suite. Even minimal ones for the topic parser
  and CSV uploader would catch the class of bugs we hit during
  development.
- Per-user Telegram identity binding (the audit log can use a
  `chat_id → operator_name` map).
- WebSocket / SSE updates for the dashboard.
- TLS termination story (we currently assume an upstream reverse proxy).
- Documentation improvements — anything that confused you while reading
  is fair game.

## Pull request workflow

1. Fork the repo (or clone directly if you have write access).
2. Create a feature branch from `main`. Use a descriptive name
   (`feature/per-user-telegram-binding`, `fix/clean-up-default-rows`,
   etc.).
3. Make your changes. See [`docs/development.md`](development.md) for
   style.
4. Run `./ops.sh validate` to confirm YAML / compose / env are sane.
5. Push your branch and open a Pull Request against `main`.
6. PR description should answer:
   - What problem this solves (cite issue number if any).
   - How it was tested.
   - Any backward-incompatibility (schema migration bump? config key change?)

## Repository access

This repository uses a **write-enabled deploy key** rather than a personal
access token or account-level SSH key. Deploy keys are bound to exactly one
repository, which is why the key that pushes here cannot be reused on any
other repo — and why adding it will fail with *"key is already in use"* if
you try.

If you need push access:

1. Generate a keypair dedicated to this repo:

   ```bash
   ssh-keygen -t ed25519 -C "mqtt-watchdog deploy key" \
     -f ~/.ssh/id_ed25519_mqtt_watchdog -N ""
   cat ~/.ssh/id_ed25519_mqtt_watchdog.pub
   ```

2. Add it under **Settings → Deploy keys** with **Allow write access**
   ticked. Unticked, it is read-only and pushes will fail with
   `Permission denied (publickey)`.

3. Pin it to this repo via `~/.ssh/config` so it does not collide with
   your other GitHub keys:

   ```
   Host github.com-mqtt-watchdog
       HostName github.com
       User git
       IdentityFile ~/.ssh/id_ed25519_mqtt_watchdog
       IdentitiesOnly yes
   ```

4. Point the remote at that alias:

   ```bash
   git remote set-url origin \
     git@github.com-mqtt-watchdog:lerenthal/MQTT-sensor-watchdog-platform.git
   ```

**A subtlety worth knowing:** if the remote reads `git@github.com:...`
rather than the alias above, SSH silently falls back to your default key —
which may be a deploy key for a *different* repo. The resulting error
(`denied to deploy key`) looks like a permissions problem but is really a
routing one. Verify with `ssh -T git@github.com-mqtt-watchdog`; it should
greet you with `Hi lerenthal/MQTT-sensor-watchdog-platform!`.

Cloning over HTTPS works without any of this and is the recommended path
for read-only use.

## Releases

There's no fixed release cadence. Cut a release when there's enough
change to be worth one. See [`docs/development.md`](development.md#releases--versions)
for the tag / changelog convention.

## Security

If you find a security issue, please **don't** open a public issue. Send
a private note to the maintainer listed in GitHub. The repo's `SECURITY.md`
(in the default branch's `docs/`) outlines the supported versions.

## License

By contributing, you agree that your contributions will be licensed
under the project's GPL-3.0 license. See [LICENSE](../LICENSE).
