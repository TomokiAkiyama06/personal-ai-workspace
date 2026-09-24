import unittest
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from paw_backend.api.deps import get_session
from paw_backend.app import create_app
from paw_backend.db import Base, Database, DatabaseNotConfiguredError, DatabaseStatus

from .support import FakeDatabase, make_settings

PASSWORD = "s3cr3t-pw"
URL = f"postgresql://paw:{PASSWORD}@db.internal:5432/paw"


class UnconfiguredDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_check_reports_not_configured(self):
        database = Database(make_settings())
        self.assertFalse(database.configured)
        self.assertEqual(await database.check(), DatabaseStatus.NOT_CONFIGURED)

    async def test_engine_and_session_need_a_url(self):
        database = Database(make_settings())
        with self.assertRaises(DatabaseNotConfiguredError):
            _ = database.engine
        with self.assertRaises(DatabaseNotConfiguredError):
            database.session()


class ConfiguredDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_engine_is_lazy_async_psycopg_and_masks_the_password(self):
        database = Database(make_settings(database_url=URL, database_pool_size=3))
        self.assertTrue(database.configured)
        self.assertIsNone(database._engine)  # nothing is created at construction

        engine = database.engine
        self.assertIsInstance(engine, AsyncEngine)
        self.assertEqual(engine.dialect.driver, "psycopg")
        self.assertEqual(engine.pool.size(), 3)
        self.assertNotIn(PASSWORD, repr(engine.url))
        self.assertIs(database.engine, engine)
        await database.dispose()
        self.assertIsNone(database._engine)

    async def test_session_is_created_without_connecting(self):
        database = Database(make_settings(database_url=URL))
        async with database.session() as session:
            self.assertIsInstance(session, AsyncSession)
            self.assertIs(session.bind, database.engine)
        await database.dispose()

    async def test_dispose_without_an_engine_is_a_no_op(self):
        await Database(make_settings(database_url=URL)).dispose()


class SessionDependencyTest(unittest.TestCase):
    def build_client(self, database: Database) -> TestClient:
        app = create_app(make_settings(), database=database)
        router = APIRouter()

        @router.get("/test/session")
        async def session_route(
            session: Annotated[AsyncSession, Depends(get_session)],
        ):
            return {"session": type(session).__name__}

        app.include_router(router)
        return TestClient(app)

    def test_unconfigured_database_answers_503(self):
        response = self.build_client(FakeDatabase()).get("/test/session")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_not_configured")

    def test_configured_database_provides_a_session(self):
        database = Database(make_settings(database_url=URL))
        response = self.build_client(database).get("/test/session")
        self.assertEqual(response.json(), {"session": "AsyncSession"})


class MetadataTest(unittest.TestCase):
    def test_base_metadata_has_a_deterministic_naming_convention(self):
        self.assertEqual(Base.metadata.naming_convention["pk"], "pk_%(table_name)s")


if __name__ == "__main__":
    unittest.main()
