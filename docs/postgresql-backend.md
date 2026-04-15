# PostgreSQL Backend Support

The cq server now supports PostgreSQL as an optional backend database alongside SQLite.

## Quick Start

### SQLite (Default)

No configuration needed:

```bash
export CQ_JWT_SECRET=your-secret
docker compose up
```

### PostgreSQL

Use the PostgreSQL compose override:

```bash
export CQ_JWT_SECRET=your-secret
export CQ_PG_PASSWORD=your-pg-password
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up
```

## Configuration

The server accepts database configuration via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `CQ_DATABASE_URL` | Database connection string (highest priority) | `sqlite:////data/cq.db` |
| `CQ_DB_PATH` | SQLite file path (backward compatible) | `/data/cq.db` |
| `CQ_PG_POOL_SIZE` | PostgreSQL connection pool size | `10` |
| `CQ_PG_POOL_OVERFLOW` | PostgreSQL pool overflow limit | `20` |

### Database URL Formats

**SQLite:**
```bash
CQ_DATABASE_URL=sqlite:////data/cq.db
```

**PostgreSQL:**
```bash
CQ_DATABASE_URL=postgresql://user:password@host:5432/database
```

The server automatically uses the psycopg (v3) driver for PostgreSQL connections.

## Implementation Details

### Phase 1: SQLAlchemy Core (Completed)

- Replaced raw `sqlite3` module with SQLAlchemy Engine
- Single codebase handles both SQLite and PostgreSQL
- Automatic placeholder translation (`:name` format works for both)
- Thread-safe connection pooling

### Phase 3: PostgreSQL Support (Completed)

- Added psycopg v3 driver with connection pooling
- Configurable pool size via environment variables
- Pre-ping health checks for stale connections
- Fixed `daily_counts()` for PostgreSQL compatibility

### What's Working

✅ All 48 store tests pass with SQLite
✅ PostgreSQL driver integration and URL parsing
✅ Connection pool configuration
✅ Backward compatibility with existing SQLite deployments
✅ Docker Compose configuration for both backends

### Not Yet Implemented

⏸️ **Phase 2: Alembic migrations** - Currently using `CREATE TABLE IF NOT EXISTS` (works but not production-ready for schema evolution)
⏸️ **Phase 4: Parameterized tests** - Tests only run against SQLite
⏸️ **Phase 5: Documentation** - Full deployment guide
⏸️ **Phase 6: Migration script** - SQLite → PostgreSQL data migration tool

## Known Limitations

1. **Schema Management**: Using ad-hoc `CREATE TABLE IF NOT EXISTS` instead of proper migration framework
2. **Test Coverage**: Tests run only against SQLite, not PostgreSQL
3. **Migration Path**: No automated tool to migrate existing SQLite databases to PostgreSQL

These will be addressed in future phases when moving beyond the initial demo.

## Testing

Run the test suite:

```bash
cd server/backend
uv run pytest tests/test_store.py -v
```

All 48 tests should pass.

## Architecture Decisions

### Why SQLAlchemy Core?

- Single Store class eliminates code duplication
- Automatic SQL placeholder translation
- Built-in connection pooling
- Future-proof for Alembic migrations (Phase 2)

### Why psycopg v3?

- Recommended for new PostgreSQL projects
- Better connection pooling than v2
- Active maintenance (v2 is maintenance-only)

### Why Connection Pooling?

- Production deployments often run multiple server instances
- PostgreSQL connections are expensive to create
- Pool handles concurrent requests efficiently
- Pre-ping validation catches stale connections

## Next Steps for Production

Before deploying to production with PostgreSQL:

1. Implement Alembic migrations (Phase 2)
2. Add parameterized tests for both backends (Phase 4)
3. Create migration script for existing databases (Phase 6)
4. Load test connection pool sizing
5. Set up database backups and monitoring
