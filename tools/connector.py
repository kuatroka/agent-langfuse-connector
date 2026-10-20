#!/usr/bin/env python3
"""Install, inspect, and verify the local Langfuse connectors.

The repository is the source of truth. This tool copies or links the built
adapter files into each host's active location, leaving a backup of anything
it replaces. It does not contain credentials and does not modify Langfuse.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = HOME / ".local" / "share" / "agent-langfuse-connector"
BACKUP_ROOT = STATE_ROOT / "backups"
STATE_PATH = STATE_ROOT / "install-state.json"
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

PYTHON_PACKAGE = ROOT / "packages" / "python"
DROID_SOURCE = PYTHON_PACKAGE / "hooks" / "droid"
DEVIN_SOURCE = PYTHON_PACKAGE / "hooks" / "devin"
CLAUDE_SOURCE = PYTHON_PACKAGE / "hooks" / "claude"
COMMON_SOURCE = PYTHON_PACKAGE / "agent_langfuse_connector" / "common.py"
AMP_SOURCE = ROOT / "packages" / "typescript" / "amp" / "langfuse.ts"
CODEX_SOURCE = ROOT / "packages" / "typescript" / "codex" / "plugins" / "tracing"
PI_SOURCE = ROOT / "packages" / "typescript" / "pi"
OMP_SOURCE = ROOT / "packages" / "typescript" / "omp"
OMP_STAGE = HOME / ".omp" / "plugins" / "agent-langfuse-connector" / "omp"

ADAPTERS = ("claude", "droid", "devin", "amp", "codex", "pi", "omp")
EXCLUDED_PARTS = {".git", "node_modules", "__pycache__", ".DS_Store"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup(path: Path, adapter: str) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    destination = BACKUP_ROOT / adapter / RUN_ID / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        destination = destination.with_name(destination.name + ".symlink.txt")
        destination.write_text(os.readlink(path) + "\n", encoding="utf-8")
        return destination
    if path.is_dir():
        shutil.copytree(path, destination)
    else:
        shutil.copy2(path, destination)
    return destination


def copy_file(source: Path, target: Path, adapter: str, dry_run: bool) -> bool:
    if not source.is_file():
        raise FileNotFoundError(f"missing source file: {source}")
    if target.is_file() and digest(source) == digest(target):
        print(f"  unchanged  {target}")
        return False
    print(f"  {'would copy' if dry_run else 'copy':<10} {target}")
    if dry_run:
        return True
    if target.exists() or target.is_symlink():
        backup(target, adapter)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def copy_tree(source: Path, target: Path, adapter: str, dry_run: bool) -> int:
    changed = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        relative = path.relative_to(source)
        changed += int(copy_file(path, target / relative, adapter, dry_run))
    return changed


def replace_symlink(source: Path, target: Path, adapter: str, dry_run: bool) -> bool:
    source = source.resolve()
    if target.is_symlink() and os.path.realpath(target) == os.path.realpath(source):
        print(f"  unchanged  {target} -> {source}")
        return False
    print(f"  {'would link' if dry_run else 'link':<10} {target} -> {source}")
    if dry_run:
        return True
    if target.is_dir() and not target.is_symlink():
        destination = BACKUP_ROOT / adapter / RUN_ID / target.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(destination))
    else:
        if target.exists() or target.is_symlink():
            backup(target, adapter)
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(source, target)
    return True


def merge_devin_config(hook_path: Path, dry_run: bool) -> bool:
    config_path = HOME / ".config" / "devin" / "config.json"
    if not config_path.is_file():
        print(f"  skipped    Devin config not found: {config_path}")
        return False
    data = json.loads(config_path.read_text(encoding="utf-8"))
    hooks = data.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    command = f"python3 {hook_path}"
    if any(command in json.dumps(entry) for entry in stop):
        print("  unchanged  Devin Stop hook already registered")
        return False
    print(f"  {'would add' if dry_run else 'add':<10} Devin Stop hook")
    if dry_run:
        return True
    backup(config_path, "devin")
    stop.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": command, "timeout": 20}],
    })
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def droid_targets() -> list[Path]:
    targets = [HOME / ".factory" / "local-langfuse-marketplace" / "factory-langfuse"]
    cache = HOME / ".factory" / "plugins" / "cache"
    for candidate in sorted(cache.glob("local-langfuse-marketplace-*/factory-langfuse-*/*")):
        if candidate.is_dir():
            targets.append(candidate)
    return targets


def codex_target() -> Path | None:
    root = HOME / ".codex" / "plugins" / "cache" / "personal-langfuse" / "tracing-normalized"
    candidates = sorted((path for path in root.glob("*") if path.is_dir()), key=lambda path: path.name)
    return candidates[-1] if candidates else None


def install_claude(dry_run: bool) -> list[Path]:
    target = HOME / ".claude" / "hooks" / "langfuse_hook.py"
    copy_file(CLAUDE_SOURCE / "langfuse_hook.py", target, "claude", dry_run)
    return [target]


def install_droid(dry_run: bool) -> list[Path]:
    paths: list[Path] = []
    mappings = (
        ("langfuse_hook.py", "hooks/langfuse_hook.py"),
        ("hooks.json", "hooks/hooks.json"),
        ("run.sh", "hooks/run.sh"),
        ("plugin.json", ".factory-plugin/plugin.json"),
    )
    for target_root in droid_targets():
        if not target_root.exists():
            print(f"  skipped    Droid target not found: {target_root}")
            continue
        for source_name, relative in mappings:
            target = target_root / relative
            copy_file(DROID_SOURCE / source_name, target, "droid", dry_run)
            paths.append(target)
    return paths


def remove_devin_config_hook(dry_run: bool) -> bool:
    config_path = HOME / ".config" / "devin" / "config.json"
    if not config_path.is_file():
        return False
    data = json.loads(config_path.read_text(encoding="utf-8"))
    stop = data.get("hooks", {}).get("Stop", [])
    kept = [
        entry for entry in stop
        if "connector/hooks/export_devin.py" not in json.dumps(entry)
    ]
    if len(kept) == len(stop):
        return False
    print(f"  {'would remove' if dry_run else 'remove':<11} legacy Devin connector Stop hook")
    if dry_run:
        return True
    backup(config_path, "devin")
    if kept:
        data["hooks"]["Stop"] = kept
    else:
        data.get("hooks", {}).pop("Stop", None)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def install_devin(dry_run: bool) -> list[Path]:
    if shutil.which("devin"):
        print(f"  {'would install' if dry_run else 'install':<11} Devin plugin {DEVIN_SOURCE}")
        if not dry_run:
            result = subprocess.run(
                ["devin", "plugins", "install", "--local", "-y", str(DEVIN_SOURCE)],
                text=True,
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout)[-1000:])
        remove_devin_config_hook(dry_run)
        return [DEVIN_SOURCE, HOME / ".config" / "devin" / "config.json"]

    # Fallback for hosts without the Devin plugin CLI: install the files and
    # register the same Stop hook directly in config.json.
    target_root = HOME / ".config" / "devin" / "connector" / "hooks"
    hook = target_root / "export_devin.py"
    common = target_root / "agent_langfuse_common.py"
    copy_file(DEVIN_SOURCE / "export_devin.py", hook, "devin", dry_run)
    copy_file(COMMON_SOURCE, common, "devin", dry_run)
    merge_devin_config(hook, dry_run)
    return [hook, common, HOME / ".config" / "devin" / "config.json"]


def install_amp(dry_run: bool) -> list[Path]:
    target = HOME / ".cache" / "amp" / "repositories" / "ampcode.com-user-plugins" / "langfuse.ts"
    copy_file(AMP_SOURCE, target, "amp", dry_run)
    return [target]


def install_codex(dry_run: bool) -> list[Path]:
    target_root = codex_target()
    if target_root is None:
        print("  skipped    Codex plugin target not found")
        return []
    paths: list[Path] = []
    for source_name, relative in (
        ("dist/index.mjs", "dist/index.mjs"),
        ("hooks/hooks.json", "hooks/hooks.json"),
        (".codex-plugin/plugin.json", ".codex-plugin/plugin.json"),
    ):
        target = target_root / relative
        copy_file(CODEX_SOURCE / source_name, target, "codex", dry_run)
        paths.append(target)
    return paths


def install_pi(dry_run: bool) -> list[Path]:
    target_root = HOME / ".pi" / "agent" / "npm" / "node_modules" / "pi-langfuse"
    if not target_root.exists():
        print(f"  skipped    Pi package target not found: {target_root}")
        return []
    copy_tree(PI_SOURCE, target_root, "pi", dry_run)
    return [target_root]


def install_omp(dry_run: bool) -> list[Path]:
    target = HOME / ".omp" / "plugins" / "node_modules" / "pi-langfuse"
    if not target.parent.exists():
        print(f"  skipped    OMP package parent not found: {target.parent}")
        return []
    print(f"  {'would stage' if dry_run else 'stage':<18} {OMP_STAGE}")
    if not dry_run:
        if OMP_STAGE.is_symlink():
            OMP_STAGE.unlink()
        OMP_STAGE.mkdir(parents=True, exist_ok=True)
    copy_tree(OMP_SOURCE, OMP_STAGE, "omp", dry_run)
    # OMP must see a linked plugin so its lockfile loader starts it. The stage
    # sits under ~/.omp/plugins so Node resolves dependencies from the host's
    # existing node_modules tree.
    replace_symlink(OMP_STAGE, target, "omp", dry_run)
    return [OMP_STAGE, target]


INSTALLERS = {
    "claude": install_claude,
    "droid": install_droid,
    "devin": install_devin,
    "amp": install_amp,
    "codex": install_codex,
    "pi": install_pi,
    "omp": install_omp,
}


def record_state(adapter: str, targets: list[Path]) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    state[adapter] = {
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "targets": [str(target) for target in targets],
    }
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def command_plan(_: argparse.Namespace) -> int:
    print(f"repository: {ROOT}")
    for name in ADAPTERS:
        print(f"{name}:")
        if name == "claude":
            print(f"  {CLAUDE_SOURCE / 'langfuse_hook.py'} -> {HOME / '.claude/hooks/langfuse_hook.py'}")
        elif name == "droid":
            for target in droid_targets():
                print(f"  {DROID_SOURCE} -> {target}")
        elif name == "devin":
            print(f"  {DEVIN_SOURCE} -> Devin plugin store (native plugin install)")
        elif name == "amp":
            print(f"  {AMP_SOURCE} -> {HOME / '.cache/amp/repositories/ampcode.com-user-plugins/langfuse.ts'}")
        elif name == "codex":
            print(f"  {CODEX_SOURCE} -> {codex_target() or '<not installed>'}")
        elif name == "pi":
            print(f"  {PI_SOURCE} -> {HOME / '.pi/agent/npm/node_modules/pi-langfuse'}")
        elif name == "omp":
            print(f"  {OMP_SOURCE} -> {OMP_STAGE} -> {HOME / '.omp/plugins/node_modules/pi-langfuse'} (symlink)")
    return 0


def command_install(args: argparse.Namespace) -> int:
    selected = args.only or list(ADAPTERS)
    if args.all:
        selected = list(ADAPTERS)
    print(f"install run: {RUN_ID}{' (dry run)' if args.dry_run else ''}")
    failures = 0
    for name in selected:
        print(f"\n[{name}]")
        try:
            targets = INSTALLERS[name](args.dry_run)
            if not args.dry_run:
                record_state(name, targets)
        except Exception as error:
            failures += 1
            print(f"  FAILED     {type(error).__name__}: {error}", file=sys.stderr)
    if failures:
        print(f"\n{failures} adapter(s) failed", file=sys.stderr)
        return 1
    print("\ninstall complete")
    return 0


def check_file(source: Path, target: Path) -> tuple[bool, str]:
    if not target.is_file():
        return False, "missing"
    if digest(source) != digest(target):
        return False, "different content"
    return True, "ok"


def command_doctor(_: argparse.Namespace) -> int:
    failures = 0
    checks: list[tuple[str, bool, str]] = []

    checks.append(("claude hook", *check_file(
        CLAUDE_SOURCE / "langfuse_hook.py",
        HOME / ".claude/hooks/langfuse_hook.py",
    )))

    for target_root in droid_targets():
        for source_name, relative in (
            ("langfuse_hook.py", "hooks/langfuse_hook.py"),
            ("hooks.json", "hooks/hooks.json"),
            ("run.sh", "hooks/run.sh"),
            ("plugin.json", ".factory-plugin/plugin.json"),
        ):
            checks.append((f"droid {target_root.name}/{relative}", *check_file(
                DROID_SOURCE / source_name, target_root / relative,
            )))

    devin_manifest = DEVIN_SOURCE / ".devin-plugin" / "plugin.json"
    devin_config = HOME / ".config/devin/config.json"
    devin_ok = False
    devin_detail = "devin CLI unavailable and no fallback hook"
    if shutil.which("devin"):
        result = subprocess.run(
            ["devin", "plugins", "list"],
            text=True, capture_output=True, timeout=30,
        )
        devin_ok = result.returncode == 0 and "langfuse-unified" in result.stdout
        devin_detail = "plugin registered" if devin_ok else (result.stderr or result.stdout)[-300:]
    elif devin_config.is_file():
        try:
            devin_ok = "connector/hooks/export_devin.py" in devin_config.read_text(encoding="utf-8")
            devin_detail = "fallback Stop hook registered" if devin_ok else "fallback Stop hook missing"
        except OSError:
            devin_detail = "config unreadable"
    checks.append(("devin plugin", devin_ok, devin_detail))
    checks.append(("devin manifest", devin_manifest.is_file(), "ok" if devin_manifest.is_file() else "missing"))

    checks.append(("amp plugin", *check_file(
        AMP_SOURCE,
        HOME / ".cache/amp/repositories/ampcode.com-user-plugins/langfuse.ts",
    )))

    codex = codex_target()
    if codex:
        for source_name, relative in (
            ("dist/index.mjs", "dist/index.mjs"),
            ("hooks/hooks.json", "hooks/hooks.json"),
            (".codex-plugin/plugin.json", ".codex-plugin/plugin.json"),
        ):
            checks.append((f"codex {relative}", *check_file(CODEX_SOURCE / source_name, codex / relative)))
    else:
        checks.append(("codex plugin", False, "not installed"))

    pi_target = HOME / ".pi" / "agent" / "npm" / "node_modules" / "pi-langfuse"
    pi_ok = (pi_target / "src" / "langfuse.ts").is_file() and (
        (pi_target / "src" / "langfuse.ts").read_bytes() == (PI_SOURCE / "src" / "langfuse.ts").read_bytes()
    )
    checks.append(("pi package", pi_ok, "ok" if pi_ok else "missing or different"))

    omp_target = HOME / ".omp" / "plugins" / "node_modules" / "pi-langfuse"
    omp_stage = OMP_STAGE / "src" / "langfuse.ts"
    omp_ok = (
        omp_target.is_symlink()
        and os.path.realpath(omp_target) == os.path.realpath(OMP_STAGE)
        and omp_stage.is_file()
        and omp_stage.read_bytes() == (OMP_SOURCE / "src" / "langfuse.ts").read_bytes()
    )
    checks.append(("omp package", omp_ok, "ok" if omp_ok else "missing or different"))

    for label, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL':4} {label}: {detail}")
        failures += int(not ok)

    legacy_hits = []
    for path in list(ROOT.glob("packages/**/*.py")) + list(ROOT.glob("packages/**/*.ts")):
        if any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in ("/api/public/traces", "/api/public/sessions", "/api/public/observations"):
            if marker in text:
                legacy_hits.append(f"{path.relative_to(ROOT)}: {marker}")
    for hit in legacy_hits:
        print(f"FAIL legacy endpoint: {hit}")
        failures += 1
    if not legacy_hits:
        print("PASS no legacy trace, session, or v1 observation endpoints in packages")

    for path in (AMP_SOURCE, PI_SOURCE / "src/langfuse.ts", OMP_SOURCE / "src/langfuse.ts", COMMON_SOURCE):
        if "x-langfuse-ingestion-version" not in path.read_text(encoding="utf-8", errors="replace"):
            print(f"FAIL missing v4 header in {path.relative_to(ROOT)}")
            failures += 1
    if not failures:
        print("PASS v4 ingestion headers present")
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


def command_verify(args: argparse.Namespace) -> int:
    command = [sys.executable, str(ROOT / "tools" / "verify_agent_session.py")]
    if args.harness:
        command += ["--harness", args.harness]
    if args.session_id:
        command += ["--session-id", args.session_id]
    if args.skip_native:
        command.append("--skip-native")
    if args.from_time:
        command += ["--from", args.from_time]
    if args.to_time:
        command += ["--to", args.to_time]
    return subprocess.call(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="show source-to-target mappings")
    plan.set_defaults(func=command_plan)

    install = subparsers.add_parser("install", help="install one or more adapters")
    install.add_argument("--all", action="store_true", help="install every adapter")
    install.add_argument("--only", nargs="+", choices=ADAPTERS, help="install selected adapters")
    install.add_argument("--dry-run", action="store_true", help="show changes without writing")
    install.set_defaults(func=command_install)

    doctor = subparsers.add_parser("doctor", help="verify installed files and source invariants")
    doctor.set_defaults(func=command_doctor)

    verify = subparsers.add_parser("verify", help="run the Langfuse v2 session verifier")
    verify.add_argument("--harness", choices=ADAPTERS)
    verify.add_argument("--session-id")
    verify.add_argument("--from", dest="from_time")
    verify.add_argument("--to", dest="to_time")
    verify.add_argument("--skip-native", action="store_true")
    verify.set_defaults(func=command_verify)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
