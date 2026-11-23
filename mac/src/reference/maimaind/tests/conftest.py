import pytest

from brain import db, fixtures, ingest


@pytest.fixture
def transcripts(tmp_path):
    return fixtures.write(tmp_path / "transcripts")


@pytest.fixture
def conn(tmp_path, transcripts):
    c = db.connect(tmp_path / "index.sqlite")
    ingest.sync(c, transcripts)
    ingest.sync_people(c, fixtures.PEOPLE)
    yield c
    c.close()
