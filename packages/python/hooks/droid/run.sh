#!/bin/sh
exec "$HOME/.factory/langfuse-venv/bin/python" "$(dirname "$0")/langfuse_hook.py"
