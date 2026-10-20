import * as fs from "node:fs/promises";

/**
 * Per-rollout dedup ledger.
 *
 * The `Stop` hook fires after every Codex turn and re-reads the whole rollout
 * file, so completed turns would be re-uploaded each time. We record uploaded
 * turn ids in a sidecar file (`<rolloutFile>.langfuse`) and skip them on
 * subsequent invocations. In-progress (not-yet-completed) turns are uploaded
 * but intentionally not recorded, so they finalize on the next hook run.
 */
export async function loadUploadedTurnIds(rolloutFile: string): Promise<Set<string>> {
  try {
    const data = await fs.readFile(`${rolloutFile}.langfuse`, "utf-8");
    return new Set(data.split("\n").filter(Boolean));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return new Set();
    throw error;
  }
}

export async function markTurnUploaded(rolloutFile: string, turnId: string): Promise<void> {
  try {
    await fs.appendFile(`${rolloutFile}.langfuse`, `${turnId}\n`, "utf-8");
  } catch {
    // Best-effort: a failed write only risks a duplicate upload next time.
  }
}

/**
 * Serialize Stop hook invocations for one rollout.
 *
 * Codex can run the Stop hook more than once for a turn. Without a lock, two
 * hook processes both read an empty ledger, upload the same turn, and append
 * the same id afterwards, producing duplicate Langfuse traces. Wait for the
 * first process to finish and mark the turn, then let the second one skip it.
 */
export async function acquireRolloutLock(rolloutFile: string): Promise<() => Promise<void>> {
  const lockPath = `${rolloutFile}.langfuse.lock`;
  const startedAt = Date.now();
  for (;;) {
    try {
      const handle = await fs.open(lockPath, "wx");
      await handle.writeFile(String(process.pid), "utf-8");
      await handle.close();
      return async () => {
        try {
          await fs.unlink(lockPath);
        } catch {
          // Another process may have cleared a stale lock.
        }
      };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      try {
        const stat = await fs.stat(lockPath);
        if (Date.now() - stat.mtimeMs > 60_000) {
          await fs.unlink(lockPath).catch(() => undefined);
          continue;
        }
      } catch {
        // The lock disappeared between open and stat; retry immediately.
        continue;
      }
      if (Date.now() - startedAt > 20_000) return async () => undefined;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
}
