"""Notification Hub API — Phase v4a dispatcher with HMAC auth.

Routes:
  GET /v1/health — liveness
  POST /v1/notifications — single dispatch endpoint, runs the 8-step pipeline.
    Phase v4a auth: HMAC(timestamp+nonce+body) signed with per-source hub_secret.
    Headers: X-Hub-Timestamp, X-Hub-Nonce, X-Hub-Signature.
    Bypass: HUB_PILOT_TOKEN Bearer (back-compat for transition; deprecate in v4b).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from gateway.hub.hmac_sign import HmacError, verify as hmac_verify
from gateway.hub.hub_state import HubState, connect as hub_state_connect
from gateway.hub.nonce_store import remember_or_replay
from gateway.hub.pipeline import PipelineError, run_pipeline
from gateway.hub.registry_client import RegistryClient, RegistryUnavailable
from gateway.hub.registry_sync import RegistrySync
from gateway.hub.schemas import NotificationIntent
from gateway.hub.secret_store import get_secret_store

logger = logging.getLogger(__name__)

router = APIRouter()


def _registry_unavailable_exc() -> HTTPException:
    """503 for a transient registry (Mission Control) outage.

    Distinct from a 500 crash and from a 401 auth rejection: a revoked token,
    a 5xx, or a network failure on a registry read means the hub could not
    read its config, not that the caller is unauthorized. Surfacing it as a
    typed 503 keeps one source's registry failure from cascading the whole
    hub hop into direct-fallback (the 2026-05-27 regression).
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error_code": "registry_unavailable",
            "message": "Notification registry (Mission Control) is unavailable",
        },
    )


async def _audit_auth_failed(
    error_code: str,
    message: str,
    source_slug: str | None = None,
    request: Request | None = None,
) -> None:
    """Phase 2 finding fix: write audit row before raising auth-failure HTTPException.

    Best-effort: any audit-push failure is swallowed (logged at WARN) so the
    auth-rejection contract is never weakened by MC outages.
    """
    try:
        registry = get_registry()
        actor_ip = ""
        if request is not None:
            client = request.client
            actor_ip = client.host if client else ""
        await registry.post_audit(
            actor="hub-auth",
            action="auth_failed",
            entity_type="source",
            entity_id=source_slug or "(unknown)",
            metadata={
                "error_code": error_code,
                "message": message,
                "actor_ip": actor_ip,
            },
        )
    except Exception as exc:
        logger.warning(
            "auth-failed audit-push failed (error_code=%s source=%s): %s",
            error_code,
            source_slug,
            exc,
        )

# Module-level shared client; created lazily on first request.
_registry: Optional[RegistryClient] = None


def get_registry() -> RegistryClient:
    """Lazily-instantiated shared registry client (httpx connection pool)."""
    global _registry
    if _registry is None:
        _registry = RegistryClient()
    return _registry


def set_registry(registry: Optional[RegistryClient]) -> None:
    """Test-helper. Set a custom registry (or None to reset)."""
    global _registry
    _registry = registry


@router.get("/v1/health")
async def health() -> dict:
    """Liveness endpoint for MC-side health-check."""
    return {
        "data": {
            "status": "healthy",
            "version": "0.1.0",
        }
    }


_hub_state: Optional[HubState] = None


async def get_hub_state() -> HubState:
    """Lazily-init shared hub_state DAO."""
    global _hub_state
    if _hub_state is None:
        _hub_state = await hub_state_connect()
        await _hub_state.migrate()
    return _hub_state


def set_hub_state(state: Optional[HubState]) -> None:
    """Test-helper."""
    global _hub_state
    _hub_state = state


async def _authenticate(request: Request, body: bytes) -> str:
    """Phase v4a auth: HMAC headers preferred, HUB_PILOT_TOKEN as transition fallback.

    Returns the source_slug after successful auth. Raises HTTPException on failure.
    """
    auth_header = request.headers.get("authorization", "")
    # Back-compat path: legacy pilot-token Bearer. Deprecated; remove in v4b.
    pilot_token = os.environ.get("HUB_PILOT_TOKEN", "")
    if pilot_token and auth_header.startswith("Bearer "):
        bearer = auth_header[len("Bearer "):].strip()
        from hmac import compare_digest
        if compare_digest(bearer.encode(), pilot_token.encode()):
            return ""  # empty source_slug → pipeline-side bypass

    # Phase v4a HMAC path
    ts = request.headers.get("x-hub-timestamp", "")
    nonce = request.headers.get("x-hub-nonce", "")
    sig = request.headers.get("x-hub-signature", "")
    if not (ts and nonce and sig):
        await _audit_auth_failed(
            "missing_auth",
            "Bearer HUB_PILOT_TOKEN or X-Hub-* headers required",
            request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "missing_auth", "message": "Bearer HUB_PILOT_TOKEN or X-Hub-* headers required"},
        )

    try:
        body_json = json.loads(body)
    except json.JSONDecodeError:
        await _audit_auth_failed("bad_body", "Body is not valid JSON", request=request)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "bad_body", "message": "Body is not valid JSON"},
        )
    source_slug = body_json.get("source_slug")
    if not source_slug:
        await _audit_auth_failed(
            "missing_source",
            "source_slug required in body for HMAC auth",
            request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "missing_source", "message": "source_slug required in body for HMAC auth"},
        )

    # Fetch source.hub_secret via registry. A transient registry outage
    # (revoked token / 5xx / network) must surface as 503 — NOT an uncaught
    # 500 — and is distinct from a clean unknown-source answer (None) below.
    try:
        source = await get_registry().get_source(source_slug)
    except RegistryUnavailable as exc:
        await _audit_auth_failed(
            "registry_unavailable",
            "Notification registry (Mission Control) is unavailable",
            source_slug=source_slug,
            request=request,
        )
        raise _registry_unavailable_exc() from exc
    if source is None:
        await _audit_auth_failed(
            "unknown_source",
            f"Source {source_slug} not registered",
            source_slug=source_slug,
            request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "unknown_source", "message": f"Source {source_slug} not registered"},
        )
    # Step-6 secret source: prefer the inline hub_secret (mc-mode), else fall
    # back to the local file-backed secret store (mirror-mode, where the
    # source-dict is secret-free). The fallback is short-circuited when the
    # inline secret is present, so the default mc-mode path is unchanged.
    secret = source.get("hub_secret") or get_secret_store().get_hub_secret(source_slug)
    if not secret:
        await _audit_auth_failed(
            "no_hub_secret",
            f"Source {source_slug} has no hub_secret configured",
            source_slug=source_slug,
            request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "no_hub_secret", "message": f"Source {source_slug} has no hub_secret configured"},
        )

    # Verify HMAC
    try:
        hmac_verify(secret.encode(), ts, nonce, body, sig)
    except HmacError as exc:
        await _audit_auth_failed(
            exc.error_code,
            exc.message,
            source_slug=source_slug,
            request=request,
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc

    # Replay-check via nonce-store
    state = await get_hub_state()
    if not await remember_or_replay(state, nonce, source_slug):
        await _audit_auth_failed(
            "replay_detected",
            f"Nonce {nonce} already used",
            source_slug=source_slug,
            request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "replay_detected", "message": f"Nonce {nonce} already used"},
        )

    return source_slug


@router.post("/v1/notifications")
async def notifications(request: Request) -> dict:
    """Run the 8-step dispatch pipeline for an inbound notification intent.

    Phase v4a auth: HMAC signed with per-source hub_secret OR HUB_PILOT_TOKEN
    Bearer as transition fallback. PipelineError → proper HTTPException.
    """
    body = await request.body()
    authed_source_slug = await _authenticate(request, body)  # raises HTTPException on auth-fail

    try:
        body_json = json.loads(body)
        intent = NotificationIntent.model_validate(body_json)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "schema_invalid", "message": str(exc)},
        ) from exc

    # Structural identity check: the HMAC-authenticated source_slug MUST match
    # the slug Pydantic parsed into the intent. Empty authed_source_slug means
    # the back-compat HUB_PILOT_TOKEN path was taken (no per-source identity);
    # only that path is allowed to dispatch as any source.
    if authed_source_slug and authed_source_slug != intent.source_slug:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "source_identity_mismatch",
                "message": "HMAC-authed source_slug does not match body intent.source_slug",
            },
        )

    registry = get_registry()
    try:
        result = await run_pipeline(
            intent,
            source_token_hash="",  # HMAC is the auth-of-truth; pipeline-side check is v4b
            registry=registry,
            state=await get_hub_state(),  # T4: write durable event-log row
        )
    except PipelineError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except RegistryUnavailable as exc:
        raise _registry_unavailable_exc() from exc

    # v4d-A Phase 0.5: persist per-channel dispatch results to MC so the
    # fingerprint -> provider_message_id round-trip exists for edit-in-place
    # lookups on subsequent firings. Best-effort: never blocks the response.
    if result.event_id and result.deliveries:
        deliveries_payload = [d.model_dump(exclude_none=False) for d in result.deliveries]
        await registry.persist_deliveries(
            event_id=result.event_id,
            deliveries=deliveries_payload,
        )

    return {"data": result.model_dump(exclude_none=True)}


@router.post("/v1/notifications/simulate")
async def simulate(request: Request) -> dict:
    """Dry-run routing simulator (Phase 2).

    Runs schema-validation + registry-lookup + source-scope-check + rule-match
    against the same registry the real pipeline uses, but skips dedup,
    cooldown, flapping, quiet-hours and dispatch. No adapter calls, no
    nonce burn, no audit-rows written.

    Use this to inspect *which* channels a candidate event would land on
    before sending it. Suppression-states (dedup/cooldown/quiet) are
    intentionally not simulated — they depend on runtime state and would
    bias future real-runs if probed read-only here.
    """
    from gateway.hub.pipeline import (
        PipelineError,
        validate_and_lookup,
        validate_scope,
    )
    from gateway.hub.rule_matcher import find_matching_rule

    body = await request.body()
    await _authenticate(request, body)

    try:
        body_json = json.loads(body)
        intent = NotificationIntent.model_validate(body_json)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "schema_invalid", "message": str(exc)},
        ) from exc

    registry = get_registry()
    trace: list[str] = ["received"]

    try:
        ctx = await validate_and_lookup(intent, "", registry)
        trace.append("validated")
    except PipelineError as exc:
        return {
            "data": {
                "would_match_rule": None,
                "would_deliver_to": [],
                "suppression": {"reason": exc.error_code, "message": exc.message},
                "audit_trace": trace,
            }
        }
    except RegistryUnavailable as exc:
        raise _registry_unavailable_exc() from exc

    try:
        await validate_scope(intent, ctx, registry)
        trace.append("scope_ok")
    except PipelineError as exc:
        return {
            "data": {
                "would_match_rule": None,
                "would_deliver_to": [],
                "suppression": {"reason": exc.error_code, "message": exc.message},
                "audit_trace": trace,
            }
        }

    try:
        rule, channel_set = await find_matching_rule(
            registry,
            topic=intent.topic,
            audience_slug=ctx.audience_slug,
            severity=intent.severity,
            urgency=intent.urgency,
            actionability=intent.actionability,
            source_id=ctx.source["id"],
        )
    except RegistryUnavailable as exc:
        raise _registry_unavailable_exc() from exc
    if rule is None or channel_set is None:
        trace.append("no_rule_match")
        return {
            "data": {
                "would_match_rule": None,
                "would_deliver_to": [],
                "suppression": {"reason": "no_rule_match"},
                "audit_trace": trace,
            }
        }

    trace.append("rule_matched")
    would_deliver_to = []
    for member in channel_set.get("members", []):
        channel = member.get("channel", {}) or {}
        if not channel.get("enabled", True):
            continue
        would_deliver_to.append(
            {
                "channel_id": channel.get("id"),
                "channel_slug": channel.get("slug"),
                "adapter": channel.get("type"),
                "required": bool(member.get("required", False)),
            }
        )

    return {
        "data": {
            "would_match_rule": rule.get("slug") or rule.get("id"),
            "would_deliver_to": would_deliver_to,
            "suppression": None,
            "audit_trace": trace,
        }
    }


def _require_pilot_token(request: Request) -> None:
    """Admin guard: a valid HUB_PILOT_TOKEN Bearer is mandatory.

    Reuses the same constant-time compare_digest check as the back-compat
    notifications path (~:140-144). Unlike that path, the pilot token here is
    the ONLY accepted credential — admin endpoints are operator-only and have
    no HMAC fallback. Raises 401 on any mismatch/missing token.
    """
    from hmac import compare_digest

    pilot_token = os.environ.get("HUB_PILOT_TOKEN", "")
    if not pilot_token:
        # No configured token → fail closed: admin endpoints stay locked rather
        # than implicitly accepting any/empty Bearer. (Behaviour matches the
        # `pilot_token and ...` short-circuit below; made explicit for intent.)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "missing_auth", "message": "Bearer HUB_PILOT_TOKEN required"},
        )
    auth_header = request.headers.get("authorization", "")
    if pilot_token and auth_header.startswith("Bearer "):
        bearer = auth_header[len("Bearer "):].strip()
        if compare_digest(bearer.encode(), pilot_token.encode()):
            return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error_code": "missing_auth", "message": "Bearer HUB_PILOT_TOKEN required"},
    )


@router.post("/v1/admin/registry-sync-now")
async def registry_sync_now(request: Request) -> dict:
    """Operator-triggered full-table registry mirror sync (Phase 6b Step 4b).

    Pulls the entire notification registry from Mission Control into the local
    SQLite mirror and returns per-table row counts. A transient MC outage
    surfaces as a typed 503 `registry_unavailable` (never an uncaught 500),
    consistent with the read path's channel-isolation contract.

    Read-only to dispatch: the mirror is not yet consulted at send time (that
    is Step 5), so triggering a sync cannot change routing behaviour.
    """
    _require_pilot_token(request)

    sync = RegistrySync(get_registry(), await get_hub_state())
    try:
        counts = await sync.sync_once()
    except RegistryUnavailable as exc:
        raise _registry_unavailable_exc() from exc

    return {"data": counts}
