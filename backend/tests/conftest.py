import os
import sys
import pathlib

import fakeredis
import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault('PGHOST', '/tmp/pgrun')
os.environ.setdefault('PGPORT', '17799')
os.environ.setdefault('PGDATABASE', 'care_test')
os.environ.setdefault('PGUSER', 'care')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('MODEL_DIR', '/tmp/care-model-test')


@pytest.fixture(scope='session')
def fake_redis():
    server = fakeredis.FakeRedis(decode_responses=False)
    import src.db as db
    db.cache = server
    import src.main as main
    main.cache = server
    yield server


@pytest.fixture(scope='session', autouse=True)
def schema(fake_redis):
    from src.db import migrate
    migrate()


@pytest.fixture()
def clean_db(fake_redis):
    from src.db import connect
    with connect() as db:
        db.execute('''
            DO $$
            DECLARE r record;
            BEGIN
                FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP
                    EXECUTE format('TRUNCATE TABLE %I RESTART IDENTITY CASCADE', r.tablename);
                END LOOP;
            END $$;
        ''')
    yield
