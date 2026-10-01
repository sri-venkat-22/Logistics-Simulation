"""Alembic environment: URL from AEGIS_DB_URL (or `-x db_url=...`), metadata from the §7 models."""
from alembic import context
from sqlalchemy import create_engine

from services.api.app.config import settings
from services.api.app.db.models import Base

target_metadata = Base.metadata


def url() -> str:
    return context.get_x_argument(as_dictionary=True).get("db_url") or settings.db_url


def run_offline() -> None:
    context.configure(url=url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(url())
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
