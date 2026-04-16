# PostgreSQL Backend Support for cq Remote Server

## Context

The cq knowledge store currently uses SQLite for all deployments. While SQLite works well for single-instance deployments, production environments running multiple server instances (e.g., Kubernetes horizontal scaling) need a shared database backend. PostgreSQL support will enable:

- **Multi-instance deployments** with shared state
- **Managed database services** (AWS RDS, Google Cloud SQL, Azure Database)
- **Better concurrency** through connection pooling
- **Production-grade monitoring** and backup tooling

This change is **server-only** and **optional** — SQLite remains the default for zero-config deployments. Local SDKs (Go/Python) and CLI local stores continue using SQLite intentionally (they're local caches, not shared databases).

## Plan Comparison Analysis

Two implementation approaches were evaluated:

### Upstream Plan (nissessenap/cq)

**Key Features:**
- **SQLAlchemy Core** for database abstraction (single Store class)
- **Alembic** for schema migrations (version-tracked, dialect-agnostic)
- **CQ_DATABASE_URL** environment variable (standard connection string format)
- **psycopg v3** (modern driver with native pooling)
- **testcontainers** for PostgreSQL integration tests
- ~90% code reuse between backends (one Store class handles both)

**Strengths:**
- Proper migration framework eliminates ad-hoc schema management
- Single codebase for all database operations (DRY principle)
- SQLAlchemy already pulled in by Alembic (zero extra dependencies)
- Automatic placeholder translation (`:name` → `?` or `%s`)
- Built-in connection pooling with configurable limits
- Handles existing databases gracefully (stamping + migration)

**Tradeoffs:**
- Learning curve for SQLAlchemy Core (text() API, Engine abstraction)
- One more abstraction layer between code and database
- Requires rewriting current sqlite3-based code

### My Initial Plan

**Key Features:**
- **Protocol-based abstraction** without ORM (interface pattern)
- **Manual SQL translation** with backend-specific implementations
- **Individual environment variables** (CQ_PG_HOST, CQ_PG_PORT, etc.)
- **psycopg2-binary** (stable, widely used)
- **Manual migration script** (sqlite-to-postgres.py)
- ~10% code reuse (two separate Store classes)

**Strengths:**
- No new abstraction layer (direct driver usage)
- Each backend implementation is self-contained and explicit
- Minimal changes to existing sqlite3 code
- Lower learning curve for contributors familiar with sqlite3

**Tradeoffs:**
- ~90% duplicated SQL across backends (maintenance burden)
- No migration framework (risky schema evolution)
- Manual connection pool implementation required
- Change once, update twice pattern

## Recommended Approach: Hybrid Strategy

After careful analysis, **I recommend adopting the upstream approach with refinements**:

### Core Decisions

1. **Use SQLAlchemy Core** (not raw SQL duplication)
   - Rationale: Alembic requires SQLAlchemy anyway, so Core adds zero dependencies
   - Single Store class eliminates maintenance burden of duplicate SQL
   - Automatic placeholder translation and connection pooling

2. **Use Alembic for migrations** (not ad-hoc schema management)
   - Rationale: Current `CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info()` doesn't scale to multiple backends
   - Version-tracked migrations prevent schema drift
   - Handles existing databases via stamping

3. **Use `CQ_DATABASE_URL`** (not individual params)
   - Rationale: Standard format, composable, Docker-friendly
   - Backward compatible: falls back to `CQ_DB_PATH` for SQLite
   - Example: `postgresql://user:pass@host/db` or `sqlite:////data/cq.db`

4. **Use psycopg v3** (not psycopg2)
   - Rationale: Recommended for new projects, better pooling, actively maintained
   - psycopg2 is maintenance-only mode

5. **Use testcontainers** for integration tests
   - Rationale: Real PostgreSQL in CI, no mocking database behavior

### Implementation Refinements

**Configuration fallback chain:**
```python
# Priority order:
1. CQ_DATABASE_URL (new, highest priority)
2. CQ_DB_PATH (existing, SQLite only)
3. Default: sqlite:////data/cq.db
```

**Environment variable compatibility:**
```bash
# New projects: use connection string
CQ_DATABASE_URL=postgresql://cq:pass@postgres:5432/cq

# Existing deployments: no change needed
CQ_DB_PATH=/data/cq.db
# Automatically becomes: sqlite:////data/cq.db
```

## Implementation Plan

### Phase 1: SQLAlchemy Core Migration (SQLite-only)

**Goal:** Replace raw sqlite3 with SQLAlchemy Core, keeping SQLite as only backend. This phase is independently mergeable and proves the abstraction works.

**Files to modify:**
- `/workspace/cq/server/backend/src/cq_server/store.py` - Replace sqlite3.Connection with sqlalchemy.Engine
- `/workspace/cq/server/backend/src/cq_server/app.py` - Update lifespan to create Engine
- `/workspace/cq/server/backend/pyproject.toml` - Add SQLAlchemy dependency

**Tasks:**

1. **Add dependencies:**
   ```toml
   [project]
   dependencies = [
       "sqlalchemy>=2.0.25,<3",
   ]
   ```

2. **Update RemoteStore class:**
   ```python
   from sqlalchemy import create_engine, text, event
   
   class RemoteStore:
       def __init__(self, database_url: str):
           self._engine = create_engine(database_url)
           self._db_type = self._engine.dialect.name
           
           # SQLite-specific pragmas
           if self._db_type == "sqlite":
               @event.listens_for(self._engine, "connect")
               def set_sqlite_pragmas(dbapi_conn, _):
                   dbapi_conn.execute("PRAGMA foreign_keys = ON")
                   dbapi_conn.execute("PRAGMA journal_mode = WAL")
                   dbapi_conn.execute("PRAGMA synchronous = NORMAL")
                   dbapi_conn.execute("PRAGMA busy_timeout = 5000")
   ```

3. **Convert queries to text() API:**
   ```python
   # Before:
   row = self._conn.execute(
       "SELECT data FROM knowledge_units WHERE id = ?",
       (unit_id,)
   ).fetchone()
   
   # After:
   with self._engine.connect() as conn:
       row = conn.execute(
           text("SELECT data FROM knowledge_units WHERE id = :id"),
           {"id": unit_id}
       ).fetchone()
   ```

4. **Update app.py lifespan:**
   ```python
   @asynccontextmanager
   async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
       global _store
       jwt_secret = os.environ.get("CQ_JWT_SECRET")
       if not jwt_secret:
           raise RuntimeError("CQ_JWT_SECRET required")
       
       # New: build database URL
       database_url = os.environ.get("CQ_DATABASE_URL")
       if not database_url:
           db_path = os.environ.get("CQ_DB_PATH", "/data/cq.db")
           database_url = f"sqlite:///{db_path}"
       
       _store = RemoteStore(database_url=database_url)
       app_instance.state.store = _store
       yield
       _store.close()
   ```

5. **Remove thread lock** (Engine handles this):
   - Delete `self._lock = threading.Lock()`
   - Remove all `with self._lock:` blocks
   - Engine's connection pool is thread-safe by default

6. **Verify all 42 tests pass** with SQLite

**Success criteria:**
- No change in behavior (still SQLite-only)
- All existing tests pass
- `CQ_DB_PATH` still works (backward compatible)
- `CQ_DATABASE_URL` works for SQLite URLs

### Phase 2: Alembic Migration Framework

**Goal:** Replace ad-hoc schema management with version-tracked migrations.

**New files:**
- `/workspace/cq/server/backend/alembic.ini`
- `/workspace/cq/server/backend/alembic/env.py`
- `/workspace/cq/server/backend/alembic/versions/001_baseline.py`

**Files to modify:**
- `/workspace/cq/server/backend/src/cq_server/store.py` - Remove `_ensure_schema()` logic
- `/workspace/cq/server/backend/src/cq_server/tables.py` - Remove `ensure_*` functions
- `/workspace/cq/server/backend/src/cq_server/app.py` - Add migration runner

**Tasks:**

1. **Add Alembic dependency:**
   ```toml
   dependencies = [
       "alembic>=1.18.0,<2",
   ]
   ```

2. **Initialize Alembic:**
   ```bash
   cd /workspace/cq/server/backend
   alembic init alembic
   ```

3. **Configure env.py:**
   ```python
   # alembic/env.py
   from cq_server.store import RemoteStore
   
   def get_database_url() -> str:
       database_url = os.environ.get("CQ_DATABASE_URL")
       if not database_url:
           db_path = os.environ.get("CQ_DB_PATH", "/data/cq.db")
           database_url = f"sqlite:///{db_path}"
       return database_url
   
   config.set_main_option("sqlalchemy.url", get_database_url())
   
   context.configure(
       connection=connection,
       target_metadata=target_metadata,
       render_as_batch=True,  # Critical for SQLite ALTER TABLE
   )
   ```

4. **Create baseline migration:**
   ```python
   # alembic/versions/001_baseline.py
   """Baseline schema for existing databases."""
   
   from alembic import op
   import sqlalchemy as sa
   
   revision = "001"
   down_revision = None
   
   def upgrade():
       # knowledge_units table
       op.create_table(
           "knowledge_units",
           sa.Column("id", sa.Text(), nullable=False),
           sa.Column("data", sa.Text(), nullable=False),
           sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
           sa.Column("reviewed_by", sa.Text()),
           sa.Column("reviewed_at", sa.Text()),
           sa.Column("created_at", sa.Text()),
           sa.Column("tier", sa.Text(), nullable=False, server_default="private"),
           sa.PrimaryKeyConstraint("id"),
       )
       
       # knowledge_unit_domains junction table
       op.create_table(
           "knowledge_unit_domains",
           sa.Column("unit_id", sa.Text(), nullable=False),
           sa.Column("domain", sa.Text(), nullable=False),
           sa.ForeignKeyConstraint(["unit_id"], ["knowledge_units.id"], ondelete="CASCADE"),
           sa.PrimaryKeyConstraint("unit_id", "domain"),
       )
       op.create_index("idx_domains_domain", "knowledge_unit_domains", ["domain"])
       
       # users table
       op.create_table(
           "users",
           sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
           sa.Column("username", sa.Text(), nullable=False),
           sa.Column("password_hash", sa.Text(), nullable=False),
           sa.Column("created_at", sa.Text(), nullable=False),
       )
       op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)
   
   def downgrade():
       op.drop_table("users")
       op.drop_table("knowledge_unit_domains")
       op.drop_table("knowledge_units")
   ```

5. **Add migration runner to app.py:**
   ```python
   from alembic import command
   from alembic.config import Config
   from sqlalchemy import inspect
   
   def run_migrations(engine):
       """Run Alembic migrations at startup."""
       alembic_cfg = Config("alembic.ini")
       alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
       
       inspector = inspect(engine)
       tables = inspector.get_table_names()
       
       # Stamp existing databases at baseline without running DDL
       if "knowledge_units" in tables and "alembic_version" not in tables:
           command.stamp(alembic_cfg, "001")
       
       # Apply any pending migrations
       command.upgrade(alembic_cfg, "head")
   
   @asynccontextmanager
   async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
       global _store
       # ... existing setup ...
       _store = RemoteStore(database_url=database_url)
       run_migrations(_store._engine)  # Run migrations after store creation
       app_instance.state.store = _store
       yield
       _store.close()
   ```

6. **Remove old schema management:**
   - Delete `_ensure_schema()` from RemoteStore
   - Delete `tables.py` entirely (replaced by Alembic)

7. **Test migration on fresh and existing databases:**
   - Fresh DB: creates tables via Alembic
   - Existing DB: stamps at baseline, no DDL run

**Success criteria:**
- New deployments initialize schema via Alembic
- Existing SQLite databases stamp at baseline and continue working
- `alembic_version` table tracks migration state
- All tests pass

### Phase 3: PostgreSQL Backend Support

**Goal:** Enable PostgreSQL as an optional backend alongside SQLite.

**Files to modify:**
- `/workspace/cq/server/backend/src/cq_server/store.py` - Handle PostgreSQL dialect
- `/workspace/cq/server/backend/pyproject.toml` - Add psycopg dependency

**Tasks:**

1. **Add PostgreSQL dependencies:**
   ```toml
   dependencies = [
       "psycopg[binary,pool]>=3.2.0,<4",
   ]
   ```

2. **Update daily_counts() to be portable:**
   ```python
   def daily_counts(self, *, days: int = 30) -> list[dict[str, Any]]:
       if days <= 0:
           raise ValueError("days must be positive")
       
       # Compute cutoff in Python (portable)
       cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
       
       with self._engine.connect() as conn:
           proposed_rows = conn.execute(
               text("SELECT DATE(created_at) as day, COUNT(*) as cnt "
                    "FROM knowledge_units "
                    "WHERE created_at >= :cutoff "
                    "GROUP BY day"),
               {"cutoff": cutoff}
           ).fetchall()
           # ... similar for approved/rejected ...
   ```

3. **Handle DATE() function difference:**
   ```python
   # Build SQL based on dialect
   if self._db_type == "sqlite":
       date_expr = "DATE(created_at)"
   else:  # postgres
       date_expr = "created_at::date"
   
   sql = text(f"SELECT {date_expr} as day, COUNT(*) as cnt ...")
   ```

4. **Configure connection pool:**
   ```python
   def __init__(self, database_url: str):
       # PostgreSQL pool configuration
       if database_url.startswith("postgresql"):
           self._engine = create_engine(
               database_url,
               pool_size=int(os.environ.get("CQ_PG_POOL_SIZE", "10")),
               max_overflow=int(os.environ.get("CQ_PG_POOL_OVERFLOW", "20")),
               pool_pre_ping=True,  # Validate connections before use
           )
       else:  # SQLite
           self._engine = create_engine(database_url)
   ```

5. **Update Docker Compose with PostgreSQL example:**
   ```yaml
   # docker-compose.yml
   services:
     postgres:
       image: postgres:16-alpine
       environment:
         POSTGRES_DB: cq
         POSTGRES_USER: cq
         POSTGRES_PASSWORD: ${CQ_PG_PASSWORD:?}
       volumes:
         - postgres-data:/var/lib/postgresql/data
       healthcheck:
         test: ["CMD-SHELL", "pg_isready -U cq"]
         interval: 5s
         timeout: 5s
         retries: 5
     
     cq-server:
       environment:
         - CQ_DATABASE_URL=postgresql://cq:${CQ_PG_PASSWORD}@postgres:5432/cq
       depends_on:
         postgres:
           condition: service_healthy
   
   volumes:
     postgres-data:
   ```

6. **Test with both backends:**
   - SQLite: existing behavior unchanged
   - PostgreSQL: connection pooling, concurrent access

**Success criteria:**
- Can deploy with SQLite (existing behavior)
- Can deploy with PostgreSQL (new capability)
- All 42 tests pass for both backends
- Connection pooling works under load

### Phase 4: Testing Infrastructure

**Goal:** Ensure both backends are tested in CI.

**Files to modify:**
- `/workspace/cq/server/backend/tests/conftest.py` - Add fixtures
- `/workspace/cq/server/backend/tests/test_store.py` - Parameterize tests

**New files:**
- `/workspace/cq/server/backend/tests/test_postgres_specific.py`

**Tasks:**

1. **Add test dependencies:**
   ```toml
   [dependency-groups]
   tests = [
       "testcontainers>=3.7.0,<4",
   ]
   ```

2. **Create dual-backend fixture:**
   ```python
   # tests/conftest.py
   import pytest
   from testcontainers.postgres import PostgresContainer
   
   @pytest.fixture(scope="session")
   def postgres_container():
       """Start PostgreSQL container for test session."""
       pg_url = os.environ.get("CQ_TEST_PG_URL")
       if pg_url:
           yield pg_url
       else:
           with PostgresContainer("postgres:16-alpine") as postgres:
               yield postgres.get_connection_url()
   
   @pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
   def store(request, tmp_path, postgres_container):
       """Parameterized store fixture for both backends."""
       if request.param == "sqlite":
           db_url = f"sqlite:///{tmp_path}/test.db"
       else:
           db_url = postgres_container
       
       s = RemoteStore(database_url=db_url)
       run_migrations(s._engine)
       yield s
       s.close()
   ```

3. **Update existing tests to use fixture:**
   ```python
   # All tests automatically run against both backends
   def test_insert(store):
       unit = create_knowledge_unit(...)
       store.insert(unit)
       assert store.get(unit.id) == unit
   ```

4. **Add PostgreSQL-specific tests:**
   ```python
   # test_postgres_specific.py
   def test_connection_pooling(postgres_store):
       """Verify concurrent queries use connection pool."""
       import concurrent.futures
       
       def query():
           return postgres_store.query(["test"])
       
       with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
           futures = [executor.submit(query) for _ in range(100)]
           results = [f.result() for f in futures]
       
       assert len(results) == 100  # All queries succeeded
   ```

5. **Update CI workflow:**
   ```yaml
   # .github/workflows/test.yml
   jobs:
     test:
       services:
         postgres:
           image: postgres:16-alpine
           env:
             POSTGRES_DB: cq_test
             POSTGRES_USER: test
             POSTGRES_PASSWORD: test
           options: >-
             --health-cmd pg_isready
             --health-interval 10s
             --health-timeout 5s
             --health-retries 5
       
       env:
         CQ_TEST_PG_URL: postgresql://test:test@localhost:5432/cq_test
   ```

**Success criteria:**
- All 42 tests pass for SQLite
- All 42 tests pass for PostgreSQL
- CI runs both test suites
- Connection pooling handles concurrent load

### Phase 5: Documentation

**Goal:** Document the new functionality for operators and contributors.

**Files to modify:**
- `/workspace/cq/README.md` - Add deployment section
- `/workspace/cq/DEVELOPMENT.md` - Update dev setup

**New files:**
- `/workspace/cq/docs/deployment.md` - Production deployment guide

**Tasks:**

1. **Update README.md:**
   ```markdown
   ## Configuration (Server)
   
   | Variable | Required | Default | Purpose |
   |----------|----------|---------|---------|
   | `CQ_DATABASE_URL` | No | `sqlite:////data/cq.db` | Database connection string |
   | `CQ_DB_PATH` | No | `/data/cq.db` | SQLite path (legacy, use CQ_DATABASE_URL) |
   | `CQ_JWT_SECRET` | Yes | — | JWT signing secret |
   | `CQ_PORT` | No | `3000` | Server listen port |
   
   ### Database Backends
   
   **SQLite (default):**
   ```bash
   docker compose up
   ```
   
   **PostgreSQL:**
   ```bash
   export CQ_PG_PASSWORD=yourpassword
   docker compose -f docker-compose.yml -f docker-compose.postgres.yml up
   ```
   ```

2. **Create deployment guide:**
   ```markdown
   # Production Deployment Guide
   
   ## PostgreSQL Setup
   
   ### Managed Services
   
   **AWS RDS:**
   ```bash
   CQ_DATABASE_URL=postgresql://cq:pass@cq-db.region.rds.amazonaws.com:5432/cq
   ```
   
   **Google Cloud SQL:**
   ```bash
   CQ_DATABASE_URL=postgresql://cq:pass@/cq?host=/cloudsql/project:region:instance
   ```
   
   ### Connection Pooling
   
   Configure pool size based on workload:
   - Light: 5-10 connections
   - Medium: 10-20 connections
   - Heavy: 20-50 connections
   
   ```bash
   CQ_PG_POOL_SIZE=20
   CQ_PG_POOL_OVERFLOW=30
   ```
   
   ### Migrations
   
   Migrations run automatically at startup. For zero-downtime deployments:
   1. Run migration separately: `alembic upgrade head`
   2. Deploy new app version
   ```

3. **Update DEVELOPMENT.md:**
   ```markdown
   ## Testing with PostgreSQL
   
   **Option 1: testcontainers (automatic)**
   ```bash
   make test  # Starts PostgreSQL in Docker automatically
   ```
   
   **Option 2: manual PostgreSQL**
   ```bash
   docker run -d -p 5432:5432 \
     -e POSTGRES_DB=cq_test \
     -e POSTGRES_USER=test \
     -e POSTGRES_PASSWORD=test \
     postgres:16-alpine
   
   export CQ_TEST_PG_URL=postgresql://test:test@localhost:5432/cq_test
   make test
   ```
   ```

**Success criteria:**
- Clear instructions for both SQLite and PostgreSQL
- Connection string examples for major cloud providers
- Migration and zero-downtime deployment guidance

### Phase 6: Migration Tooling

**Goal:** Provide script to migrate existing SQLite databases to PostgreSQL.

**New files:**
- `/workspace/cq/server/scripts/migrate-to-postgres.py`

**Tasks:**

1. **Create migration script:**
   ```python
   #!/usr/bin/env python3
   """Migrate cq database from SQLite to PostgreSQL."""
   
   import argparse
   from sqlalchemy import create_engine, text
   
   def migrate(sqlite_url: str, postgres_url: str, dry_run: bool = False):
       """Copy all data from SQLite to PostgreSQL."""
       src = create_engine(sqlite_url)
       dst = create_engine(postgres_url)
       
       # Get row counts
       with src.connect() as conn:
           ku_count = conn.execute(text("SELECT COUNT(*) FROM knowledge_units")).scalar()
           domain_count = conn.execute(text("SELECT COUNT(*) FROM knowledge_unit_domains")).scalar()
           user_count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
       
       print(f"Source database: {ku_count} KUs, {domain_count} domains, {user_count} users")
       
       if dry_run:
           print("Dry run - no changes made")
           return
       
       # Copy knowledge_units
       with src.connect() as s_conn, dst.connect() as d_conn:
           rows = s_conn.execute(text("SELECT * FROM knowledge_units")).fetchall()
           for row in rows:
               d_conn.execute(
                   text("INSERT INTO knowledge_units (id, data, status, reviewed_by, reviewed_at, created_at, tier) "
                        "VALUES (:id, :data, :status, :reviewed_by, :reviewed_at, :created_at, :tier)"),
                   row._asdict()
               )
           d_conn.commit()
       
       # Similar for domains and users...
       
       # Verify
       with dst.connect() as conn:
           ku_count_dst = conn.execute(text("SELECT COUNT(*) FROM knowledge_units")).scalar()
           assert ku_count == ku_count_dst, "Row count mismatch!"
       
       print(f"Migration complete: {ku_count} KUs migrated")
   
   if __name__ == "__main__":
       parser = argparse.ArgumentParser()
       parser.add_argument("--from-sqlite", required=True)
       parser.add_argument("--to-postgres", required=True)
       parser.add_argument("--dry-run", action="store_true")
       args = parser.parse_args()
       
       migrate(f"sqlite:///{args.from_sqlite}", args.to_postgres, args.dry_run)
   ```

2. **Document migration process:**
   ```markdown
   ## Migrating from SQLite to PostgreSQL
   
   1. **Backup SQLite database:**
      ```bash
      cp /data/cq.db /data/cq.db.backup
      ```
   
   2. **Dry run:**
      ```bash
      python scripts/migrate-to-postgres.py \
        --from-sqlite /data/cq.db \
        --to-postgres postgresql://cq:pass@postgres:5432/cq \
        --dry-run
      ```
   
   3. **Migrate:**
      ```bash
      python scripts/migrate-to-postgres.py \
        --from-sqlite /data/cq.db \
        --to-postgres postgresql://cq:pass@postgres:5432/cq
      ```
   
   4. **Update deployment:**
      ```bash
      export CQ_DATABASE_URL=postgresql://cq:pass@postgres:5432/cq
      docker compose restart cq-server
      ```
   ```

**Success criteria:**
- Script copies all data correctly
- Verification step ensures data integrity
- Dry-run mode previews without changes
- Clear migration instructions

## Critical Files

The following files are most critical for this implementation:

1. `/workspace/cq/server/backend/src/cq_server/store.py` - Core RemoteStore class (656 lines)
2. `/workspace/cq/server/backend/src/cq_server/app.py` - FastAPI app with lifespan manager (180 lines)
3. `/workspace/cq/server/backend/src/cq_server/tables.py` - Schema management to be replaced by Alembic (37 lines)
4. `/workspace/cq/server/backend/pyproject.toml` - Dependencies
5. `/workspace/cq/docker-compose.yml` - Deployment configuration

## Verification Strategy

### End-to-End Testing

**SQLite (existing behavior):**
```bash
cd /workspace/cq
export CQ_JWT_SECRET=test-secret
docker compose up -d
make seed-all USER=test PASS=test123

# Verify via API
curl http://localhost:3000/health
curl http://localhost:3000/query?domains=python
```

**PostgreSQL (new capability):**
```bash
export CQ_JWT_SECRET=test-secret
export CQ_PG_PASSWORD=testpass
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d

# Verify connection pooling
for i in {1..20}; do
  curl -s http://localhost:3000/query?domains=test &
done
wait
```

### Test Coverage

- All 42 existing store tests pass for both backends
- Connection pool stress test (100 concurrent queries)
- Migration script validation (SQLite → PostgreSQL roundtrip)
- Existing database stamping test (no schema errors)

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Breaking existing deployments | SQLite remains default; CQ_DB_PATH still works; backward compatible |
| Schema migration failures | Alembic stamping handles existing DBs; migrations tested on both dialects |
| Connection pool exhaustion | Configurable limits with pre-ping validation; health checks |
| SQL dialect bugs | Comprehensive parameterized tests; testcontainers for real PostgreSQL |
| Performance regression | Benchmark tests ensure no SQLite overhead; SQLAlchemy connection pool |
| Data loss during migration | Dry-run mode, verification step, backup requirement documented |

## Timeline Estimate

| Phase | Duration | Dependencies |
|-------|----------|--------------|
| 1. SQLAlchemy Core (SQLite) | 2-3 days | None |
| 2. Alembic Migrations | 2 days | Phase 1 |
| 3. PostgreSQL Support | 1-2 days | Phase 2 |
| 4. Testing Infrastructure | 2-3 days | Phase 3 |
| 5. Documentation | 1 day | Phase 3 |
| 6. Migration Tooling | 1 day | Phase 3 |

**Total: 9-12 days** (with testing and iteration)

## Why This Plan Over Alternatives

**Why SQLAlchemy Core over raw SQL duplication:**
- Alembic requires SQLAlchemy anyway (zero extra deps)
- Single Store class = one place to change queries
- Automatic placeholder translation and connection pooling
- 90% less code duplication

**Why Alembic over ad-hoc migrations:**
- Version tracking prevents schema drift
- Handles SQLite's limited ALTER TABLE via batch mode
- Stamps existing databases without re-running DDL
- Production-grade migration framework

**Why psycopg v3 over v2:**
- Recommended for new projects by PostgreSQL community
- Better connection pooling and binary protocol
- v2 is maintenance-only mode

**Why testcontainers over mocks:**
- Tests real PostgreSQL behavior (not mocked)
- Catches dialect-specific bugs
- CI runs same tests as local development

## Success Criteria

- ✅ All 42 existing tests pass for SQLite
- ✅ All 42 existing tests pass for PostgreSQL
- ✅ Existing SQLite deployments work without config changes
- ✅ Connection pooling handles 20+ concurrent requests
- ✅ Alembic migrations apply cleanly to both backends
- ✅ Migration script transfers data with verification
- ✅ Docker Compose example deploys successfully
- ✅ Documentation covers all deployment scenarios
- ✅ No performance regression for SQLite (< 5% overhead)

## Next Steps After Plan Approval

1. Create feature branch: `feature/postgresql-backend`
2. Start with Phase 1 (SQLAlchemy Core for SQLite)
3. Get PR review after each phase for incremental validation
4. Phase 2-3 can merge as single PR (Alembic + PostgreSQL together)
5. Phase 4-6 as final PR (tests, docs, migration tool)
