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

1. Fork the repo.
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
   - Any backward-incompatibility (schema migration bump? config key change?

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
