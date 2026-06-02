"""Tests for scripts/phase6_memory_migrate.py (Jarvis-OS Phase-6 data migration)."""
from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "phase6_memory_migrate.py"


@pytest.fixture(scope="module")
def mig():
    spec = importlib.util.spec_from_file_location("phase6_memory_migrate", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase6_memory_migrate"] = module
    spec.loader.exec_module(module)
    return module


# ── transforms ───────────────────────────────────────────────────────────
def test_comments_to_markdown(mig):
    rows = [
        {"task_id": "t1", "task_title": "Auth", "author": "jarvis", "body": "done", "type": "checkpoint", "created_at": "2026-05-10"},
        {"task_id": "t1", "task_title": "Auth", "author": "marco", "body": "review please", "type": "comment", "created_at": "2026-05-11"},
        {"task_id": "t2", "task_title": "DB", "author": "jarvis", "body": "open->wip", "type": "status_change", "created_at": "2026-05-12"},
    ]
    md = mig.comments_to_markdown("Hermes", rows)
    assert "# Task comments log: Hermes" in md
    assert "## Auth (t1)" in md and "## DB (t2)" in md
    assert "marco [comment]" in md and "review please" in md
    # one header per task
    assert md.count("## ") == 2


def test_recommendations_to_markdown(mig):
    rows = [
        {"type": "stale_project", "title": "stale", "reasoning": "no activity", "source_rule": "stale_check", "priority": "high", "created_at": "2026-05-10"},
    ]
    md = mig.recommendations_to_markdown("Hermes", rows)
    assert "# Active recommendations: Hermes" in md
    assert "## [high] stale" in md
    assert "no activity" in md


def test_decision_to_paperclip(mig):
    row = {
        "id": "dec1", "type": "approval", "title": "Use X", "context": "ctx",
        "answer": "Yes, X", "urgency": "high", "consequence": "Y", "answered_at": "2026-05-10", "answered_via": "telegram",
    }
    out = mig.decision_to_paperclip(row, source_project_slug="hermes-agent")
    assert out["source_key"] == "mc:dec1"
    assert out["source_project_slug"] == "hermes-agent"
    assert out["decision"] == "Yes, X"
    assert out["context"] == "ctx"
    assert out["consequences"] == "Y"
    assert out["status"] == "accepted"
    assert out["metadata"]["mc_type"] == "approval"
    expected_hash = hashlib.sha256("dec1|Yes, X|2026-05-10".encode()).hexdigest()
    assert out["source_hash"] == expected_hash


# ── readers (in-memory sqlite) ───────────────────────────────────────────
def _mc_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, status TEXT);
        CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, created_at TEXT);
        CREATE TABLE comments (id TEXT PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, type TEXT, created_at TEXT);
        CREATE TABLE recommendations (id TEXT PRIMARY KEY, project_id TEXT, type TEXT, title TEXT, reasoning TEXT, source_rule TEXT, priority TEXT, status TEXT, created_at TEXT);
        CREATE TABLE decisions (id TEXT PRIMARY KEY, project_id TEXT, type TEXT, title TEXT, context TEXT, answer TEXT, urgency TEXT, consequence TEXT, answered_at TEXT, answered_via TEXT, status TEXT);
        INSERT INTO projects VALUES ('hermes-agent','Hermes','active'),('old','Old','deprecated');
        INSERT INTO tasks VALUES ('t1','hermes-agent','Auth','2026-05-01');
        INSERT INTO comments VALUES ('c1','t1','jarvis','hi','comment','2026-05-02');
        INSERT INTO recommendations VALUES
          ('r1','hermes-agent','stale_project','stale','reason','stale_check','high','active','2026-05-03'),
          ('r2','hermes-agent','next_task','done','x','y','low','dismissed','2026-05-03');
        INSERT INTO decisions VALUES
          ('d1','hermes-agent','approval','T','ctx','Yes','high','c','2026-05-04','telegram','answered'),
          ('d2','hermes-agent','scope','T2',NULL,NULL,'low',NULL,NULL,NULL,'pending');
        """
    )
    return conn


def test_readers(mig):
    conn = _mc_db()
    comments = mig.read_comments_by_project(conn)
    assert list(comments.keys()) == ["hermes-agent"]
    assert comments["hermes-agent"][0]["body"] == "hi"

    recs = mig.read_active_recommendations_by_project(conn)
    assert len(recs["hermes-agent"]) == 1  # only active

    decisions = mig.read_answered_decisions(conn)
    assert len(decisions) == 1 and decisions[0]["id"] == "d1"  # only answered + project set


# ── plan + apply ─────────────────────────────────────────────────────────
def test_plan_skips_unmapped_and_builds_actions(mig):
    plan = mig.plan_migration(
        comments_by_project={"hermes-agent": [{"task_id": "t1", "task_title": "A", "author": "j", "body": "b", "type": "comment", "created_at": "x", "project_name": "Hermes"}],
                             "casa-marco": [{"task_id": "t9", "task_title": "Z", "author": "j", "body": "b", "type": "comment", "created_at": "x", "project_name": "Casa"}]},
        recs_by_project={},
        answered_decisions=[{"id": "d1", "mc_project_id": "hermes-agent", "type": "approval", "title": "T", "context": "c", "answer": "Yes", "urgency": "high", "consequence": None, "answered_at": "x", "answered_via": None}],
        mapping={"hermes-agent": "uuid-hermes"},
        company_id="comp1",
    )
    docs = [a for a in plan["actions"] if a["kind"] == "project_document"]
    decs = [a for a in plan["actions"] if a["kind"] == "decision"]
    assert len(docs) == 1 and docs[0]["project_id"] == "uuid-hermes" and docs[0]["key"] == mig.COMMENTS_DOC_KEY
    assert len(decs) == 1 and decs[0]["source_key"] == "mc:d1"
    # casa-marco unmapped -> skipped
    assert any(s["mc_project_slug"] == "casa-marco" for s in plan["skips"])


def test_apply_actions_calls_client(mig):
    calls = {"docs": [], "decs": []}

    class FakeClient:
        def upsert_project_document(self, **kw):
            calls["docs"].append(kw)
            return {"key": kw["key"]}

        def upsert_decision(self, **kw):
            calls["decs"].append(kw)
            return {"source_key": kw["source_key"]}

    actions = [
        {"kind": "project_document", "company_id": "c", "project_id": "u", "mc_project_slug": "s", "key": "mc-task-comments", "body": "x", "title": "t", "tags": ["mc-import"]},
        {"kind": "decision", "company_id": "c", "project_id": "u", "mc_project_slug": "s", "source_project_slug": "s", "source_key": "mc:d1", "source_hash": "h", "title": "t", "decision": "d", "context": None, "consequences": None, "status": "accepted", "metadata": {}},
    ]
    results = mig.apply_actions(FakeClient(), actions)
    assert len(results) == 2
    assert calls["docs"][0]["key"] == "mc-task-comments"
    assert calls["decs"][0]["source_key"] == "mc:d1"
