"""Settings routes for personal and corporation alert filters."""

from datetime import UTC, datetime

import pytest

pytest.importorskip("httpx")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app.api import routes_settings
from app.db.base import Base
from app.db.models import Character, CorpSetting
from app.notifications.location_filter import load_filter
from app.security.csrf import ensure_csrf_session_id, issue_csrf_token
from tests.universe_fixture import make_universe


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'routes.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    universe = make_universe(tmp_path, monkeypatch)
    roles = {"value": {"Director"}}

    monkeypatch.setattr(routes_settings, "SessionLocal", Session)
    monkeypatch.setattr(routes_settings, "load_universe", lambda data_dir: universe)
    monkeypatch.setattr(routes_settings, "_fetch_live_corp_roles", lambda character: (roles["value"], None))

    now = datetime.now(UTC).replace(tzinfo=None)
    with Session() as db:
        db.add(Character(character_id=7, character_name="Test Pilot", corporation_id=500, scopes="", monitoring_enabled=True,
                         personal_mention_text="", use_corp_webhook=True, is_active=True, created_at=now, updated_at=now))
        db.add(CorpSetting(corporation_id=500, webhook_url="https://discord.com/api/webhooks/x", mention_text="",
                           allowed_roles='["Director"]', updated_by_character_id=7, updated_at=now))
        db.commit()

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test")
    app.include_router(routes_settings.router, prefix="/settings")

    @app.get("/login")
    def login(request: Request):
        request.session["character"] = {"character_id": 7}
        return {"csrf": issue_csrf_token(ensure_csrf_session_id(request.session))}

    client = TestClient(app)
    csrf = client.get("/login").json()["csrf"]
    return client, csrf, Session, roles


def _post(client, url, csrf, **fields):
    return client.post(url, data={"csrf_token": csrf, **fields}, follow_redirects=False)


def _error(response) -> str:
    location = response.headers["location"]
    return location if "error=" in location else ""


def test_personal_filter_saves_canonical_names(env):
    client, csrf, Session, _ = env
    r = _post(client, "/settings/me/filter", csrf, personal_filter_list_mode="whitelist",
              personal_filter_places="alpha one\nb2", personal_filter_range_origin="a1", personal_filter_range_jumps="5")
    assert not _error(r)
    with Session() as db:
        saved = load_filter(db.get(Character, 7).alert_filter)
    assert [p.name for p in saved.places] == ["Alpha One", "B2"]
    assert (saved.range_origin.name, saved.range_max_jumps) == ("A1", 5)


def test_unknown_name_is_rejected_and_nothing_saved(env):
    client, csrf, Session, _ = env
    r = _post(client, "/settings/me/filter", csrf, personal_filter_list_mode="blacklist", personal_filter_places="Nowhere")
    assert "Nowhere" in _error(r)
    with Session() as db:
        assert db.get(Character, 7).alert_filter == ""


def test_corp_filter_requires_corp_webhook_permission(env):
    client, csrf, Session, roles = env
    roles["value"] = {"Trader"}
    r = _post(client, "/settings/corp/filter", csrf, corp_filter_list_mode="whitelist", corp_filter_places="Alpha")
    assert "permitted" in _error(r)
    with Session() as db:
        assert db.get(CorpSetting, 500).alert_filter == ""

    roles["value"] = {"Director"}
    r = _post(client, "/settings/corp/filter", csrf, corp_filter_list_mode="whitelist", corp_filter_places="Alpha")
    assert not _error(r)
    with Session() as db:
        assert [p.name for p in load_filter(db.get(CorpSetting, 500).alert_filter).places] == ["Alpha"]


def test_turning_filter_off_clears_it(env):
    client, csrf, Session, _ = env
    _post(client, "/settings/corp/filter", csrf, corp_filter_list_mode="whitelist", corp_filter_places="Alpha")
    r = _post(client, "/settings/corp/filter", csrf, corp_filter_list_mode="off", corp_filter_places="Alpha")
    assert not _error(r)
    with Session() as db:
        assert db.get(CorpSetting, 500).alert_filter == ""


def test_missing_csrf_is_rejected(env):
    client, _, Session, _ = env
    r = _post(client, "/settings/me/filter", "bad-token", personal_filter_list_mode="whitelist", personal_filter_places="Alpha")
    assert "CSRF" in _error(r)
