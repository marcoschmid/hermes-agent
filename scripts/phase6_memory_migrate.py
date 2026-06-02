#!/usr/bin/env python3
"""Phase-6 project-memory migration: Mission Control (SQLite) → Paperclip.

Jarvis-OS Phase-6. Backfills MC project-memory into Paperclip project-memory
(decisions + project-memory documents) via PaperclipProjectMemoryClient.

DRY-RUN BY DEFAULT — prints a plan and writes nothing. Live writes (--apply)
are a deliberate, gated step (production data write to Paperclip).

Mapping decisions (reversible):
  * comments (per task, joined to project)  -> project_document key "mc-task-comments"
  * recommendations status='active'         -> project_document key "mc-active-recommendations"
  * decisions status='answered' (proj set)  -> Paperclip decision, source_key "mc:{id}"
  * decision_board_views / non-answered / stale -> SKIP

MC projects.id is the workspace slug; Paperclip projects.id is a UUID with no
slug. There is NO automatic join — a curated mapping file is REQUIRED:
  { "mc-slug": "paperclip-project-uuid", ... }
Unmapped MC projects are skipped and reported (no Paperclip project is created).

Idempotency: decisions upsert on (company_id, source_key); documents upsert on
the (company, project, key) document — re-runs are safe.

Usage:
  python scripts/phase6_memory_migrate.py --db <mission-control.db> \
      --mapping <map.json> --company-id <uuid> [--apply]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from typing import Any, Optional

COMMENTS_DOC_KEY = "mc-task-comments"
RECOMMENDATIONS_DOC_KEY = "mc-active-recommendations"


# ── pure transforms ──────────────────────────────────────────────────────
def comments_to_markdown(project_name: str, rows: list[dict[str, Any]]) -> str:
    """rows: {task_id, task_title, author, body, type, created_at} ordered by task then time."""
    lines = [f"# Task comments log: {project_name}", "", "_Imported from Mission Control._", ""]
    current_task: Optional[str] = None
    for row in rows:
        if row["task_id"] != current_task:
            current_task = row["task_id"]
            lines.append(f"## {row.get('task_title') or row['task_id']} ({row['task_id']})")
            lines.append("")
        lines.append(f"**{row['created_at']}** — {row['author']} [{row.get('type', 'comment')}]")
        lines.append(row["body"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def recommendations_to_markdown(project_name: str, rows: list[dict[str, Any]]) -> str:
    """rows: {type, title, reasoning, source_rule, priority, created_at} ordered by priority."""
    lines = [f"# Active recommendations: {project_name}", "", "_Imported from Mission Control._", ""]
    for row in rows:
        lines.append(f"## [{row['priority']}] {row['title']}")
        lines.append(f"_type: {row['type']} · rule: {row.get('source_rule') or '—'} · {row['created_at']}_")
        if row.get("reasoning"):
            lines.append("")
            lines.append(row["reasoning"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def decision_to_paperclip(row: dict[str, Any], *, source_project_slug: str) -> dict[str, Any]:
    """MC answered decision row -> kwargs for PaperclipProjectMemoryClient.upsert_decision."""
    source_key = f"mc:{row['id']}"
    digest = hashlib.sha256(
        f"{row['id']}|{row.get('answer') or ''}|{row.get('answered_at') or ''}".encode()
    ).hexdigest()
    metadata = {"mc_type": row.get("type"), "mc_urgency": row.get("urgency")}
    if row.get("answered_via"):
        metadata["mc_answered_via"] = row["answered_via"]
    return {
        "source_project_slug": source_project_slug,
        "source_key": source_key,
        "source_hash": digest,
        "title": row["title"],
        "decision": row.get("answer") or "(no answer recorded)",
        "context": row.get("context"),
        "consequences": row.get("consequence"),
        "status": "accepted",
        "metadata": metadata,
    }


# ── read-only MC readers ─────────────────────────────────────────────────
def _rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql).fetchall()]


def read_comments_by_project(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    rows = _rows(
        conn,
        """
        SELECT p.id AS mc_project_id, p.name AS project_name, t.id AS task_id,
               t.title AS task_title, c.id AS comment_id, c.author, c.body,
               c.type, c.created_at
        FROM projects p
        JOIN tasks t ON t.project_id = p.id
        JOIN comments c ON c.task_id = t.id
        WHERE p.status != 'deprecated'
        ORDER BY p.id, t.created_at, c.created_at ASC
        """,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["mc_project_id"], []).append(r)
    return grouped


def read_active_recommendations_by_project(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    rows = _rows(
        conn,
        """
        SELECT r.project_id AS mc_project_id, r.type, r.title, r.reasoning,
               r.source_rule, r.priority, r.created_at
        FROM recommendations r
        WHERE r.status = 'active'
        ORDER BY r.project_id,
          CASE r.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, r.created_at
        """,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["mc_project_id"], []).append(r)
    return grouped


def read_answered_decisions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT d.id, d.project_id AS mc_project_id, d.type, d.title, d.context,
               d.answer, d.urgency, d.consequence, d.answered_at, d.answered_via
        FROM decisions d
        WHERE d.status = 'answered' AND d.project_id IS NOT NULL
        """,
    )


def _project_name(comments: dict, recs: dict, slug: str) -> str:
    for grouped in (comments, recs):
        rows = grouped.get(slug)
        if rows and rows[0].get("project_name"):
            return rows[0]["project_name"]
    return slug


# ── plan (pure) ──────────────────────────────────────────────────────────
def plan_migration(
    *,
    comments_by_project: dict[str, list[dict[str, Any]]],
    recs_by_project: dict[str, list[dict[str, Any]]],
    answered_decisions: list[dict[str, Any]],
    mapping: dict[str, str],
    company_id: str,
) -> dict[str, list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    skips: list[dict[str, Any]] = []

    def resolve(slug: str, kind: str) -> Optional[str]:
        uuid = mapping.get(slug)
        if not uuid:
            skips.append({"mc_project_slug": slug, "kind": kind, "reason": "no paperclip project in mapping"})
        return uuid

    for slug, rows in comments_by_project.items():
        uuid = resolve(slug, "comments")
        if not uuid:
            continue
        actions.append({
            "kind": "project_document",
            "source": "comments",
            "company_id": company_id,
            "project_id": uuid,
            "mc_project_slug": slug,
            "key": COMMENTS_DOC_KEY,
            "title": f"MC task comments: {_project_name(comments_by_project, recs_by_project, slug)}",
            "body": comments_to_markdown(_project_name(comments_by_project, recs_by_project, slug), rows),
            "tags": ["mc-import", "comments"],
            "count": len(rows),
        })

    for slug, rows in recs_by_project.items():
        uuid = resolve(slug, "recommendations")
        if not uuid:
            continue
        actions.append({
            "kind": "project_document",
            "source": "recommendations",
            "company_id": company_id,
            "project_id": uuid,
            "mc_project_slug": slug,
            "key": RECOMMENDATIONS_DOC_KEY,
            "title": f"MC active recommendations: {_project_name(comments_by_project, recs_by_project, slug)}",
            "body": recommendations_to_markdown(_project_name(comments_by_project, recs_by_project, slug), rows),
            "tags": ["mc-import", "recommendations"],
            "count": len(rows),
        })

    for row in answered_decisions:
        slug = row["mc_project_id"]
        uuid = resolve(slug, "decision")
        if not uuid:
            continue
        payload = decision_to_paperclip(row, source_project_slug=slug)
        actions.append({
            "kind": "decision",
            "company_id": company_id,
            "project_id": uuid,
            "mc_project_slug": slug,
            **payload,
        })

    return {"actions": actions, "skips": skips}


# ── apply (gated) ────────────────────────────────────────────────────────
def apply_actions(client, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for a in actions:
        if a["kind"] == "project_document":
            res = client.upsert_project_document(
                company_id=a["company_id"], project_id=a["project_id"], key=a["key"],
                body=a["body"], title=a.get("title"), tags=a.get("tags"),
                change_summary="Phase-6 MC import",
            )
            results.append({"kind": a["kind"], "key": a["key"], "slug": a["mc_project_slug"], "result": res})
        elif a["kind"] == "decision":
            res = client.upsert_decision(
                company_id=a["company_id"], project_id=a["project_id"],
                source_project_slug=a["source_project_slug"], source_key=a["source_key"],
                source_hash=a["source_hash"], title=a["title"], decision=a["decision"],
                context=a.get("context"), consequences=a.get("consequences"),
                status=a.get("status"), metadata=a.get("metadata"),
            )
            results.append({"kind": a["kind"], "source_key": a["source_key"], "slug": a["mc_project_slug"], "result": res})
    return results


def _print_plan(plan: dict[str, list[dict[str, Any]]]) -> None:
    actions, skips = plan["actions"], plan["skips"]
    print(f"PLAN: {len(actions)} write(s), {len(skips)} skip(s)\n")
    for a in actions:
        if a["kind"] == "project_document":
            print(f"  [doc] {a['mc_project_slug']} key={a['key']} ({a.get('count')} rows, {len(a['body'])} chars) -> project {a['project_id']}")
        else:
            print(f"  [decision] {a['mc_project_slug']} source_key={a['source_key']} -> project {a['project_id']}")
    if skips:
        print("\nSKIPPED:")
        for s in skips:
            print(f"  {s['mc_project_slug']} ({s['kind']}): {s['reason']}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Phase-6 MC->Paperclip project-memory migration (dry-run default).")
    ap.add_argument("--db", required=True, help="Path to mission-control.db (read-only).")
    ap.add_argument("--mapping", required=True, help="JSON file: {mc_slug: paperclip_project_uuid}.")
    ap.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"), help="Paperclip company UUID.")
    ap.add_argument("--apply", action="store_true", help="GATED: actually write to Paperclip (default: dry-run).")
    ap.add_argument("--base-url", default=None, help="Paperclip base URL (else PAPERCLIP_API_URL).")
    args = ap.parse_args(argv)

    if not args.company_id:
        print("ERROR: --company-id (or PAPERCLIP_COMPANY_ID) is required.")
        return 2
    with open(args.mapping, encoding="utf-8") as fh:
        mapping = json.load(fh)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        plan = plan_migration(
            comments_by_project=read_comments_by_project(conn),
            recs_by_project=read_active_recommendations_by_project(conn),
            answered_decisions=read_answered_decisions(conn),
            mapping=mapping,
            company_id=args.company_id,
        )
    finally:
        conn.close()

    _print_plan(plan)
    if not args.apply:
        print("\nDRY-RUN — nothing written. Re-run with --apply to write (gated).")
        return 0

    from gateway.paperclip_project_memory_client import PaperclipProjectMemoryClient

    with PaperclipProjectMemoryClient(base_url=args.base_url) as client:
        results = apply_actions(client, plan["actions"])
    print(f"\nAPPLIED {len(results)} write(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
