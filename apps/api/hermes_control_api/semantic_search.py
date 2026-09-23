"""Private derived search data. Hermes remains authoritative; no runtimes are resumed."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from uuid import uuid4

import httpx
import numpy as np
import tiktoken
from hermes_client import EventNormalizer, project_attachment_prompt, project_email_reference_prompt
from hermes_client.history import project_history_message
from hermes_client.provider import SessionHistoryNotFound
from sqlalchemy import delete, func, or_, select, update

from .integrations import IntegrationError
from .models import (Gateway, LiveTranscript, ProfileRef, SemanticFragment, SemanticIndexState,
                     SemanticPreference, SessionLink, User, UserIntegration, utc_now)
from .openai_live import OpenAIIntegrationService
from .ownership import active_gateway_filter
from .supervision import SupervisorHealth
from .vision_context import project_camera_prompt

MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
INDEX_VERSION = 1
LOG = logging.getLogger(__name__)
PAUSE_ERRORS = {"OPENAI_NOT_CONFIGURED", "OPENAI_SECRET_UNAVAILABLE", "SEMANTIC_AUTH", "SEMANTIC_QUOTA"}


def failure(code: str, status: int = 503) -> IntegrationError:
    return IntegrationError(status_code=status, code=code, message="Semantic search is temporarily unavailable", retryable=code not in PAUSE_ERRORS)


@lru_cache(maxsize=1)
def encoding():
    return tiktoken.get_encoding("cl100k_base")


def chunks(text: str):
    tokens = encoding().encode(text.strip(), disallowed_special=())
    offset = 0
    while offset < len(tokens):
        end = min(offset + 700, len(tokens))
        part = encoding().decode(tokens[offset:end], errors="ignore")
        # Re-tokenizing a sliced token can add a token at the left boundary.
        # Preserve whitespace and enforce the limit on the actual API input.
        while len(encoding().encode(part, disallowed_special=())) > 700:
            end -= 1
            part = encoding().decode(tokens[offset:end], errors="ignore")
        if part.strip():
            yield part
        if end >= len(tokens):
            break
        offset = max(offset + 1, end - 100)


def public_text(rows: list[dict], gateway_id: str, profile: str) -> str:
    normalizer = EventNormalizer(gateway_id=gateway_id, profile_name=profile)
    parts = []
    for raw in rows:
        row = project_history_message(raw)
        if not isinstance(row, dict) or row.get("role") not in {"user", "assistant"}:
            continue
        if (row.get("channel") not in (None, "", "final", "final_answer")
                or row.get("phase") not in (None, "", "final", "final_answer")):
            continue
        text = row.get("content") or row.get("text")
        if not isinstance(text, str):
            continue
        text, _ = project_attachment_prompt(project_camera_prompt(text))
        text, _ = project_email_reference_prompt(text)
        # Projection is applied per chunk: the public sanitizer deliberately
        # truncates long strings and must not silently discard old message text.
        tokens = encoding().encode(text, disallowed_special=())
        for start in range(0, len(tokens), 700):
            part = encoding().decode(tokens[start:start + 700])
            safe = normalizer.sanitize_data({"role": row["role"], "content": part})
            if isinstance(safe, dict) and not safe.get("omitted") and safe.get("content"):
                parts.append(f"{row['role']}: {safe['content']}")
    return "\n\n".join(parts)


def normalized_vector(value) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float32)
    if vector.shape != (DIMENSIONS,) or not np.isfinite(vector).all():
        raise failure("SEMANTIC_INVALID_RESPONSE", 502)
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm <= 0:
        raise failure("SEMANTIC_INVALID_RESPONSE", 502)
    return vector / norm


class EmbeddingsClient:
    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self.http_client = http_client

    async def embed(self, key: str, texts: list[str]) -> list[np.ndarray]:
        if not texts or len(texts) > 32:
            raise ValueError("Embedding batch must contain 1–32 inputs")
        started = time.monotonic()
        client = self.http_client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        try:
            async with client.stream("POST", "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {key}", "Accept-Encoding": "identity"},
                    json={"model": MODEL, "dimensions": DIMENSIONS, "encoding_format": "float", "input": texts}) as response:
                if response.headers.get("content-encoding", "identity") not in {"", "identity"}:
                    raise failure("SEMANTIC_INVALID_RESPONSE", 502)
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 2 * 1024 * 1024:
                        raise failure("SEMANTIC_INVALID_RESPONSE", 502)
                if response.status_code in {401, 403}:
                    raise failure("SEMANTIC_AUTH", 409)
                if response.status_code == 429:
                    try:
                        error = json.loads(body).get("error", {}).get("code")
                    except (ValueError, AttributeError):
                        error = None
                    raise failure("SEMANTIC_QUOTA" if error in {"insufficient_quota", "billing_hard_limit_reached"} else "SEMANTIC_RATE_LIMIT", 429)
                if response.status_code != 200:
                    raise failure("SEMANTIC_PROVIDER", 502)
            try:
                payload = json.loads(body)
                data = payload["data"]
                if len(data) != len(texts) or sorted(item["index"] for item in data) != list(range(len(texts))):
                    raise ValueError("Invalid embedding indices")
                result = [normalized_vector(item["embedding"]) for item in sorted(data, key=lambda item: item["index"])]
                usage = int(payload.get("usage", {}).get("total_tokens", 0))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise failure("SEMANTIC_INVALID_RESPONSE", 502) from None
            LOG.info("semantic_embedding inputs=%d tokens=%d duration_ms=%d", len(texts), usage, (time.monotonic() - started) * 1000)
            return result
        except httpx.HTTPError:
            raise failure("SEMANTIC_NETWORK") from None
        finally:
            if self.http_client is None:
                await client.aclose()


@dataclass(frozen=True)
class Job:
    session_id: str
    owner_id: str
    source_version: str
    generation: str
    lease: str
    history_offset: int
    history_complete: bool
    live_offset: int


class SemanticSearch:
    def __init__(self, services, client=None):
        self.services = services
        self.factory = services.session_factory
        self.vault = services.vault
        self.credentials = OpenAIIntegrationService(self.vault)
        self.client = client or EmbeddingsClient()
        self.health = SupervisorHealth(stale_after_seconds=300)
        self.query_cache: OrderedDict = OrderedDict()
        self.query_locks: dict[str, asyncio.Lock] = {}
        self.search_slots = asyncio.Semaphore(2)
        self.query_times: dict[str, list[float]] = {}
        self.paused = lambda: False
        self.activated = False

    def initialize(self):
        # Run before accepting requests. Until someone opts in there is no
        # indexing work, and no reason to open competing background transactions
        # (especially on a single-connection in-memory SQLite database).
        with self.factory() as db:
            if db.scalar(select(SemanticPreference.owner_id).where(SemanticPreference.enabled.is_(True)).limit(1)):
                self.activated = True

    def eligible(self, owner_id=None):
        query = select(SessionLink).join(User, User.id == SessionLink.owner_id).where(
            User.is_active.is_(True), SessionLink.chat_mode != "temporary", SessionLink.archived_at.is_(None),
            active_gateway_filter(SessionLink.gateway_id, cloud=self.services.settings.deployment_mode == "cloud",
                                  owner_id=owner_id if owner_id else SessionLink.owner_id))
        return query.where(SessionLink.owner_id == owner_id) if owner_id else query

    def source_version(self, db, row, transcripts=None):
        if transcripts is None:
            transcripts = db.execute(select(func.count(LiveTranscript.id), func.coalesce(func.sum(LiveTranscript.revision), 0))
                .where(LiveTranscript.session_link_id == row.id, LiveTranscript.owner_id == row.owner_id)).one()
        return hashlib.sha256(json.dumps([row.updated_at.isoformat(), row.last_sequence,
            row.display_title or row.title, row.gateway_id, row.profile_name, *transcripts,
            MODEL, DIMENSIONS, INDEX_VERSION]).encode()).hexdigest()

    def _credential_version(self, db, owner):
        ciphertext = db.scalar(select(UserIntegration.api_key_ciphertext).where(
            UserIntegration.owner_id == owner, UserIntegration.provider == "openai"))
        return hashlib.sha256((ciphertext or "").encode()).hexdigest()

    def key(self, owner_id):
        with self.factory() as db:
            user = db.get(User, owner_id)
            preference = db.get(SemanticPreference, owner_id)
            if not user or not user.is_active or not preference or not preference.enabled:
                raise failure("SEMANTIC_DISABLED", 409)
            if preference.error_code in PAUSE_ERRORS and preference.credential_version == self._credential_version(db, owner_id):
                raise failure(preference.error_code, 409)
            return self.credentials.api_key(db, user)

    def settings(self, owner_id, enabled):
        with self.factory() as db:
            owner = db.get(User, owner_id)
            if enabled:
                self.credentials.api_key(db, owner)
            preference = db.get(SemanticPreference, owner_id)
            if preference is None:
                preference = SemanticPreference(owner_id=owner_id)
                db.add(preference)
            preference.enabled = enabled
            preference.error_code = None
            preference.credential_version = self._credential_version(db, owner_id)
            db.execute(update(SemanticIndexState).where(SemanticIndexState.owner_id == owner_id)
                       .values(retry_at=None, error_code=None, lease_until=None, lease_token=None))
            db.commit()
        if enabled:
            self.activated = True
        return self.status(owner_id)

    def status(self, owner_id):
        with self.factory() as db:
            preference = db.get(SemanticPreference, owner_id)
            enabled = bool(preference and preference.enabled)
            configured = self.credentials.configured(db, db.get(User, owner_id))
            rows = db.scalars(self.eligible(owner_id)).all()
            states = {row.session_link_id: row for row in db.scalars(select(SemanticIndexState).where(SemanticIndexState.owner_id == owner_id))}
            transcripts = {session: (count, revision) for session, count, revision in db.execute(
                select(LiveTranscript.session_link_id, func.count(LiveTranscript.id), func.sum(LiveTranscript.revision))
                .where(LiveTranscript.owner_id == owner_id).group_by(LiveTranscript.session_link_id))}
            indexed = sum(bool((state := states.get(row.id)) and state.active_generation
                               and state.indexed_version == self.source_version(db, row, transcripts.get(row.id, (0, 0)))) for row in rows)
            failed = sum(bool(states.get(row.id) and states[row.id].error_code) for row in rows)
            error = (preference.error_code if preference else None) or next(
                (states[row.id].error_code for row in rows if row.id in states and states[row.id].error_code), None)
            if enabled and not configured:
                error = "OPENAI_NOT_CONFIGURED"
            pending = len(rows) - indexed
            revision = hashlib.sha256(json.dumps(sorted((row.id, states[row.id].active_generation)
                for row in rows if row.id in states)).encode()).hexdigest()[:16]
            return {"enabled": enabled, "configured": configured, "total": len(rows), "indexed": indexed,
                    "pending": pending, "failed": failed, "errorCode": error, "revision": revision,
                    "state": "disabled" if not enabled else "blocked" if error in PAUSE_ERRORS else "indexing" if pending else "ready"}

    def reconcile(self):
        with self.factory() as db:
            preferences = {p.owner_id: p for p in db.scalars(select(SemanticPreference).where(SemanticPreference.enabled.is_(True)))}
            if not preferences:
                return
            for owner_id, preference in preferences.items():
                current = self._credential_version(db, owner_id)
                if current != preference.credential_version:
                    preference.credential_version, preference.error_code = current, None
                    db.execute(update(SemanticIndexState).where(SemanticIndexState.owner_id == owner_id)
                               .values(retry_at=None, error_code=None))
            transcripts = {session: (count, revision) for session, count, revision in db.execute(
                select(LiveTranscript.session_link_id, func.count(LiveTranscript.id), func.sum(LiveTranscript.revision))
                .where(LiveTranscript.owner_id.in_(preferences)).group_by(LiveTranscript.session_link_id))}
            states = {state.session_link_id: state for state in db.scalars(select(SemanticIndexState)
                .where(SemanticIndexState.owner_id.in_(preferences)))}
            for row in db.scalars(self.eligible().where(SessionLink.owner_id.in_(preferences))):
                version = self.source_version(db, row, transcripts.get(row.id, (0, 0)))
                state = states.get(row.id)
                if state is None:
                    db.add(SemanticIndexState(session_link_id=row.id, owner_id=row.owner_id, source_version=version))
                elif state.source_version != version:
                    state.source_version = version
                    state.building_generation = None
                    state.history_offset, state.live_offset, state.history_complete = 0, 0, False
                    state.history_tail_ciphertext = None
                    state.retry_at, state.error_code, state.lease_until, state.lease_token = None, None, None, None
                    state.attempts = 0
                    db.execute(delete(SemanticFragment).where(SemanticFragment.session_link_id == row.id,
                        SemanticFragment.generation != (state.active_generation or "")))
            db.commit()

    def claim(self):
        now = utc_now()
        with self.factory() as db:
            query = (select(SemanticIndexState).join(SessionLink, SessionLink.id == SemanticIndexState.session_link_id)
                .join(SemanticPreference, SemanticPreference.owner_id == SemanticIndexState.owner_id)
                .where(SessionLink.id.in_(self.eligible().with_only_columns(SessionLink.id)),
                       SemanticPreference.enabled.is_(True), SemanticPreference.error_code.is_(None),
                       or_(SemanticIndexState.retry_at.is_(None), SemanticIndexState.retry_at <= now),
                       or_(SemanticIndexState.lease_until.is_(None), SemanticIndexState.lease_until <= now),
                       or_(SemanticIndexState.indexed_version.is_(None),
                           SemanticIndexState.indexed_version != SemanticIndexState.source_version,
                           SemanticIndexState.building_generation.is_not(None),
                           SemanticIndexState.checked_at < now - timedelta(minutes=5)))
                .order_by(SemanticIndexState.checked_at.asc().nullsfirst(), SemanticIndexState.session_link_id)
                .limit(1).with_for_update(skip_locked=True))
            state = db.scalar(query)
            if state is None:
                return None
            lease = str(uuid4())
            # Conditional claim also works on SQLite, where FOR UPDATE is ignored.
            claimed = db.execute(update(SemanticIndexState).where(SemanticIndexState.session_link_id == state.session_link_id,
                or_(SemanticIndexState.lease_until.is_(None), SemanticIndexState.lease_until <= now))
                .values(lease_token=lease, lease_until=now + timedelta(minutes=5)))
            if claimed.rowcount != 1:
                db.rollback()
                return None
            if not state.building_generation:
                state.building_generation = str(uuid4())
                state.history_offset, state.live_offset, state.history_complete = 0, 0, False
                state.history_tail_ciphertext = None
            state.checked_at = now
            db.commit()
            return Job(state.session_link_id, state.owner_id, state.source_version, state.building_generation,
                       lease, state.history_offset, state.history_complete, state.live_offset)

    def valid(self, db, job, *, lock=False):
        state = db.get(SemanticIndexState, job.session_id, with_for_update=lock)
        row = db.scalar(self.eligible(job.owner_id).where(SessionLink.id == job.session_id))
        preference = db.get(SemanticPreference, job.owner_id)
        if (not row or not state or not preference or not preference.enabled
                or state.lease_token != job.lease or state.building_generation != job.generation
                or self.source_version(db, row) != job.source_version):
            return None
        return row, state

    def aad(self, owner, session):
        return f"semantic-search:{owner}:{session}:{MODEL}:{INDEX_VERSION}"

    def prepare(self, job, source, text):
        texts = list(dict.fromkeys(chunks(text)))
        with self.factory() as db:
            if not self.valid(db, job):
                return []
            result = []
            for text in texts:
                digest = hashlib.sha256(text.encode()).hexdigest()
                existing = db.scalar(select(SemanticFragment).where(SemanticFragment.session_link_id == job.session_id,
                    SemanticFragment.owner_id == job.owner_id, SemanticFragment.source == source,
                    SemanticFragment.content_hash == digest, SemanticFragment.model == MODEL,
                    SemanticFragment.dimensions == DIMENSIONS, SemanticFragment.version == INDEX_VERSION)
                    .order_by((SemanticFragment.generation == job.generation).desc()).limit(1))
                if existing and existing.generation == job.generation:
                    continue
                result.append((text, digest, existing.payload_ciphertext if existing else None))
            return result

    def save_fragments(self, job, source, prepared):
        with self.factory() as db:
            valid = self.valid(db, job, lock=True)
            if not valid:
                return False
            valid[1].lease_until = utc_now() + timedelta(minutes=5)
            for text, digest, ciphertext, vector in prepared:
                if ciphertext is None:
                    payload = json.dumps({"text": text, "vector": base64.b64encode(vector.astype("<f4").tobytes()).decode()})
                    ciphertext = self.vault.encrypt(payload, aad=self.aad(job.owner_id, job.session_id))
                exists = db.scalar(select(SemanticFragment.id).where(SemanticFragment.session_link_id == job.session_id,
                    SemanticFragment.generation == job.generation, SemanticFragment.source == source,
                    SemanticFragment.content_hash == digest))
                if not exists:
                    db.add(SemanticFragment(owner_id=job.owner_id, session_link_id=job.session_id,
                        generation=job.generation, source=source, content_hash=digest, model=MODEL,
                        dimensions=DIMENSIONS, version=INDEX_VERSION, payload_ciphertext=ciphertext))
            db.commit()
            return True

    async def index_text(self, job, key, source, text):
        prepared = await asyncio.to_thread(self.prepare, job, source, text)
        for start in range(0, len(prepared), 32):
            if self.paused():
                raise failure("SEMANTIC_PAUSED")
            batch = prepared[start:start + 32]
            missing = [text for text, _, ciphertext in batch if ciphertext is None]
            if missing:
                # Recheck activation/credential immediately before each paid request.
                key = await asyncio.to_thread(self.key, job.owner_id)
                vectors = iter(await self.client.embed(key, missing))
            else:
                vectors = iter(())
            records = [(text, digest, ciphertext, None if ciphertext else next(vectors)) for text, digest, ciphertext in batch]
            if not await asyncio.to_thread(self.save_fragments, job, source, records):
                return

    def finish_page(self, job, *, offset=None, complete=None, tail_ciphertext=None, live_offset=None, publish=False):
        with self.factory() as db:
            valid = self.valid(db, job, lock=True)
            if not valid:
                return
            _, state = valid
            if offset is not None:
                state.history_offset, state.history_complete = offset, complete
                state.history_tail_ciphertext = tail_ciphertext
            if live_offset is not None:
                state.live_offset = live_offset
            if publish:
                state.active_generation, state.indexed_version = job.generation, job.source_version
                state.building_generation = None
                db.execute(delete(SemanticFragment).where(SemanticFragment.session_link_id == job.session_id,
                    SemanticFragment.generation != job.generation))
            state.lease_until, state.lease_token, state.retry_at, state.error_code = None, None, None, None
            state.attempts = 0
            db.commit()

    async def process(self, job):
        from .services import GatewayService
        key = await asyncio.to_thread(self.key, job.owner_id)
        with self.factory() as db:
            valid = self.valid(db, job)
            if not valid:
                return
            row, state = valid
            tail_ciphertext = state.history_tail_ciphertext
            title = row.display_title or row.title or ""
            gateway_id, profile = row.gateway_id, row.profile_name
            stored_id, initially_empty = row.stored_session_id, row.initial_history_pending
            gateway = db.get(Gateway, gateway_id)
            connector = gateway.transport_kind == "connector"
            if not job.history_complete:
                connection = await GatewayService(self.services).connection(db, gateway_id, profile)
        if not job.history_complete:
            provider = await self.services.provider_pool.get(connection)
            if connector and not (await provider.capabilities()).supports("connector.historyPageV1"):
                raise failure("SEMANTIC_CONNECTOR_UPDATE", 409)
            try:
                page = await provider.history_page(stored_id, offset=job.history_offset, limit=100)
            except SessionHistoryNotFound:
                if not initially_empty:
                    raise
                page = {"messages": [], "next_offset": 0, "complete": True}
            if (not isinstance(page, dict) or not isinstance(page.get("messages"), list)
                    or len(page["messages"]) > 100 or type(page.get("complete")) is not bool
                    or type(page.get("next_offset")) is not int
                    or page["next_offset"] != job.history_offset + len(page["messages"])
                    or (not page["complete"] and not page["messages"])):
                raise failure("SEMANTIC_HISTORY_INVALID", 502)
            text = await asyncio.to_thread(public_text, page["messages"], gateway_id, profile)
            tail_aad = f"semantic-history-tail:{job.owner_id}:{job.session_id}:{INDEX_VERSION}"
            if tail_ciphertext:
                text = self.vault.decrypt(tail_ciphertext, aad=tail_aad) + "\n\n" + text
            await self.index_text(job, key, "text", text)
            def next_tail():
                if page["complete"] or not text.strip():
                    return None
                tail = encoding().decode(encoding().encode(text, disallowed_special=())[-100:], errors="ignore")
                return self.vault.encrypt(tail, aad=tail_aad)
            tail_ciphertext = await asyncio.to_thread(next_tail)
            await asyncio.to_thread(self.finish_page, job, offset=page["next_offset"], complete=page["complete"],
                                    tail_ciphertext=tail_ciphertext)
            return
        def live_page():
            with self.factory() as db:
                return list(db.scalars(select(LiveTranscript).where(LiveTranscript.session_link_id == job.session_id,
                    LiveTranscript.owner_id == job.owner_id).order_by(LiveTranscript.id).offset(job.live_offset).limit(5)))
        transcripts = await asyncio.to_thread(live_page)
        for transcript in transcripts:
            def live_text():
                raw = self.vault.decrypt(transcript.payload_ciphertext,
                    aad=f"live-transcript:{job.owner_id}:{job.session_id}:{transcript.id}")
                parts = json.loads(raw)["fragments"]
                return public_text([{"role": part["role"], "content": part["text"]} for part in parts], gateway_id, profile)
            await self.index_text(job, key, "live", await asyncio.to_thread(live_text))
        if len(transcripts) < 5:
            normalizer = EventNormalizer(gateway_id=gateway_id, profile_name=profile)
            await self.index_text(job, key, "title", normalizer.sanitize_data(title))
        await asyncio.to_thread(self.finish_page, job, live_offset=job.live_offset + len(transcripts), publish=len(transcripts) < 5)

    def record_error(self, job, code):
        with self.factory() as db:
            state = db.get(SemanticIndexState, job.session_id)
            if not state or state.lease_token != job.lease:
                return
            state.attempts += 1
            state.error_code, state.lease_until, state.lease_token = code, None, None
            state.retry_at = utc_now() + timedelta(seconds=min(3600, 15 * 2 ** min(state.attempts, 8)))
            preference = db.get(SemanticPreference, job.owner_id)
            if preference and code in PAUSE_ERRORS:
                preference.error_code = code
            db.commit()
        LOG.warning("semantic_index_error code=%s", code)

    async def tick(self):
        if self.paused():
            return
        await asyncio.to_thread(self.reconcile)
        job = await asyncio.to_thread(self.claim)
        if job:
            try:
                await self.process(job)
            except asyncio.CancelledError:
                # Leave the lease and checkpoint durable for the next process.
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, IntegrationError) else "SEMANTIC_CONNECTION" if isinstance(exc, (ConnectionError, TimeoutError, httpx.HTTPError)) else "SEMANTIC_INDEX_ERROR"
                await asyncio.to_thread(self.record_error, job, code)

    async def run(self):
        while True:
            self.health.mark_attempt()
            try:
                if self.activated:
                    await self.tick()
                self.health.mark_success()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health.mark_failure()
                LOG.warning("semantic_supervisor_error")
            await asyncio.sleep(2)

    async def query_vector(self, owner_id, text):
        key = await asyncio.to_thread(self.key, owner_id)
        digest = hashlib.sha256(unicodedata.normalize("NFC", " ".join(text.split())).encode()).hexdigest()
        cache_key = (owner_id, MODEL, DIMENSIONS, digest)
        lock = self.query_locks.setdefault(owner_id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self.query_cache.get(cache_key)
            if cached and now - cached[0] < 600:
                self.query_cache.move_to_end(cache_key)
                return cached[1]
            recent = [at for at in self.query_times.get(owner_id, []) if now - at < 60]
            if len(recent) >= 30:
                raise failure("SEMANTIC_RATE_LIMIT", 429)
            self.query_times[owner_id] = recent + [now]
            vector = (await self.client.embed(key, [text]))[0]
            self.query_cache[cache_key] = (now, vector)
            while len(self.query_cache) > 256:
                self.query_cache.popitem(last=False)
            return vector

    def rank(self, owner_id, vector, limit):
        with self.factory() as db:
            # Authorization joins are repeated here after the OpenAI round trip.
            preference = db.get(SemanticPreference, owner_id)
            if not preference or not preference.enabled:
                raise failure("SEMANTIC_DISABLED", 409)
            eligible = self.eligible(owner_id).with_only_columns(SessionLink.id)
            query = select(SemanticFragment).join(SemanticIndexState,
                (SemanticIndexState.session_link_id == SemanticFragment.session_link_id)
                & (SemanticIndexState.active_generation == SemanticFragment.generation)).where(
                    SemanticFragment.owner_id == owner_id, SemanticFragment.session_link_id.in_(eligible),
                    SemanticFragment.model == MODEL, SemanticFragment.dimensions == DIMENSIONS,
                    SemanticFragment.version == INDEX_VERSION).execution_options(yield_per=128)
            best = {}
            for batch in db.scalars(query).partitions(128):
                payloads = [json.loads(self.vault.decrypt(fragment.payload_ciphertext,
                    aad=self.aad(owner_id, fragment.session_link_id))) for fragment in batch]
                matrix = np.stack([np.frombuffer(base64.b64decode(payload["vector"]), dtype="<f4") for payload in payloads])
                scores = matrix @ vector
                for fragment, payload, score in zip(batch, payloads, scores, strict=True):
                    previous = best.get(fragment.session_link_id)
                    if previous is None or float(score) > previous[0]:
                        best[fragment.session_link_id] = (float(score), payload["text"], fragment.source)
            ordered = sorted(best, key=lambda session: (-best[session][0], session))[:min(limit, 20)]
            items = []
            for session_id in ordered:
                row = db.scalar(self.eligible(owner_id).where(SessionLink.id == session_id))
                if row is None:
                    continue
                profile = db.scalar(select(ProfileRef.display_name).where(ProfileRef.gateway_id == row.gateway_id,
                    ProfileRef.profile_name == row.profile_name))
                score, excerpt, source = best[session_id]
                items.append({"id": f"semantic:{session_id}", "targetId": session_id, "kind": "session",
                    "title": row.display_title or row.title or "Conversation", "excerpt": excerpt[:240],
                    "meta": f"{profile or row.profile_name} · {row.last_activity_at.isoformat()}",
                    "source": "live" if source == "live" else "text"})
            status = self.status(owner_id)
            return {"items": items, "partial": status["pending"] > 0 or status["failed"] > 0}

    async def search(self, owner_id, text, limit=20):
        if not 2 <= len(text.strip()) <= 200:
            raise failure("SEMANTIC_QUERY_INVALID", 422)
        async with self.search_slots:
            try:
                vector = await self.query_vector(owner_id, text)
            except IntegrationError as exc:
                if exc.code in PAUSE_ERRORS:
                    def pause():
                        with self.factory() as db:
                            preference = db.get(SemanticPreference, owner_id)
                            if preference:
                                preference.error_code = exc.code
                                db.commit()
                    await asyncio.to_thread(pause)
                raise
            return await asyncio.to_thread(self.rank, owner_id, vector, limit)
