# Connector Repository Instructions

- Keep all adapter sources in this repository. Do not edit files in agent caches
  directly; edit here and run `python3 tools/connector.py install --all`.
- Preserve the one-trace-per-user-turn contract and the exact token accounting
  documented in `spec/`.
- Use OTel v4 ingestion and v2 observations reads. Do not add legacy trace,
  session, or v1 observation calls.
- Keep credentials out of source, tests, fixtures, and logs.
- Back up existing host files before replacing them. Never overwrite a
  user-owned README or unrelated host configuration.
- Run `make check` and `python3 tools/connector.py doctor` after changes.
- Live verification uses `tools/verify_agent_session.py` and is opt-in because
  it calls the configured Langfuse project.
