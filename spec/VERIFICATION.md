# End-to-End Verification

Date: 2026-10-21
Repository: `/Users/yo_macbook/Documents/dev/agent-langfuse-connector`

All Langfuse totals below were read through
`tools/verify_agent_session.py` and the v2 observations API.

| Adapter | Session | Native turn totals | Langfuse v2 total | Traces | Tools |
| --- | --- | ---: | ---: | ---: | --- |
| Codex | `01a15b70-396d-71a3-8f35-95113d934337` | 37,078 / 37,336 | 74,414 | 2 | 1 per turn |
| Devin | `glory-taurus` | 31,609 / 31,993 | 63,602 | 2 | 0 (hook has no tool export) |
| Pi | `01a15b51-0000-7000-8000-000000000001` | 10,688 / 10,826 | 21,514 | 2 | 1 per turn |
| OMP | `01a15b68-8c05-7553-b185-8fd1b30d9e6d` | 17,352 / 17,539 | 34,891 | 2 | 1 per turn |
| Amp | `T-01a15b6b-fd08-73dd-977e-828b1da526bb` | 32,458 | 32,458 | 1 | 1 |
| Claude Code | `68075f73-203a-41e8-af99-2a1fb6ee003f` | 65,660 / 68,371 | 134,031 | 2 | 1 per turn |
| Droid | `ea65fc66-8dd2-42b0-afdf-36702965e1f8` | 22,842 / 37,016 | 59,858 | 2 | 1 per turn |

Droid's two-turn native values come from the two `droid exec` result payloads.
Its session settings only retain the most recent `tokenUsage`, so the v2
verifier was run with `--skip-native` for that session.

## Connector checks

```text
python3 tools/connector.py doctor
PASS claude hook
PASS droid marketplace and cache copies
PASS devin plugin registered
PASS devin manifest
PASS amp plugin
PASS codex dist, hooks, and manifest
PASS pi package
PASS omp staged package
PASS no legacy trace, session, or v1 observation endpoints in packages
PASS v4 ingestion headers present
0 failure(s)
```

`make check` passes:

- Python compile for packages, tools, and tests
- Bun builds for Pi, OMP, and Amp
- syntax check for the Codex bundle
- 7 connector unit tests

## Notes

- Codex emitted an empty `model:unknown` trace when a resume attempt failed
  before the fix. The source now skips any turn with zero model steps, and the
  rebuilt bundle produced exactly two traces for the successful two-turn test.
- Amp execute mode produced a clean single-turn trace. Its older noninteractive
  continuation thread remains the only known Amp lifecycle edge; normal
  two-turn threads have produced two exact traces.
- Devin was previously installed as a broken plugin symlink pointing at the
  old repository path. The connector now installs the native Devin plugin from
  the new repository and removes the fallback Stop hook from
  `~/.config/devin/config.json`.
- OMP must be staged under `~/.omp/plugins` and linked from its node_modules
  tree. Linking directly to the repository breaks Node module resolution for
  the host's OTel dependencies.
