from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import connector  # noqa: E402


class ConnectorSourceTests(unittest.TestCase):
    def test_required_sources_exist(self) -> None:
        required = [
            connector.CLAUDE_SOURCE / "langfuse_hook.py",
            connector.DROID_SOURCE / "langfuse_hook.py",
            connector.DEVIN_SOURCE / "export_devin.py",
            connector.COMMON_SOURCE,
            connector.AMP_SOURCE,
            connector.CODEX_SOURCE / "dist/index.mjs",
            connector.PI_SOURCE / "src/langfuse.ts",
            connector.OMP_SOURCE / "src/langfuse.ts",
        ]
        for path in required:
            self.assertTrue(path.is_file(), f"missing source: {path}")

    def test_no_legacy_trace_or_session_reads(self) -> None:
        markers = (
            "/api/public/traces",
            "/api/public/sessions",
            "/api/public/observations",
        )
        hits: list[str] = []
        for path in (ROOT / "packages").rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".ts"}:
                continue
            if any(part in {".git", "node_modules", "__pycache__"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            hits.extend(f"{path.relative_to(ROOT)}: {marker}" for marker in markers if marker in text)
        self.assertEqual(hits, [])

    def test_v4_ingestion_header_is_present(self) -> None:
        for path in (
            connector.AMP_SOURCE,
            connector.PI_SOURCE / "src/langfuse.ts",
            connector.OMP_SOURCE / "src/langfuse.ts",
            connector.COMMON_SOURCE,
        ):
            self.assertIn("x-langfuse-ingestion-version", path.read_text(encoding="utf-8"))

    def test_turn_numbering_is_present(self) -> None:
        groups = {
            "claude": [connector.CLAUDE_SOURCE / "langfuse_hook.py"],
            "droid": [connector.DROID_SOURCE / "langfuse_hook.py"],
            "devin": [connector.DEVIN_SOURCE / "export_devin.py"],
            "amp": [connector.AMP_SOURCE],
            "codex": [connector.CODEX_SOURCE / "src/trace.ts"],
            "pi": list((connector.PI_SOURCE / "src").rglob("*.ts")),
            "omp": list((connector.OMP_SOURCE / "src").rglob("*.ts")),
        }
        for name, paths in groups.items():
            text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
            self.assertIn("turn_number", text, f"missing turn_number for {name}")
            self.assertTrue("Turn" in text or "emit_turn" in text, f"missing turn label for {name}")

    def test_plan_is_read_only_and_lists_all_adapters(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "connector.py"), "plan"],
            text=True,
            capture_output=True,
            check=True,
        )
        for name in connector.ADAPTERS:
            self.assertIn(f"{name}:", result.stdout)
        self.assertIn("repository:", result.stdout)

    def test_devin_common_link_is_valid(self) -> None:
        link = connector.DEVIN_SOURCE / "agent_langfuse_common.py"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), connector.COMMON_SOURCE.resolve())

    def test_amp_readme_is_not_a_managed_target(self) -> None:
        # The Amp personal plugin repository owns README.md; the connector only
        # manages langfuse.ts.
        self.assertEqual(connector.AMP_SOURCE.name, "langfuse.ts")


if __name__ == "__main__":
    unittest.main()
