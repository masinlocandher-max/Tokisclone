from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return value or "untitled"


def new_id() -> str:
    return str(uuid.uuid4())


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dramas (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT,
    poster_url TEXT,
    language TEXT,
    genres_json TEXT NOT NULL DEFAULT '[]',
    episode_count INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    drama_id TEXT NOT NULL REFERENCES dramas(id) ON DELETE CASCADE,
    platform TEXT NOT NULL COLLATE NOCASE,
    external_id TEXT,
    source_url TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_checked TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS sources_platform_external_uidx
ON sources(platform, external_id)
WHERE external_id IS NOT NULL AND external_id <> '';

CREATE UNIQUE INDEX IF NOT EXISTS sources_platform_url_uidx
ON sources(platform, source_url)
WHERE source_url IS NOT NULL AND source_url <> '';

CREATE INDEX IF NOT EXISTS sources_drama_idx ON sources(drama_id);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    drama_id TEXT NOT NULL REFERENCES dramas(id) ON DELETE CASCADE,
    episode_number INTEGER NOT NULL CHECK (episode_number > 0),
    title TEXT,
    duration_seconds REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(drama_id, episode_number)
);

CREATE TABLE IF NOT EXISTS episode_sources (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    playback_url TEXT,
    status TEXT NOT NULL DEFAULT 'available',
    last_checked TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(episode_id, source_id)
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    storage_provider TEXT NOT NULL COLLATE NOCASE,
    provider_file_id TEXT NOT NULL,
    provider_path TEXT,
    sha256 TEXT,
    bytes INTEGER,
    mime_type TEXT,
    verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    verification_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(storage_provider, provider_file_id)
);

CREATE INDEX IF NOT EXISTS files_episode_idx ON files(episode_id);
CREATE INDEX IF NOT EXISTS files_sha256_idx ON files(sha256);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    drama_id TEXT REFERENCES dramas(id) ON DELETE SET NULL,
    source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
    episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ingestion_jobs_state_idx ON ingestion_jobs(state, created_at);
"""


class LibraryDB:
    """SQLite source-of-truth catalogue for Tokisclone's private media library."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("TOKISCLONE_LIBRARY_DB", "tokisclone_library.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _init_schema(self) -> None:
        with self.connect() as con:
            con.executescript(SCHEMA)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def upsert_drama(
        self,
        *,
        title: str,
        slug: str | None = None,
        description: str | None = None,
        poster_url: str | None = None,
        language: str | None = None,
        genres: list[str] | None = None,
        episode_count: int | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        slug = slugify(slug or title)
        now = utcnow()
        genres_json = json.dumps(genres or [], ensure_ascii=False)

        with self.connect() as con:
            existing = con.execute("SELECT * FROM dramas WHERE slug = ?", (slug,)).fetchone()
            if existing:
                con.execute(
                    """
                    UPDATE dramas
                    SET title = ?, description = COALESCE(?, description),
                        poster_url = COALESCE(?, poster_url), language = COALESCE(?, language),
                        genres_json = ?, episode_count = COALESCE(?, episode_count),
                        status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        description,
                        poster_url,
                        language,
                        genres_json,
                        episode_count,
                        status,
                        now,
                        existing["id"],
                    ),
                )
                row = con.execute("SELECT * FROM dramas WHERE id = ?", (existing["id"],)).fetchone()
            else:
                drama_id = new_id()
                con.execute(
                    """
                    INSERT INTO dramas
                    (id, title, slug, description, poster_url, language, genres_json,
                     episode_count, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        drama_id,
                        title,
                        slug,
                        description,
                        poster_url,
                        language,
                        genres_json,
                        episode_count,
                        status,
                        now,
                        now,
                    ),
                )
                row = con.execute("SELECT * FROM dramas WHERE id = ?", (drama_id,)).fetchone()
        result = self._row(row) or {}
        result["genres"] = json.loads(result.pop("genres_json", "[]"))
        return result

    def add_source(
        self,
        *,
        drama_id: str,
        platform: str,
        external_id: str | None = None,
        source_url: str | None = None,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        platform = platform.strip().lower()
        if not platform:
            raise ValueError("platform is required")
        if not external_id and not source_url:
            raise ValueError("external_id or source_url is required")
        now = utcnow()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self.connect() as con:
            existing = None
            if external_id:
                existing = con.execute(
                    "SELECT * FROM sources WHERE platform = ? AND external_id = ?",
                    (platform, external_id),
                ).fetchone()
            if existing is None and source_url:
                existing = con.execute(
                    "SELECT * FROM sources WHERE platform = ? AND source_url = ?",
                    (platform, source_url),
                ).fetchone()

            if existing:
                if existing["drama_id"] != drama_id:
                    raise ValueError("source already belongs to a different drama")
                con.execute(
                    """
                    UPDATE sources
                    SET external_id = COALESCE(?, external_id), source_url = COALESCE(?, source_url),
                        status = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (external_id, source_url, status, metadata_json, now, existing["id"]),
                )
                source_id = existing["id"]
            else:
                source_id = new_id()
                con.execute(
                    """
                    INSERT INTO sources
                    (id, drama_id, platform, external_id, source_url, status, last_checked,
                     metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (source_id, drama_id, platform, external_id, source_url, status, metadata_json, now, now),
                )
            row = con.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        result = self._row(row) or {}
        result["metadata"] = json.loads(result.pop("metadata_json", "{}"))
        return result

    def upsert_episode(
        self,
        *,
        drama_id: str,
        episode_number: int,
        title: str | None = None,
        duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        episode_number = int(episode_number)
        if episode_number <= 0:
            raise ValueError("episode_number must be positive")
        now = utcnow()

        with self.connect() as con:
            existing = con.execute(
                "SELECT * FROM episodes WHERE drama_id = ? AND episode_number = ?",
                (drama_id, episode_number),
            ).fetchone()
            if existing:
                con.execute(
                    """
                    UPDATE episodes
                    SET title = COALESCE(?, title), duration_seconds = COALESCE(?, duration_seconds),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (title, duration_seconds, now, existing["id"]),
                )
                episode_id = existing["id"]
            else:
                episode_id = new_id()
                con.execute(
                    """
                    INSERT INTO episodes
                    (id, drama_id, episode_number, title, duration_seconds, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (episode_id, drama_id, episode_number, title, duration_seconds, now, now),
                )
            row = con.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        return self._row(row) or {}

    def link_episode_source(
        self,
        *,
        episode_id: str,
        source_id: str,
        playback_url: str | None = None,
        status: str = "available",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self.connect() as con:
            existing = con.execute(
                "SELECT * FROM episode_sources WHERE episode_id = ? AND source_id = ?",
                (episode_id, source_id),
            ).fetchone()
            if existing:
                con.execute(
                    """
                    UPDATE episode_sources
                    SET playback_url = COALESCE(?, playback_url), status = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (playback_url, status, metadata_json, now, existing["id"]),
                )
                link_id = existing["id"]
            else:
                link_id = new_id()
                con.execute(
                    """
                    INSERT INTO episode_sources
                    (id, episode_id, source_id, playback_url, status, last_checked,
                     metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (link_id, episode_id, source_id, playback_url, status, metadata_json, now, now),
                )
            row = con.execute("SELECT * FROM episode_sources WHERE id = ?", (link_id,)).fetchone()
        result = self._row(row) or {}
        result["metadata"] = json.loads(result.pop("metadata_json", "{}"))
        return result

    def find_file_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM files WHERE sha256 = ? AND verified = 1 ORDER BY created_at LIMIT 1",
                (sha256.lower(),),
            ).fetchone()
        result = self._row(row)
        if result:
            result["verified"] = bool(result["verified"])
            result["verification"] = json.loads(result.pop("verification_json", "{}"))
        return result

    def register_file(
        self,
        *,
        episode_id: str,
        storage_provider: str,
        provider_file_id: str,
        provider_path: str | None = None,
        sha256: str | None = None,
        bytes_size: int | None = None,
        mime_type: str | None = None,
        verified: bool = False,
        verification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        storage_provider = storage_provider.strip().lower()
        if not storage_provider or not provider_file_id:
            raise ValueError("storage_provider and provider_file_id are required")
        now = utcnow()
        verification_json = json.dumps(verification or {}, ensure_ascii=False)
        sha256 = sha256.lower() if sha256 else None

        with self.connect() as con:
            existing = con.execute(
                "SELECT * FROM files WHERE storage_provider = ? AND provider_file_id = ?",
                (storage_provider, provider_file_id),
            ).fetchone()
            if existing:
                con.execute(
                    """
                    UPDATE files
                    SET episode_id = ?, provider_path = COALESCE(?, provider_path),
                        sha256 = COALESCE(?, sha256), bytes = COALESCE(?, bytes),
                        mime_type = COALESCE(?, mime_type), verified = ?,
                        verification_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        episode_id,
                        provider_path,
                        sha256,
                        bytes_size,
                        mime_type,
                        int(bool(verified)),
                        verification_json,
                        now,
                        existing["id"],
                    ),
                )
                file_id = existing["id"]
            else:
                file_id = new_id()
                con.execute(
                    """
                    INSERT INTO files
                    (id, episode_id, storage_provider, provider_file_id, provider_path,
                     sha256, bytes, mime_type, verified, verification_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        episode_id,
                        storage_provider,
                        provider_file_id,
                        provider_path,
                        sha256,
                        bytes_size,
                        mime_type,
                        int(bool(verified)),
                        verification_json,
                        now,
                        now,
                    ),
                )
            row = con.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        result = self._row(row) or {}
        result["verified"] = bool(result.get("verified"))
        result["verification"] = json.loads(result.pop("verification_json", "{}"))
        return result

    def create_job(
        self,
        *,
        kind: str,
        drama_id: str | None = None,
        source_id: str | None = None,
        episode_id: str | None = None,
        state: str = "queued",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        job_id = new_id()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO ingestion_jobs
                (id, kind, drama_id, source_id, episode_id, state, error, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (job_id, kind, drama_id, source_id, episode_id, state, json.dumps(metadata or {}), now, now),
            )
            row = con.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        result = self._row(row) or {}
        result["metadata"] = json.loads(result.pop("metadata_json", "{}"))
        return result

    def update_job(self, job_id: str, *, state: str, error: str | None = None) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as con:
            con.execute(
                "UPDATE ingestion_jobs SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                (state, error, now, job_id),
            )
            row = con.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = self._row(row) or {}
        result["metadata"] = json.loads(result.pop("metadata_json", "{}"))
        return result

    def get_drama(self, slug_or_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            drama = con.execute(
                "SELECT * FROM dramas WHERE id = ? OR slug = ?",
                (slug_or_id, slug_or_id),
            ).fetchone()
            if drama is None:
                return None
            drama_id = drama["id"]
            sources = con.execute(
                "SELECT * FROM sources WHERE drama_id = ? ORDER BY platform, created_at",
                (drama_id,),
            ).fetchall()
            episodes = con.execute(
                "SELECT * FROM episodes WHERE drama_id = ? ORDER BY episode_number",
                (drama_id,),
            ).fetchall()
            episode_ids = [row["id"] for row in episodes]
            files: list[sqlite3.Row] = []
            if episode_ids:
                placeholders = ",".join("?" for _ in episode_ids)
                files = con.execute(
                    f"SELECT * FROM files WHERE episode_id IN ({placeholders}) ORDER BY created_at",
                    episode_ids,
                ).fetchall()

        result = dict(drama)
        result["genres"] = json.loads(result.pop("genres_json", "[]"))
        result["sources"] = []
        for row in sources:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json", "{}"))
            result["sources"].append(item)

        files_by_episode: dict[str, list[dict[str, Any]]] = {}
        for row in files:
            item = dict(row)
            item["verified"] = bool(item["verified"])
            item["verification"] = json.loads(item.pop("verification_json", "{}"))
            files_by_episode.setdefault(item["episode_id"], []).append(item)

        result["episodes"] = []
        for row in episodes:
            item = dict(row)
            item["files"] = files_by_episode.get(item["id"], [])
            result["episodes"].append(item)
        return result

    def list_dramas(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT d.*,
                       (SELECT COUNT(*) FROM episodes e WHERE e.drama_id = d.id) AS stored_episode_rows,
                       (SELECT COUNT(*) FROM files f JOIN episodes e ON e.id = f.episode_id
                        WHERE e.drama_id = d.id AND f.verified = 1) AS verified_files
                FROM dramas d
                ORDER BY d.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["genres"] = json.loads(item.pop("genres_json", "[]"))
            values.append(item)
        return values
