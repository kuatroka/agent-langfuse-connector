#!/usr/bin/env python3
"""Langfuse Observations API v2 client used by the session verifiers.

The legacy trace/session/observation reads are deprecated and are removed on
Langfuse Cloud on 2026-11-16. This client only uses the v4-compatible read
surface:

  GET /api/public/v2/observations

It keeps cursor-based pagination and always sends bounded time ranges.
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KEYCHAIN_SERVICE = "amp-langfuse"
OBSERVATION_FIELDS = "core,basic,usage,trace_context,metadata"


def keychain(account: str) -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


class LangfuseV2:
    def __init__(self) -> None:
        self.base = keychain("base-url").rstrip("/")
        token = base64.b64encode(
            f'{keychain("public-key")}:{keychain("secret-key")}'.encode()
        ).decode()
        self.auth = "Basic " + token

    def get(self, path: str, **params):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(8):
            request = urllib.request.Request(url, headers={"Authorization": self.auth})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                detail = exc.read().decode(errors="replace")[:1000]
                raise RuntimeError(f"Langfuse HTTP {exc.code}: {detail}") from exc
        raise RuntimeError("Langfuse rate limited")

    def _query(self, **params) -> list[dict]:
        rows: list[dict] = []
        params = {
            "fields": OBSERVATION_FIELDS,
            "expandMetadata": "turn_number",
            "limit": "1000",
            **params,
        }
        while True:
            body = self.get("/api/public/v2/observations", **params)
            rows.extend(body.get("data") or [])
            meta = body.get("meta") or {}
            cursor = meta.get("cursor") or meta.get("nextCursor")
            if not cursor:
                return rows
            params = {**params, "cursor": cursor}
            time.sleep(0.5)

    @staticmethod
    def _time_params(from_time: datetime, to_time: datetime) -> dict[str, str]:
        return {
            "fromStartTime": from_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "toStartTime": to_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def observations(
        self,
        *,
        session_id: str,
        from_time: datetime,
        to_time: datetime,
    ) -> list[dict]:
        """Return the complete observation tree for a session.

        On the v4 data model, child observations do not repeat the trace's
        sessionId. Querying only by sessionId therefore returns the root
        observations and drops all generations/tools. Resolve the trace ids
        first, then fetch each trace's full tree by traceId.
        """
        time_params = self._time_params(from_time, to_time)
        session_rows = self._query(sessionId=session_id, **time_params)
        trace_ids: list[str] = []
        for row in session_rows:
            trace_id = row.get("traceId")
            if trace_id and trace_id not in trace_ids:
                trace_ids.append(trace_id)
        if not trace_ids:
            return session_rows
        rows: list[dict] = []
        for trace_id in trace_ids:
            rows.extend(self._query(traceId=trace_id, **time_params))
        return rows

    def observations_for_trace(
        self,
        *,
        trace_id: str,
        from_time: datetime,
        to_time: datetime,
    ) -> list[dict]:
        return self._query(trace_id=trace_id, **self._time_params(from_time, to_time))


def group_traces(rows: list[dict]) -> list[dict]:
    """Group v2 observation rows into trace summaries.

    Trace names, tags, and turn numbers live on observation rows in v2. The
    authoritative usage total is the sum of GENERATION totalUsage values.
    """
    grouped: dict[str, dict] = {}
    for row in rows:
        trace_id = row.get("traceId")
        if not trace_id:
            continue
        entry = grouped.setdefault(
            trace_id,
            {
                "id": trace_id,
                "name": None,
                "session_id": row.get("sessionId"),
                "tags": row.get("tags") or [],
                "turn_number": None,
                "start_time": row.get("startTime"),
                "generation_total": 0,
                "all_total": 0,
                "tools": 0,
                "generations": 0,
                "observations": 0,
            },
        )
        entry["observations"] += 1
        entry["name"] = row.get("traceName") or entry["name"]
        if row.get("startTime") and (
            not entry["start_time"] or row["startTime"] < entry["start_time"]
        ):
            entry["start_time"] = row["startTime"]
        if row.get("sessionId"):
            entry["session_id"] = row["sessionId"]
        if row.get("tags"):
            entry["tags"] = row["tags"]
        metadata = row.get("metadata") or {}
        if metadata.get("turn_number") is not None:
            try:
                entry["turn_number"] = int(metadata["turn_number"])
            except (TypeError, ValueError):
                entry["turn_number"] = metadata["turn_number"]
        total = int(row.get("totalUsage") or 0)
        entry["all_total"] += total
        if row.get("type") == "GENERATION":
            entry["generation_total"] += total
            entry["generations"] += 1
        if row.get("type") == "TOOL":
            entry["tools"] += 1
    return sorted(grouped.values(), key=lambda item: item.get("start_time") or "")


def default_window(days: int = 365) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now + timedelta(days=1)
