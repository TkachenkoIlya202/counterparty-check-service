import os
from fastapi.testclient import TestClient
import pytest

from main import DB_PATH, app, init_db


@pytest.fixture(autouse=True)
def setup_db():
    if DB_PATH.exists():
        os.remove(DB_PATH)
    init_db()
    yield
    if DB_PATH.exists():
        os.remove(DB_PATH)


client = TestClient(app)


def test_get_company_success():
    res = client.get("/api/v1/companies/7707083893")
    assert res.status_code == 200
    data = res.json()
    assert data["inn"] == "7707083893"
    assert data["name"] == 'ПАО "Сбербанк"'
    assert data["status"] == "ACTIVE"
    assert data["risk_level"] == "LOW"
    assert data["source"] == "external_api"


def test_cache_hits_on_second_request():
    first = client.get("/api/v1/companies/7707083893")
    assert first.status_code == 200
    assert first.json()["source"] == "external_api"

    second = client.get("/api/v1/companies/7707083893")
    assert second.status_code == 200
    assert second.json()["source"] == "cache"


def test_invalid_inn_returns_400():
    res = client.get("/api/v1/companies/7707083894")
    assert res.status_code == 400
    assert res.json()["detail"] == "INVALID_INN"


def test_external_service_outage_returns_502():
    res = client.get("/api/v1/companies/0000000000")
    assert res.status_code == 502
    assert res.json()["detail"] == "EXTERNAL_SERVICE_UNAVAILABLE"
