from __future__ import annotations

import pytest

from metering.config import Settings


@pytest.mark.parametrize(
    "given",
    [
        "postgres://metering:secret@db.render.internal:5432/metering",
        "postgresql://metering:secret@db.render.internal:5432/metering",
    ],
)
def test_managed_postgres_urls_are_rewritten_to_psycopg3(given: str) -> None:
    settings = Settings(_env_file=None, database_url=given)
    assert (
        settings.database_url
        == "postgresql+psycopg://metering:secret@db.render.internal:5432/metering"
    )


@pytest.mark.parametrize(
    "given",
    [
        "sqlite://",
        "sqlite:///./metering.db",
        "postgresql+psycopg://metering:metering@db:5432/metering",
    ],
)
def test_explicit_driver_urls_are_left_alone(given: str) -> None:
    assert Settings(_env_file=None, database_url=given).database_url == given
