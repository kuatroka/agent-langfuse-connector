# Unified coding-agent Langfuse contract

Every completed user turn uses one Langfuse trace and one native session ID.

Required trace tags:

- `harness:<harness>`
- `model:<model>` where `<model>` includes `(effort)` when effort is known
- `reasoning-effort:<effort>`

Required trace metadata:

- `harness`
- `model`
- `model_base`
- `model_source`
- `reasoning_effort`
- `reasoning_effort_source`

Each model request should be a `GENERATION` observation whose `model` is the base model and whose metadata repeats `reasoning_effort` and `reasoning_effort_source`.

`unknown` is an explicit value when the harness does not expose a field. Inferred values must use an `*_source` value beginning with `inferred-`; they must not be presented as runtime evidence.
