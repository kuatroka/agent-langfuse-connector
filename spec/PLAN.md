# Connector Consolidation Plan

Status: implemented and verified in this repository
Date: 2026-10-21
Destination: `/Users/yo_macbook/Documents/dev/agent-langfuse-connector`

## Goal

Make this repository the source of truth for every local coding-agent Langfuse
connector, and make updates reproducible with one install command instead of
hand-editing agent cache directories.

The repository does not try to make every host use the same runtime. Claude,
Droid, and Devin are Python; Amp, Codex, Pi, and OMP are TypeScript. The shared
contract is semantic, not a shared executable.

## Invariants

Every adapter must preserve these rules:

1. One completed user prompt produces one Langfuse trace.
2. Trace names are `<Display> - Turn N`; `turn_number` is an integer.
3. Token usage is attached once per turn, on a generation observation, and
   equals the host's native turn total.
4. Session totals equal the sum of turn traces.
5. Every model or tool observation keeps the trace's session and turn context.
6. Tags include `harness:<name>`, model, and reasoning effort when available.
7. No trace observation uses `/api/public/traces`, `/api/public/sessions`, or
   v1 `/api/public/observations`.
8. Writes use OTel; credentials are read from the host's existing secret store.

The full metadata contract is in `spec/CONTRACT.md`. Token-specific decisions
are in `spec/TOKEN-ACCOUNTING-SPEC.md`.

## Repository layout

```text
spec/                         contracts and migration notes
packages/python/              shared Python primitives and hooks
packages/typescript/amp/      Amp single-file plugin
packages/typescript/codex/    Codex plugin source plus built dist
packages/typescript/pi/       Pi extension
packages/typescript/omp/      OMP extension
tools/connector.py            plan, install, doctor, verify
tools/langfuse_v2.py          current read client
tools/verify_agent_session.py native-vs-v2 comparison
tests/                        static and installer checks
```

## Installation contract

`tools/connector.py` is the only supported way to update active host files.

Targets:

| Adapter | Active target |
| --- | --- |
| Claude | `~/.claude/hooks/langfuse_hook.py` |
| Droid | local marketplace plus matching plugin cache roots |
| Devin | native `langfuse-unified` plugin installed from `packages/python/hooks/devin` |
| Amp | user plugin repository `langfuse.ts` |
| Codex | active `tracing-normalized` plugin cache |
| Pi | `~/.pi/agent/npm/node_modules/pi-langfuse` |
| OMP | `~/.omp/plugins/node_modules/pi-langfuse` (symlink to staged copy) |

Rules:

- `plan` is read-only.
- `install --dry-run` is read-only.
- `install` backs up files it replaces under
  `~/.local/share/agent-langfuse-connector/backups/`.
- `doctor` checks installed content, source invariants, and the absence of
  legacy endpoints.
- `verify` delegates to `tools/verify_agent_session.py`.
- Devin uses its native plugin manager when available. The installer removes
  only the legacy connector-generated `Stop` hook, never unrelated hooks.
- The installer never edits the Amp `README.md`, never stores credentials, and
  never deletes unrelated files.

## Delivery phases

### Phase 1: freeze the working sources

Copy the verified implementations from their active locations into packages.
This preserves the code that passed the v2 tests before any refactor.

Acceptance: every adapter has a source file in this repository.

### Phase 2: central installer

Add `tools/connector.py` with plan, install, dry-run, doctor, and verify.

Acceptance: `connector.py doctor` can distinguish an installed adapter from a
missing or stale one, and installation is reversible from backups.

### Phase 3: build and install

Build the TypeScript artifacts that require a bundle, then install all seven
adapters. OMP is staged under `~/.omp/plugins/agent-langfuse-connector/omp` and
linked from the OMP node_modules tree, so OMP keeps its linked-plugin loader and
Node resolves the host's existing dependency tree. Pi is copied into its
existing package directory for the same dependency reason. `doctor` compares
the installed `src/langfuse.ts` byte-for-byte with the repository source.

Acceptance: all active files match their repository sources.

### Phase 4: conformance and live verification

Run static checks and the v2 verifier. For each harness, use a two-turn session
with at least one tool call per turn when the host exposes tools.

Acceptance:

- two traces for two prompts,
- `Turn 1` and `Turn 2`,
- integer `turn_number`,
- exact per-turn native usage,
- exact session total,
- tool spans where supported,
- no legacy read endpoints.

### Phase 5: documentation

Record the supported host versions, installation command, backup location, and
known limitations in the README and migration note.

## Test matrix

| Adapter | Native source | Expected trace |
| --- | --- | --- |
| Claude | JSONL transcript | `Claude Code - Turn N` |
| Droid | session settings and transcript | `Factory Droid - Turn N` |
| Devin | Devin sessions database | `Devin - Turn N` |
| Amp | `amp threads export` | `Amp - Turn N` |
| Codex | rollout JSONL plus sidecar | `Codex - Turn N` |
| Pi | Pi session JSONL | `Pi - Turn N` |
| OMP | OMP session JSONL | `OMP - Turn N` |

## Known limits

- Langfuse keeps `score-create` events on `/api/public/ingestion` after the v4
  cutover. Pi/OMP retain that path for scores only; it is not trace ingestion.
- Amp's older `threads continue ... -x` replay path produced only one trace in
  one historical test. Normal two-turn Amp threads pass; recheck the
  noninteractive continuation path before claiming parity.
- Codex historical duplicate traces from before its sidecar fix are not
  rewritten.
- Droid's default hosted model returned HTTP 402 for this account. The custom
  SWE-2-MAX model is the verified live path.
- Devin's hook has no tool export, so its traces have zero tool observations.

## Acceptance for this delivery

```sh
python3 tools/connector.py install --all
python3 tools/connector.py doctor
make check
```

Then run one live two-turn session per adapter and verify it with the v2 tools.
The completed results are recorded in `spec/VERIFICATION.md`; `doctor` reports
zero failures and `make check` passes.
