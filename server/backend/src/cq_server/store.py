"""SQLite-backed remote knowledge store.

Stores knowledge units in a SQLite database for remote sharing.
Auto-creates the database directory and schema on first use.
Implements the context manager protocol for deterministic resource cleanup.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from cq.models import KnowledgeUnit
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from .scoring import calculate_relevance
from .tables import ensure_review_columns, ensure_users_table

DEFAULT_DB_PATH = Path("/data/cq.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_units (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_unit_domains (
    unit_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    FOREIGN KEY (unit_id) REFERENCES knowledge_units(id) ON DELETE CASCADE,
    PRIMARY KEY (unit_id, domain)
);

CREATE INDEX IF NOT EXISTS idx_domains_domain
    ON knowledge_unit_domains(domain);
"""


def normalize_domains(domains: list[str]) -> list[str]:
    """Lowercase, strip whitespace, drop empties, and deduplicate domain tags."""
    return list(dict.fromkeys(d.strip().lower() for d in domains if d.strip()))


class RemoteStore:
    """SQLite-backed remote knowledge store.

    Uses SQLAlchemy Engine for database connections.
    Use as a context manager or call ``close()`` explicitly.

    Thread-safe: SQLAlchemy's connection pool handles thread safety.
    """

    def __init__(self, database_url: str | None = None, db_path: Path | None = None) -> None:
        """Initialise the store, creating the database and schema if needed.

        Args:
            database_url: SQLAlchemy database URL. Takes precedence over db_path.
            db_path: Path to the SQLite database file. Defaults to /data/cq.db.
                     Only used if database_url is not provided.
        """
        if database_url:
            self._database_url = database_url
        else:
            # Backward compatibility: construct URL from db_path
            path = db_path or DEFAULT_DB_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            self._database_url = f"sqlite:///{path}"

        self._closed = False
        self._engine = self._create_engine()
        self._db_type = self._engine.dialect.name
        self._ensure_schema()

    def _create_engine(self) -> Engine:
        """Create and configure a SQLAlchemy engine.

        For PostgreSQL, use postgresql+psycopg:// URLs to explicitly use psycopg (v3).
        """
        # Normalize PostgreSQL URLs: postgresql:// -> postgresql+psycopg://
        database_url = self._database_url
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

        # PostgreSQL connection pool configuration
        if database_url.startswith("postgresql"):
            pool_size = int(os.environ.get("CQ_PG_POOL_SIZE", "10"))
            max_overflow = int(os.environ.get("CQ_PG_POOL_OVERFLOW", "20"))
            engine = create_engine(
                database_url,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_pre_ping=True,  # Validate connections before use
            )
        else:
            engine = create_engine(database_url)

        # SQLite-specific pragmas via event listener
        if database_url.startswith("sqlite"):
            @event.listens_for(engine, "connect")
            def set_sqlite_pragmas(dbapi_conn, _connection_record):
                dbapi_conn.execute("PRAGMA foreign_keys = ON")
                dbapi_conn.execute("PRAGMA journal_mode = WAL")
                dbapi_conn.execute("PRAGMA synchronous = NORMAL")
                dbapi_conn.execute("PRAGMA busy_timeout = 5000")

        return engine

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._engine.begin() as conn:
            # Execute each statement separately (text() doesn't support multi-statement SQL)
            for statement in _SCHEMA_SQL.strip().split(";"):
                statement = statement.strip()
                if statement:
                    conn.execute(text(statement))
            # Note: ensure_review_columns and ensure_users_table still expect raw connection
            # We'll need to pass the underlying DBAPI connection
            ensure_review_columns(conn.connection.dbapi_connection)
            ensure_users_table(conn.connection.dbapi_connection)

    def _check_open(self) -> None:
        """Raise if the store has been closed."""
        if self._closed:
            raise RuntimeError("RemoteStore is closed")

    def close(self) -> None:
        """Dispose of the underlying engine and connection pool."""
        if self._closed:
            return
        self._closed = True
        self._engine.dispose()

    def __enter__(self) -> "RemoteStore":
        """Enter the context manager."""
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager, closing the engine."""
        self.close()

    @property
    def database_url(self) -> str:
        """Database URL used by this store."""
        return self._database_url

    def insert(self, unit: KnowledgeUnit) -> None:
        """Insert a knowledge unit into the store.

        Args:
            unit: The knowledge unit to insert.

        Raises:
            IntegrityError: If a unit with the same ID already exists.
            ValueError: If domain normalization results in no valid domains.
        """
        self._check_open()
        domains = normalize_domains(unit.domains)
        if not domains:
            raise ValueError("At least one non-empty domain is required")
        unit = unit.model_copy(update={"domains": domains})
        data = unit.model_dump_json()
        created_at = (
            unit.evidence.first_observed.isoformat() if unit.evidence.first_observed else datetime.now(UTC).isoformat()
        )
        with self._engine.begin() as conn:
            conn.execute(
                text("INSERT INTO knowledge_units (id, data, created_at, tier) VALUES (:id, :data, :created_at, :tier)"),
                {"id": unit.id, "data": data, "created_at": created_at, "tier": unit.tier.value},
            )
            for domain in domains:
                conn.execute(
                    text("INSERT INTO knowledge_unit_domains (unit_id, domain) VALUES (:unit_id, :domain)"),
                    {"unit_id": unit.id, "domain": domain},
                )

    def get(self, unit_id: str) -> KnowledgeUnit | None:
        """Retrieve an approved knowledge unit by ID.

        Agent-facing: only returns KUs that have passed human review.
        For internal access regardless of status, use get_any().

        Args:
            unit_id: The knowledge unit identifier.

        Returns:
            The knowledge unit, or None if not found or not approved.
        """
        self._check_open()
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM knowledge_units WHERE id = :id AND status = 'approved'"),
                {"id": unit_id},
            ).fetchone()
        if row is None:
            return None
        return KnowledgeUnit.model_validate_json(row[0])

    def get_any(self, unit_id: str) -> KnowledgeUnit | None:
        """Retrieve a knowledge unit by ID regardless of review status.

        Internal use only — review endpoints and activity feed.

        Args:
            unit_id: The knowledge unit identifier.

        Returns:
            The knowledge unit, or None if not found.
        """
        self._check_open()
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM knowledge_units WHERE id = :id"),
                {"id": unit_id},
            ).fetchone()
        if row is None:
            return None
        return KnowledgeUnit.model_validate_json(row[0])

    def get_review_status(self, unit_id: str) -> dict[str, str | None] | None:
        """Return review metadata for a knowledge unit.

        Args:
            unit_id: The knowledge unit identifier.

        Returns:
            A dict with status, reviewed_by, and reviewed_at keys, or None
            if the unit does not exist.
        """
        self._check_open()
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT status, reviewed_by, reviewed_at FROM knowledge_units WHERE id = :id"),
                {"id": unit_id},
            ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "reviewed_by": row[1], "reviewed_at": row[2]}

    def set_review_status(self, unit_id: str, status: str, reviewed_by: str) -> None:
        """Update the review status of a knowledge unit.

        Args:
            unit_id: The knowledge unit identifier.
            status: The new review status (e.g. "approved", "rejected").
            reviewed_by: Username of the reviewer.

        Raises:
            KeyError: If no unit with the given ID exists.
        """
        self._check_open()
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            result = conn.execute(
                text("UPDATE knowledge_units SET status = :status, reviewed_by = :reviewed_by, reviewed_at = :reviewed_at WHERE id = :id"),
                {"status": status, "reviewed_by": reviewed_by, "reviewed_at": now, "id": unit_id},
            )
            if result.rowcount == 0:
                raise KeyError(f"Knowledge unit not found: {unit_id}")

    def delete(self, unit_id: str) -> None:
        """Delete a knowledge unit and its domain associations.

        Args:
            unit_id: The knowledge unit identifier.

        Raises:
            KeyError: If no unit with the given ID exists.
        """
        self._check_open()
        with self._engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM knowledge_units WHERE id = :id"),
                {"id": unit_id},
            )
            if result.rowcount == 0:
                raise KeyError(f"Knowledge unit not found: {unit_id}")

    def update(self, unit: KnowledgeUnit) -> None:
        """Replace an existing knowledge unit in the store.

        Args:
            unit: The updated knowledge unit.

        Raises:
            KeyError: If no unit with the given ID exists.
            ValueError: If domain normalization results in no valid domains.
        """
        self._check_open()
        domains = normalize_domains(unit.domains)
        if not domains:
            raise ValueError("At least one non-empty domain is required")
        unit = unit.model_copy(update={"domains": domains})
        data = unit.model_dump_json()
        with self._engine.begin() as conn:
            result = conn.execute(
                text("UPDATE knowledge_units SET data = :data, tier = :tier WHERE id = :id"),
                {"data": data, "tier": unit.tier.value, "id": unit.id},
            )
            if result.rowcount == 0:
                raise KeyError(f"Knowledge unit not found: {unit.id}")
            conn.execute(
                text("DELETE FROM knowledge_unit_domains WHERE unit_id = :unit_id"),
                {"unit_id": unit.id},
            )
            for domain in domains:
                conn.execute(
                    text("INSERT INTO knowledge_unit_domains (unit_id, domain) VALUES (:unit_id, :domain)"),
                    {"unit_id": unit.id, "domain": domain},
                )

    def query(
        self,
        domains: list[str],
        *,
        languages: list[str] | None = None,
        frameworks: list[str] | None = None,
        limit: int = 5,
    ) -> list[KnowledgeUnit]:
        """Search for knowledge units by domain tags with relevance ranking.

        Args:
            domains: Domain tags to search for.
            languages: Optional language ranking signal. KUs matching any
                listed language rank higher but non-matching KUs are still returned.
            frameworks: Optional framework ranking signal. KUs matching any
                listed framework rank higher but non-matching KUs are still returned.
            limit: Maximum number of results to return. Must be positive.

        Returns:
            Knowledge units ranked by relevance * confidence, descending.

        Raises:
            ValueError: If limit is not positive.
        """
        self._check_open()
        if limit <= 0:
            raise ValueError("limit must be positive")
        if not domains:
            return []

        normalized = normalize_domains(domains)
        if not normalized:
            return []
        # Build named parameters for each domain
        params = {f"domain_{i}": d for i, d in enumerate(normalized)}
        placeholders = ",".join(f":{name}" for name in params.keys())
        sql = f"""
            SELECT ku.data
            FROM knowledge_units ku
            WHERE ku.status = 'approved'
            AND ku.id IN (
                SELECT DISTINCT unit_id
                FROM knowledge_unit_domains
                WHERE domain IN ({placeholders})
            )
        """
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()

        # PoC: all filtering and scoring is in-memory after deserialization.
        # For larger stores, push coarse filters into SQL.
        units = [KnowledgeUnit.model_validate_json(row[0]) for row in rows]

        scored = []
        for unit in units:
            relevance = calculate_relevance(
                unit,
                normalized,
                query_languages=languages,
                query_frameworks=frameworks,
            )
            scored.append((relevance * unit.evidence.confidence, unit))

        scored.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
        return [unit for _, unit in scored[:limit]]

    def count(self) -> int:
        """Return the total number of knowledge units in the store."""
        self._check_open()
        with self._engine.connect() as conn:
            row = conn.execute(text("SELECT COUNT(*) FROM knowledge_units")).fetchone()
        return row[0]

    def domain_counts(self) -> dict[str, int]:
        """Return the count of approved knowledge units per domain tag."""
        self._check_open()
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT d.domain, COUNT(*) "
                     "FROM knowledge_unit_domains d "
                     "JOIN knowledge_units ku ON ku.id = d.unit_id "
                     "WHERE ku.status = 'approved' "
                     "GROUP BY d.domain ORDER BY COUNT(*) DESC")
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def pending_queue(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """Return pending KUs with review metadata, oldest first.

        Args:
            limit: Maximum number of results to return.
            offset: Number of results to skip.

        Returns:
            List of dicts with knowledge_unit, status, reviewed_by,
            and reviewed_at keys.
        """
        self._check_open()
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT data, status, reviewed_by, reviewed_at "
                     "FROM knowledge_units WHERE status = 'pending' "
                     "ORDER BY created_at ASC LIMIT :limit OFFSET :offset"),
                {"limit": limit, "offset": offset},
            ).fetchall()
        return [
            {
                "knowledge_unit": KnowledgeUnit.model_validate_json(row[0]),
                "status": row[1],
                "reviewed_by": row[2],
                "reviewed_at": row[3],
            }
            for row in rows
        ]

    def pending_count(self) -> int:
        """Return the number of pending KUs."""
        self._check_open()
        with self._engine.connect() as conn:
            row = conn.execute(text("SELECT COUNT(*) FROM knowledge_units WHERE status = 'pending'")).fetchone()
        return row[0]

    def counts_by_status(self) -> dict[str, int]:
        """Return KU counts grouped by review status."""
        self._check_open()
        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT status, COUNT(*) FROM knowledge_units GROUP BY status")).fetchall()
        return {row[0]: row[1] for row in rows}

    def counts_by_tier(self) -> dict[str, int]:
        """Return approved KU counts grouped by tier."""
        self._check_open()
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT tier, COUNT(*) FROM knowledge_units WHERE status = 'approved' GROUP BY tier")
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def list_units(
        self,
        *,
        domain: str | None = None,
        confidence_min: float | None = None,
        confidence_max: float | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return KUs with review metadata, filtered by domain, confidence, or status.

        Confidence filtering is applied in-memory after deserialization
        since confidence lives in the JSON blob.

        Args:
            domain: Optional domain tag to filter by.
            confidence_min: Optional minimum confidence (inclusive).
            confidence_max: Optional maximum confidence (exclusive when < 1.0, inclusive at 1.0).
            status: Optional review status to filter by (e.g. "approved", "rejected").
            limit: Maximum number of results to return.

        Returns:
            List of dicts with knowledge_unit, status, reviewed_by,
            and reviewed_at keys.
        """
        self._check_open()
        params: dict[str, Any] = {}
        conditions: list[str] = []

        if status:
            conditions.append("ku.status = :status")
            params["status"] = status

        if domain:
            normalized = normalize_domains([domain])
            if not normalized:
                return []
            conditions.append("ku.id IN (SELECT DISTINCT unit_id FROM knowledge_unit_domains WHERE domain = :domain)")
            params["domain"] = normalized[0]

        has_confidence_filter = confidence_min is not None or confidence_max is not None
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql_limit = "" if has_confidence_filter else f"LIMIT {limit}"
        sql = (
            "SELECT ku.data, ku.status, ku.reviewed_by, ku.reviewed_at "
            f"FROM knowledge_units ku {where} "
            f"ORDER BY ku.created_at DESC {sql_limit}"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()

        results = []
        for row in rows:
            unit = KnowledgeUnit.model_validate_json(row[0])
            c = unit.evidence.confidence
            if confidence_min is not None and c < confidence_min:
                continue
            if confidence_max is not None and (c > confidence_max or (c >= confidence_max and confidence_max < 1.0)):
                continue
            results.append(
                {
                    "knowledge_unit": unit,
                    "status": row[1] or "pending",
                    "reviewed_by": row[2],
                    "reviewed_at": row[3],
                }
            )
            if len(results) >= limit:
                break
        return results

    def create_user(self, username: str, password_hash: str) -> None:
        """Insert a new user.

        Args:
            username: The user's login name.
            password_hash: Bcrypt hash of the user's password.

        Raises:
            IntegrityError: If a user with the same username already exists.
        """
        self._check_open()
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (username, password_hash, created_at) VALUES (:username, :password_hash, :created_at)"),
                {"username": username, "password_hash": password_hash, "created_at": now},
            )

    def get_user(self, username: str) -> dict[str, str] | None:
        """Retrieve a user by username.

        Args:
            username: The user's login name.

        Returns:
            A dict with username, password_hash, and created_at keys, or None
            if no user with that username exists.
        """
        self._check_open()
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT username, password_hash, created_at FROM users WHERE username = :username"),
                {"username": username},
            ).fetchone()
        if row is None:
            return None
        return {"username": row[0], "password_hash": row[1], "created_at": row[2]}

    def confidence_distribution(self) -> dict[str, int]:
        """Return confidence distribution buckets for approved KUs."""
        self._check_open()
        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT data FROM knowledge_units WHERE status = 'approved'")).fetchall()
        buckets = {"0.0-0.3": 0, "0.3-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
        for (data,) in rows:
            unit = KnowledgeUnit.model_validate_json(data)
            c = unit.evidence.confidence
            if c < 0.3:
                buckets["0.0-0.3"] += 1
            elif c < 0.6:
                buckets["0.3-0.6"] += 1
            elif c < 0.8:
                buckets["0.6-0.8"] += 1
            else:
                buckets["0.8-1.0"] += 1
        return buckets

    def recent_activity(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent activity as one event per knowledge unit.

        Each KU appears once: reviewed KUs show as approved/rejected,
        pending KUs show as proposed.  Ordered by the most recent
        timestamp (reviewed_at for reviewed KUs, created_at otherwise).

        Args:
            limit: Maximum number of activity entries to return.

        Returns:
            List of activity event dicts, newest first.
        """
        self._check_open()
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, data, status, reviewed_by, reviewed_at "
                     "FROM knowledge_units "
                     "ORDER BY COALESCE(reviewed_at, created_at) DESC LIMIT :limit"),
                {"limit": limit * 2},
            ).fetchall()
        activity = []
        for row in rows:
            unit = KnowledgeUnit.model_validate_json(row[1])
            proposed_ts = unit.evidence.first_observed.isoformat() if unit.evidence.first_observed else ""
            # Show only the terminal state per KU: the review event if
            # reviewed, otherwise the proposed event.
            if row[2] in ("approved", "rejected"):
                activity.append(
                    {
                        "type": row[2],
                        "unit_id": row[0],
                        "summary": unit.insight.summary,
                        "reviewed_by": row[3],
                        "timestamp": row[4] or proposed_ts,
                    }
                )
            else:
                activity.append(
                    {
                        "type": "proposed",
                        "unit_id": row[0],
                        "summary": unit.insight.summary,
                        "timestamp": proposed_ts,
                    }
                )
        activity.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return activity[:limit]

    def daily_counts(self, *, days: int = 30) -> list[dict[str, Any]]:
        """Return daily proposal and approval counts with contiguous dates.

        Returns one entry per day from the earliest activity (within the
        lookback window) through today, filling gaps with zero counts.
        Pre-migration rows with NULL created_at are excluded.

        Args:
            days: Number of days to look back.

        Returns:
            List of dicts with date, proposed, approved, and rejected
            counts, ordered ascending.

        Raises:
            ValueError: If days is not positive.
        """
        if days <= 0:
            raise ValueError("days must be positive")
        self._check_open()

        # Compute cutoff date in Python (portable across backends)
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()

        with self._engine.connect() as conn:
            proposed_rows = conn.execute(
                text("SELECT DATE(created_at) as day, COUNT(*) as cnt "
                     "FROM knowledge_units "
                     "WHERE created_at >= :cutoff "
                     "GROUP BY DATE(created_at)"),
                {"cutoff": cutoff},
            ).fetchall()
            approved_rows = conn.execute(
                text("SELECT DATE(reviewed_at) as day, COUNT(*) as cnt "
                     "FROM knowledge_units "
                     "WHERE status = 'approved' "
                     "AND reviewed_at >= :cutoff "
                     "GROUP BY DATE(reviewed_at)"),
                {"cutoff": cutoff},
            ).fetchall()
            rejected_rows = conn.execute(
                text("SELECT DATE(reviewed_at) as day, COUNT(*) as cnt "
                     "FROM knowledge_units "
                     "WHERE status = 'rejected' "
                     "AND reviewed_at >= :cutoff "
                     "GROUP BY DATE(reviewed_at)"),
                {"cutoff": cutoff},
            ).fetchall()
        proposed = {row[0]: row[1] for row in proposed_rows}
        approved = {row[0]: row[1] for row in approved_rows}
        rejected = {row[0]: row[1] for row in rejected_rows}
        all_dates = set(proposed) | set(approved) | set(rejected)
        if not all_dates:
            return []
        start = min(datetime.strptime(d, "%Y-%m-%d").date() for d in all_dates)
        end = datetime.now(UTC).date()
        result: list[dict[str, Any]] = []
        current = start
        while current <= end:
            key = current.isoformat()
            result.append(
                {
                    "date": key,
                    "proposed": proposed.get(key, 0),
                    "approved": approved.get(key, 0),
                    "rejected": rejected.get(key, 0),
                }
            )
            current += timedelta(days=1)
        return result
