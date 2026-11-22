import json
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

# Isolated local SQLite: no Docker or live Telegram/AI keys needed for tests.
TEST_HOME = tempfile.mkdtemp(prefix="vpnnews-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_HOME}/test.db"
os.environ["DATA_DIR"] = f"{TEST_HOME}/data"
os.environ["COOKIE_SECURE"] = "false"

from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.main import app
from app.db import SessionLocal, Source, FeedItem, Topic, Draft
from app.core import bootstrap_code, put_setting, get_setting
from app.logic import parse_feed, similarity, add_item, analyze_pending, make_draft


def get_csrf(client):
    html = client.get("/?page=dashboard").text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, html[:1000]
    return m.group(1)


def test_rss_parser():
    rss = b'''<?xml version="1.0"?><rss version="2.0"><channel><title>News</title>
    <item><guid>post-12</guid><title>Major VPN outage in region</title>
    <description><![CDATA[<p>Connectivity problems for users.</p>]]></description>
    <pubDate>Sat, 21 Nov 2026 10:00:00 GMT</pubDate>
    <link>https://example.org/post-12</link></item></channel></rss>'''
    rows = parse_feed(rss)
    assert len(rows) == 1
    assert rows[0]["title"] == "Major VPN outage in region"
    assert rows[0]["external"] == "post-12"
    assert rows[0]["published"].year == 2026
    atom = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>atom-24</id><title>VPN availability news</title><link href="https://example.com/a"/>
    <updated>2026-11-22T12:00:00Z</updated><summary>Service warning issued today</summary>
    </entry></feed>'''
    assert parse_feed(atom)[0]["link"] == "https://example.com/a"
    assert similarity("Интернет провайдер ограничил доступ к VPN", "Провайдер ограничил доступ к VPN") > 0.6


def test_full_web_journey():
    with TestClient(app, follow_redirects=True) as client:
        start = client.get("/")
        assert "Первый запуск" in start.text
        code = bootstrap_code()
        setup = client.post("/setup", data={"code": code, "username": "admin", "password": "super-strong-test-pass"})
        assert setup.status_code == 200, setup.text[:1000]
        assert "Обзор" in setup.text
        csrf = get_csrf(client)
        blocked = client.post("/jobs/scan", data={"csrf": "INVALID"})
        assert blocked.status_code == 403
        rss = client.post("/sources/add", data={"csrf": csrf, "kind": "telegram", "title": "Tech channel", "address": "@technewsroom"})
        assert "Источник добавлен" in rss.text
        assert "@technewsroom" in client.get("/?page=sources").text
        manual = client.post("/feed/add", data={"csrf": csrf, "title": "Проблемы с доступностью VPN в регионе",
                                                "body": "Пользователи сообщили о проблемах с доступом к интернету и VPN в нескольких городах региона.",
                                                "url": "https://example.org/story"})
        assert "Материал добавлен" in manual.text
        assert "Проблемы с доступностью VPN" in client.get("/?page=feed").text
        with SessionLocal() as db:
            assert db.scalar(select(func.count(FeedItem.id))) == 1
            item = db.scalar(select(FeedItem))
            put_setting(db, "ai_api_key", "FAKE_TEST_KEY")
            put_setting(db, "telegram_bot_token", "123:FAKE")
            put_setting(db, "channel_id", "@myvpnchannel")
            db.commit()
            assert get_setting(db, "ai_api_key") == "FAKE_TEST_KEY"
            # Verify encryption at rest.
            from app.db import Setting
            assert db.get(Setting, "ai_api_key").value.startswith("enc:")
            evaluation = {"items": [{"id": item.id, "relevance": 95, "credibility": 70,
                                     "novelty": 90, "topic": "VPN: проблемы с доступностью",
                                     "category": "VPN", "summary": "Есть сообщения о сбоях.",
                                     "rationale": "Важно для подписчиков"}]}
            with patch("app.logic.call_ai", return_value=json.dumps(evaluation, ensure_ascii=False)):
                count, msg = analyze_pending(db)
            assert count == 1
            topic = db.scalar(select(Topic))
            assert topic.relevance >= 70
            draft_json = {"title": "Новости VPN", "body": "По сообщениям пользователей, в регионе замечены проблемы с доступностью интернета и VPN. Достоверной информации о причинах пока нет. Мы следим за ситуацией. Источник: https://example.org/story"}
            with patch("app.logic.call_ai", return_value=json.dumps(draft_json, ensure_ascii=False)):
                draft = make_draft(db, topic)
            draft_id = draft.id
        assert "VPN: проблемы с доступностью" in client.get("/?page=topics").text
        dpage = client.get(f"/?page=drafts&draft_id={draft_id}")
        assert "ПРЕДПРОСМОТР TELEGRAM" in dpage.text
        saved = client.post(f"/drafts/{draft_id}/save", data={"csrf": csrf, "title": "Исправленный заголовок",
                                                              "body": "Проверенная вручную новость. Источник: https://example.org/story"})
        assert "Черновик сохранён" in saved.text
        with patch("app.logic.bot_call", return_value={"message_id": 321}) as sender:
            published = client.post(f"/drafts/{draft_id}/publish", data={"csrf": csrf})
            assert "Публикация отправлена" in published.text
            sender.assert_called_once()
        with SessionLocal() as db:
            published = db.get(Draft, draft_id)
            assert published.status == "published"
            assert published.telegram_message_id == "321"
        second_publish = client.post(f"/drafts/{draft_id}/publish", data={"csrf": csrf})
        assert "уже отправлен" in second_publish.text
        client.post("/logout", data={"csrf": csrf})
        assert "С возвращением" in client.get("/").text


def test_cross_source_preserves_evidence():
    with SessionLocal() as db:
        a = db.scalar(select(Source).where(Source.address == "@technewsroom"))
        b = Source(kind="telegram", title="Second", address="@secondchannel")
        db.add(b)
        db.commit()
        t = "Публикация о работе VPN на территории города"
        assert add_item(db, source_id=a.id, external_id="one", title=t, body=t)
        assert add_item(db, source_id=b.id, external_id="one", title=t, body=t)
        assert not add_item(db, source_id=a.id, external_id="two", title=t, body=t)


def test_telegram_ingestion_and_poll_offset():
    from app.logic import ingest_channel_post, poll_telegram
    from app.db import Setting
    with SessionLocal() as db:
        update = {"chat": {"id": -100123456789, "username": "technewsroom"}, "message_id": 231,
                  "date": 1795352000, "text": "В нескольких городах страны появились сообщения о нестабильной работе интернет-подключений и VPN-сервисов."}
        assert ingest_channel_post(db, update)
        assert not ingest_channel_post(db, update)
        with patch("app.logic.bot_call", return_value=[{"update_id": 901, "channel_post": {
            **update, "message_id": 232, "text": "Позднее появилась другая новость о восстановлении доступа к сервисам и работе VPN после технических проблем."}}]) as mocked:
            assert poll_telegram(db) == 1
            assert db.get(Setting, "telegram_offset").value == "902"
            assert mocked.call_args.args[1] == "getUpdates"
        with patch("app.logic.bot_call", return_value=[]):
            assert poll_telegram(db) == 0


def test_publish_timeout_does_not_resend():
    import httpx
    from app.logic import publish_draft
    import pytest
    with SessionLocal() as db:
        draft = Draft(title="Тест отправки", body="Это тестовая публикация")
        db.add(draft)
        db.commit()
        with patch("app.logic.bot_call", side_effect=httpx.TimeoutException("timed out")) as mocked:
            with pytest.raises(httpx.TimeoutException):
                publish_draft(db, draft)
            assert mocked.call_count == 1
        db.refresh(draft)
        assert draft.status == "publishing"
        with patch("app.logic.bot_call") as mocked:
            with pytest.raises(ValueError, match="уже отправлен"):
                publish_draft(db, draft)
            mocked.assert_not_called()


def test_source_archive_then_readd():
    with SessionLocal() as db:
        source = db.scalar(select(Source).where(Source.address == "@technewsroom"))
        assert source is not None
        source_id = source.id
    with TestClient(app) as client:
        r = client.post("/login", data={"username": "admin", "password": "super-strong-test-pass"})
        assert r.status_code == 200
        csrf = get_csrf(client)
        r = client.post(f"/sources/{source_id}/delete", data={"csrf": csrf})
        assert "Источник удалён из списка" in r.text
        with SessionLocal() as db:
            source = db.get(Source, source_id)
            assert source.archived and not source.active
            assert db.scalar(select(func.count(FeedItem.id)).where(FeedItem.source_id == source_id)) >= 1
        added = client.post("/sources/add", data={"csrf": csrf, "kind": "telegram", "title": "Tech again", "address": "@technewsroom"})
        assert "Источник добавлен" in added.text
        with SessionLocal() as db:
            same = db.get(Source, source_id)
            assert not same.archived and same.active and same.title == "Tech again"
        r = client.post("/jobs/scan", data={"csrf": csrf}, headers={"Origin": "https://evil.test"})
        assert r.status_code == 403


def test_json_and_rss_safety():
    import pytest
    from app.logic import parse_json_response, check_public_feed_url
    assert parse_json_response('```json\n{"items": []}\n```') == {"items": []}
    with pytest.raises(ValueError):
        parse_feed(b'<rss><!DOCTYPE x [ <!ENTITY x "lol"> ]></rss>')
    with pytest.raises(ValueError):
        check_public_feed_url("http://127.0.0.1:7777/secrets")
