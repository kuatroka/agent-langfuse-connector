# agent-langfuse-connector

Source of truth for the local Langfuse connectors used by Claude Code, Factory Droid,
Devin, Amp, Codex, Pi, and OMP.

The repository keeps the host-specific adapters in one place, with shared
semantics documented in `spec/` and enforced by the v2 verifier and static
checks. `tools/connector.py` installs the sources into each host's active
location, takes a backup, and records the installation state.

## Quick start

```sh
python3 tools/connector.py plan
python3 tools/connector.py install --all
python3 tools/connector.py doctor
python3 tools/connector.py verify --harness pi --session-id <session-id>
```

`install` is idempotent. Use `--dry-run` to preview it, or
`--only claude droid` to update selected adapters. Backups and the install
state live under `~/.local/share/agent-langfuse-connector/`.

## Layout

```text
spec/                         contracts, migration notes, test matrix
packages/python/              Claude, Droid, Devin, shared Python primitives
packages/typescript/amp/      Amp user plugin
packages/typescript/codex/    Codex plugin source and built artifact
packages/typescript/pi/       Pi extension
packages/typescript/omp/      OMP extension (staged under ~/.omp, linked from the plugin directory)
tools/                        installer, v2 read client, session verifier
tests/                        repository and adapter conformance checks
```

## Langfuse API

- Writes: OTel `POST /api/public/otel/v1/traces`
- Header: `x-langfuse-ingestion-version: 4`
- Reads: `GET /api/public/v2/observations`
- Scores: supported `score-create` events only, if an adapter needs them

Credentials remain in the macOS Keychain service `amp-langfuse` or the host's
environment. They are not stored in this repository.

## Checks

```sh
make check
make install
make doctor
```

The active integrations are the source of truth for behavior; the connectors
are only considered installed when `doctor` passes against the repository
sources.
