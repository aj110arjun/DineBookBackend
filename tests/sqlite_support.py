from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.compiler import compiles


@compiles(UUID, "sqlite")
def sqlite_uuid_type(type_, compiler, **kw):
    # SQLite's native UUID declaration has numeric affinity. UUIDs resembling
    # scientific notation must remain text instead of being converted to floats.
    return "CHAR(32)"
