# Factory Droid Langfuse plugin

This local user-scoped plugin adapts Langfuse's official Claude Code Stop-hook
approach to Factory Droid's compatible plugin hooks. The Stop hook only
launches a detached exporter and returns immediately; Langfuse network work
never blocks an agent turn.

Each completed turn emits a root `Factory Droid - Turn N` span, generation spans,
and nested tool spans. Root trace metadata and tags are normalized for comparison
with other harnesses:

- `harness:factory`
- `model:<model>(<reasoning>)`
- `reasoning-effort:<reasoning>`

The hook reads the actual `modelId` and `reasoningEffort` from Factory's session
transcript. Custom model IDs are resolved through `~/.factory/settings.json`.

## Configuration and secrets

Enable or disable tracing in `~/.factory/langfuse.json`:

```json
{"enabled": true, "max_chars": 20000}
```

Credentials are deliberately not stored in this plugin or configuration file. The
hook reads `public-key`, `secret-key`, and `base-url` from macOS Keychain service
`amp-langfuse`, which is also used by the Amp integration.

The Python SDK is isolated in `~/.factory/langfuse-venv` and pinned to Langfuse
4.x, matching the official Claude Code integration.

Exporter state is serialized across concurrent sessions and written atomically.
If Langfuse is unavailable, the exporter logs the failure and leaves the
transcript offset unchanged so a later Stop hook can retry.
