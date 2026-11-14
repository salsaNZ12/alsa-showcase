import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

# Use an ABSOLUTE path anchored to this file, not a relative one ("./alsa.db").
# A relative path depends on the working directory the server was started from:
# launching uvicorn from another folder would silently create a NEW, empty alsa.db
# there, and every user's data would appear to be gone even though the real database
# is intact elsewhere. An absolute path makes the app always open the same file,
# wherever the server is started from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SQLITE_URL = "sqlite:///" + os.path.join(BASE_DIR, "alsa.db")

# The database URL comes from the environment (Render/Railway). Without it (for example
# local development with no .env) the app falls back to a local SQLite file at the fixed
# location above.
#
# Production guard: on the host (Render sets the RENDER env var), an empty DATABASE_URL
# makes the server REFUSE to start instead of silently creating an empty SQLite file that
# would be wiped on every deploy and make customer data look lost. Local runs (no RENDER)
# keep the fallback. To deliberately use SQLite on the host, set ALLOW_SQLITE=1.
if (
    not os.getenv("DATABASE_URL")
    and os.getenv("RENDER")
    and os.getenv("ALLOW_SQLITE") != "1"
):
    raise RuntimeError(
        "DATABASE_URL belum diset di environment hosting. Server dihentikan supaya tidak "
        "memakai database SQLite kosong. Isi DATABASE_URL (Supabase) lalu deploy ulang."
    )

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL)

# connect_args are only needed for SQLite, not PostgreSQL.
#
# The PostgreSQL/Supabase branch sets a connection timeout. Without one, a slow network
# or a paused Supabase project (free-tier auto-pause) leaves every request that needs the
# database, including GET/POST /state called when the dashboard opens, hanging until the
# OS gives up (tens of seconds to minutes), so the frontend appears frozen on a blank
# screen. A short connect_timeout makes a bad connection fail fast, so the endpoint can
# answer with an error (503) instead of hanging indefinitely.
connect_args = (
    {"check_same_thread": False}
    if SQLALCHEMY_DATABASE_URL.startswith("sqlite")
    else {"connect_timeout": 5}
)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,   # test a connection before use and discard it if it has died
    pool_recycle=280,     # recycle connections before the Supabase pooler closes them first
    pool_timeout=10,      # wait at most 10 seconds for a pooled connection instead of hanging
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# SQLite concurrency: in the default rollback-journal mode, ONE open read transaction
# (SELECT) can block EVERY other write transaction (INSERT/UPDATE/DELETE) until it closes.
# This happened in practice: the /ai/context-bar endpoint queries the database first and
# then calls the AI (which can take several seconds) while still holding the connection
# open. A concurrent write, such as POST /notes sending a Team Note, was queued behind it
# and looked slow or stuck even though the network was fine.
#
# WAL (Write-Ahead Logging) mode fixes this for the local SQLite database: readers never
# block writers and writers never block readers. It is standard practice for a SQLite
# database serving concurrent web requests. It is not needed for PostgreSQL (Supabase in
# production), which is designed for concurrency.
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        # If two writers do collide (rare, but possible), busy_timeout makes SQLite wait up
        # to 5 seconds before failing with "database is locked", instead of failing at once.
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()