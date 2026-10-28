from .sqlite import Repository as SQLiteRepository


def create_repository(path):
    if str(path).startswith(("postgresql://", "postgres://")):
        from .postgres import PostgresRepository
        return PostgresRepository(str(path))
    return SQLiteRepository(path)
