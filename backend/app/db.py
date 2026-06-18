"""Database engine + session helpers (SQLModel over SQLAlchemy)."""
from sqlmodel import SQLModel, create_engine, Session
from .config import settings

# Render gives postgres URLs as postgres:// — SQLAlchemy needs postgresql://
db_url = settings.DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
engine = create_engine(db_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    # For a real product, switch to Alembic migrations. This is fine to start.
    import app.models  # noqa: F401  (register models)
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
