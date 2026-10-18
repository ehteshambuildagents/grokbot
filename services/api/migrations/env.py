from alembic import context
from localcrew.config import settings
from localcrew.models import Base
from sqlalchemy import create_engine

target_metadata = Base.metadata


def url():
    return context.config.attributes.get("url") or settings().database_url


if context.is_offline_mode():
    context.configure(url=url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url()).connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
