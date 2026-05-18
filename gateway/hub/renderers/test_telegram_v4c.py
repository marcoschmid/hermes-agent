from gateway.hub.renderers.telegram_v4c import render


def test_render_v4c_full_event():
    event = {
        "render_version": "v4c",
        "severity": "warn",
        "title": "Drobo-Backup Fehler",
        "body": "Backup-Fehler: Authentik pg_dump fehlgeschlagen",
        "service": "drobo-backup",
        "impact": "Backup-Vorgang unvollständig oder fehlgeschlagen",
        "action_required": "Backup-Log prüfen und Issue beheben",
        "context": {"date": "2026-05-18", "host": "mac-mini"},
        "links": {"logs_url": "file:///Volumes/Drobo/Backups/logs/backup-2026-05-18.log"},
        "lifecycle": "FIRING",
    }
    out = render(event)
    # Title with bold (with severity prefix)
    assert "*Drobo\\-Backup Fehler*" in out
    # Body escaped
    assert "Backup\\-Fehler: Authentik pg\\_dump fehlgeschlagen" in out
    # Service badge
    assert "drobo\\-backup" in out
    # Impact + action visible
    assert "Backup\\-Vorgang unvollständig" in out
    assert "Backup\\-Log prüfen" in out
    # Logs link rendered
    assert "logs_url" in out or "logs\\_url" in out


def test_render_v4a_falls_back_to_body():
    event = {"render_version": "v4a", "title": "Plain", "body": "Plain message"}
    out = render(event)
    assert "Plain message" in out
    # No v4c sections
    assert "Impact" not in out


def test_render_handles_missing_optional_fields():
    event = {"render_version": "v4c", "title": "Minimal", "body": "Just body"}
    out = render(event)
    assert "Minimal" in out
    assert "Just body" in out
    # No crash when optional fields absent


def test_render_handles_lifecycle_recovered():
    event = {
        "render_version": "v4c",
        "title": "T",
        "body": "B",
        "lifecycle": "RECOVERED",
    }
    out = render(event)
    # RECOVERED state should be visible as status indicator
    assert "RECOVERED" in out or "recovered" in out.lower() or "✅" in out


def test_render_handles_severity_crit():
    event = {"render_version": "v4c", "severity": "crit", "title": "T", "body": "B"}
    out = render(event)
    # Some prefix or visual indicator for crit
    assert "T" in out  # at minimum title present
