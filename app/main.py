from __future__ import annotations

import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import quote, urlparse, parse_qs

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .core import (DATA_DIR, bootstrap_code, check_password, create_session, delete_bootstrap_code,
                   get_session, get_setting, hash_password, is_bootstrapped, normalize_channel,
                   public_setting_summary, put_setting, session_hash, valid_channel_id)
from .db import (Admin, Draft, FeedItem, Job, LoginSession, Source, Topic, SessionLocal,
                 initialize_database, utcnow)
from .logic import add_item, check_public_feed_url, publish_draft

logger = logging.getLogger("newsroom.app")
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    initialize_database()
    with SessionLocal() as db:
        if not is_bootstrapped(db):
            logger.warning("== ПЕРВЫЙ ЗАПУСК: КОД НАСТРОЙКИ: %s ==", bootstrap_code())
    yield


app = FastAPI(title="VPN Newsroom", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def get_db():
    with SessionLocal() as db:
        yield db


def go(page: str = "dashboard", notice: str = "", error: bool = False):
    link = "/?page=" + quote(page)
    if notice:
        link += "&notice=" + quote(notice[:350])
    if error:
        link += "&error=1"
    return RedirectResponse(link, status_code=303)


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Внешний API ответил ошибкой HTTP {exc.response.status_code}. Проверьте токен, модель и права."
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return "Сервис не ответил. Попробуйте позже."
    if isinstance(exc, ValueError):
        return str(exc)[:250]
    return "Не удалось выполнить действие. Подробности смотрите в логах."


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path == "/health":
        return await call_next(request)
    # Restrict write operations to same origin even if browser sends a cookie.
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin", "")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in ("https", "http") or parsed.netloc.lower() != request.headers.get("host", "").lower():
                return HTMLResponse("Недопустимый Origin", status_code=403)
    with SessionLocal() as db:
        registered = is_bootstrapped(db)
        request.state.registered = registered
        request.state.login_session = get_session(db, request.cookies.get("vpnnews_session"))
    if path.startswith("/static/"):
        return await call_next(request)
    if not registered and path != "/setup":
        return RedirectResponse("/setup", status_code=303)
    if registered and path == "/setup":
        return RedirectResponse("/login", status_code=303)
    if registered and not request.state.login_session and path not in ("/login",):
        return RedirectResponse("/login", status_code=303)
    if registered and request.state.login_session and path == "/login":
        return RedirectResponse("/", status_code=303)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


async def fields(request: Request) -> dict[str, str]:
    """Parse our URL-encoded forms without multipart or uploads."""
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Ожидалась HTML-форма")
    raw = await request.body()
    if len(raw) > 60000:
        raise HTTPException(status_code=413, detail="Слишком длинная форма")
    try:
        query = parse_qs(raw.decode("utf-8", errors="strict"), keep_blank_values=True)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Некорректная кодировка") from exc
    return {key: values[-1] for key, values in query.items()}


def csrf_guard(request: Request, value: str):
    session = getattr(request.state, "login_session", None)
    if not session or not secrets.compare_digest(session.csrf_token, value):
        raise HTTPException(status_code=403, detail="Неверный CSRF-токен")


def view(request: Request, template: str, **context):
    context["request"] = request
    context["csrf"] = request.state.login_session.csrf_token if request.state.login_session else ""
    return templates.TemplateResponse(request, template, context)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    return view(request, "auth.html", setup=True, error="")


@app.post("/setup")
async def setup(request: Request, db: Session = Depends(get_db)):
    form = await fields(request)
    code, username, password = form.get("code", ""), form.get("username", ""), form.get("password", "")
    if is_bootstrapped(db):
        return RedirectResponse("/login", 303)
    if not secrets.compare_digest(code.strip(), bootstrap_code()) or not re.fullmatch(r"[a-zA-Z0-9_.-]{3,40}", username):
        return view(request, "auth.html", setup=True, error="Неверный код установки или имя пользователя")
    if len(password) < 12 or len(password) > 200:
        return view(request, "auth.html", setup=True, error="Пароль должен быть длиной от 12 символов")
    admin = Admin(username=username, password_hash=hash_password(password))
    db.add(admin)
    db.commit()
    delete_bootstrap_code()
    token, _ = create_session(db, admin.id)
    resp = RedirectResponse("/", 303)
    resp.set_cookie("vpnnews_session", token, httponly=True, secure=os.getenv("COOKIE_SECURE", "true") == "true",
                    samesite="strict", max_age=7 * 86400, path="/")
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return view(request, "auth.html", setup=False, error="")


@app.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await fields(request)
    username, password = form.get("username", ""), form.get("password", "")
    admin = db.scalar(select(Admin).where(Admin.username == username))
    if not admin or not check_password(password, admin.password_hash):
        return view(request, "auth.html", setup=False, error="Неверный логин или пароль")
    token, _ = create_session(db, admin.id)
    response = RedirectResponse("/", 303)
    response.set_cookie("vpnnews_session", token, secure=os.getenv("COOKIE_SECURE", "true") == "true",
                        httponly=True, samesite="strict", max_age=7 * 86400, path="/")
    return response


@app.post("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    csrf = (await fields(request)).get("csrf", "")
    csrf_guard(request, csrf)
    session = request.state.login_session
    db.query(LoginSession).filter(LoginSession.id == session.id).delete()
    db.commit()
    response = RedirectResponse("/login", 303)
    response.delete_cookie("vpnnews_session")
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request, page: str = "dashboard", notice: str = "", error: int = 0,
          offset: int = 0, draft_id: int = 0, db: Session = Depends(get_db)):
    allowed = {"dashboard", "feed", "topics", "drafts", "sources", "settings"}
    page = page if page in allowed else "dashboard"
    offset = max(0, offset)
    stats = {
        "items": db.scalar(select(func.count(FeedItem.id))) or 0,
        "pending": db.scalar(select(func.count(FeedItem.id)).where(FeedItem.status == "pending")) or 0,
        "topics": db.scalar(select(func.count(Topic.id)).where(Topic.status == "open")) or 0,
        "drafts": db.scalar(select(func.count(Draft.id)).where(Draft.status == "draft")) or 0,
        "published": db.scalar(select(func.count(Draft.id)).where(Draft.status == "published")) or 0,
        "sources": db.scalar(select(func.count(Source.id)).where(Source.active.is_(True))) or 0,
    }
    topic_query = select(Topic)
    if page == "dashboard":
        topic_query = topic_query.where(Topic.status == "open")
    topics = db.scalars(topic_query.order_by((Topic.status != "open"), Topic.relevance.desc(), Topic.updated_at.desc()).offset(offset if page == "topics" else 0).limit(50 if page == "topics" else 8)).all()
    items = db.scalars(select(FeedItem).order_by(FeedItem.id.desc()).offset(offset if page == "feed" else 0).limit(60 if page == "feed" else 8)).all()
    drafts = db.scalars(select(Draft).order_by(Draft.id.desc()).limit(100)).all()
    sources = db.scalars(select(Source).where(Source.archived.is_(False)).order_by(Source.id.desc())).all()
    jobs = db.scalars(select(Job).order_by(Job.id.desc()).limit(8)).all()
    edit_draft = db.get(Draft, draft_id) if draft_id else None
    return view(request, "index.html", page=page, notice=notice, error=bool(error), stats=stats,
                topics=topics, items=items, drafts=drafts, sources=sources, jobs=jobs,
                settings=public_setting_summary(db), edit_draft=edit_draft, offset=offset,
                has_pending_job=any(job.status in ("queued", "running") for job in jobs))


@app.post("/sources/add")
async def source_add(request: Request, db: Session = Depends(get_db)):
    form = await fields(request)
    csrf, kind, title, address = (form.get(key, "") for key in ("csrf", "kind", "title", "address"))
    csrf_guard(request, csrf)
    try:
        if kind not in {"rss", "telegram"} or len(title.strip()) < 2 or len(title) > 200:
            raise ValueError("Укажите корректный тип и название")
        if kind == "rss":
            address = check_public_feed_url(address.strip())
        else:
            address = normalize_channel(address)
            if not valid_channel_id(address):
                raise ValueError("Telegram: укажите @channel или числовой ID -100...")
            if address == normalize_channel(get_setting(db, "channel_id")):
                raise ValueError("Собственный канал публикации нельзя добавлять в источники")
        existing = db.scalar(select(Source).where(Source.kind == kind, Source.address == address))
        if existing:
            if not existing.archived:
                raise ValueError("Этот источник уже добавлен")
            existing.archived, existing.active, existing.title = False, True, title.strip()
        else:
            db.add(Source(kind=kind, title=title.strip(), address=address))
        db.commit()
        return go("sources", "Источник добавлен")
    except Exception as exc:
        return go("sources", friendly_error(exc), True)


@app.post("/sources/{source_id}/toggle")
async def source_toggle(request: Request, source_id: int, db: Session = Depends(get_db)):
    csrf = (await fields(request)).get("csrf", "")
    csrf_guard(request, csrf)
    source = db.get(Source, source_id)
    if source:
        source.active = not source.active
        db.commit()
    return go("sources", "Статус источника изменён")


@app.post("/sources/{source_id}/delete")
async def source_delete(request: Request, source_id: int, db: Session = Depends(get_db)):
    csrf = (await fields(request)).get("csrf", "")
    csrf_guard(request, csrf)
    source = db.get(Source, source_id)
    if source:
        # Existing items stay in the database with their source for reference.
        source.active = False
        source.archived = True
        db.commit()
    return go("sources", "Источник удалён из списка; история сохранена")


@app.post("/feed/add")
async def manual_add(request: Request, db: Session = Depends(get_db)):
    form = await fields(request)
    csrf, title, body, url = (form.get(key, "") for key in ("csrf", "title", "body", "url"))
    csrf_guard(request, csrf)
    try:
        url = url.strip()
        if url and urlparse(url).scheme not in ("http", "https"):
            raise ValueError("Ссылка должна начинаться с https:// или http://")
        unique = "manual:" + secrets.token_hex(12)
        ok = add_item(db, source_id=None, external_id=unique, title=title, body=body, url=url)
        return go("feed", "Материал добавлен" if ok else "Материал слишком короткий или уже существует", not ok)
    except Exception as exc:
        return go("feed", friendly_error(exc), True)


@app.post("/jobs/{kind}")
async def enqueue_job(request: Request, kind: str, db: Session = Depends(get_db)):
    csrf = (await fields(request)).get("csrf", "")
    csrf_guard(request, csrf)
    if kind not in {"scan", "analyze", "full"}:
        raise HTTPException(400)
    if kind in {"full", "analyze"} and not get_setting(db, "ai_api_key"):
        return go("settings", "Сначала укажите ключ ИИ", True)
    if db.scalar(select(Job.id).where(Job.kind == kind, Job.status.in_(["queued", "running"]))):
        return go("dashboard", "Такая задача уже выполняется")
    db.add(Job(kind=kind))
    db.commit()
    return go("dashboard", "Задача поставлена в очередь")


@app.post("/topics/{topic_id}/draft")
async def draft_job(request: Request, topic_id: int, db: Session = Depends(get_db)):
    csrf = (await fields(request)).get("csrf", "")
    csrf_guard(request, csrf)
    topic = db.get(Topic, topic_id)
    if not topic:
        return go("topics", "Тема не найдена", True)
    if not get_setting(db, "ai_api_key"):
        return go("settings", "Сначала укажите ключ ИИ", True)
    if db.scalar(select(Job.id).where(Job.kind == "draft", Job.details == str(topic_id), Job.status == "queued")):
        return go("topics", "Черновик уже создаётся")
    db.add(Job(kind="draft", details=str(topic_id)))
    db.commit()
    return go("drafts", "Генерация черновика добавлена в очередь")


@app.post("/drafts/{draft_id}/save")
async def draft_save(request: Request, draft_id: int, db: Session = Depends(get_db)):
    form = await fields(request)
    csrf, title, body = (form.get(key, "") for key in ("csrf", "title", "body"))
    csrf_guard(request, csrf)
    draft = db.get(Draft, draft_id)
    if not draft or draft.status != "draft":
        return go("drafts", "Редактировать можно только неопубликованный черновик", True)
    if not 1 <= len(body.strip()) <= 4096:
        return go("drafts", "Текст должен быть от 1 до 4096 символов", True)
    draft.title, draft.body = title[:250].strip(), body.strip()
    db.commit()
    return go("drafts", "Черновик сохранён")


@app.post("/drafts/{draft_id}/publish")
async def draft_publish(request: Request, draft_id: int, db: Session = Depends(get_db)):
    csrf = (await fields(request)).get("csrf", "")
    csrf_guard(request, csrf)
    draft = db.get(Draft, draft_id)
    if not draft:
        return go("drafts", "Черновик не найден", True)
    try:
        message_id = publish_draft(db, draft)
        return go("drafts", f"Публикация отправлена. ID сообщения {message_id}")
    except Exception as exc:
        return go("drafts", friendly_error(exc) + " Если статус «Отправляется», проверьте канал вручную перед повтором.", True)


@app.post("/drafts/{draft_id}/delete")
async def draft_delete(request: Request, draft_id: int, db: Session = Depends(get_db)):
    csrf = (await fields(request)).get("csrf", "")
    csrf_guard(request, csrf)
    draft = db.get(Draft, draft_id)
    if draft and draft.status == "draft":
        db.delete(draft)
        db.commit()
    return go("drafts", "Черновик удалён")


@app.post("/settings")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    form = await fields(request)
    csrf, ai_provider, ai_model, ai_api_key, telegram_bot_token, channel_id, brand_name, brand_tone = (
        form.get(key, "") for key in ("csrf", "ai_provider", "ai_model", "ai_api_key", "telegram_bot_token",
                                      "channel_id", "brand_name", "brand_tone"))
    scan_minutes = int(form.get("scan_minutes", "15")) if form.get("scan_minutes", "15").isdigit() else 0
    csrf_guard(request, csrf)
    try:
        if ai_provider not in ("groq", "gemini"):
            raise ValueError("Выберите Groq или Gemini")
        if not 5 <= scan_minutes <= 1440:
            raise ValueError("Интервал от 5 до 1440 минут")
        if channel_id and not valid_channel_id(channel_id):
            raise ValueError("Канал для публикаций должен быть @channel или ID -100...")
        if ai_model and not re.fullmatch(r"[\w./-]{2,100}", ai_model):
            raise ValueError("Некорректное имя модели")
        values = {"ai_provider": ai_provider, "ai_model": ai_model.strip() or
                  ("openai/gpt-oss-20b" if ai_provider == "groq" else "gemini-3.1-flash-lite"),
                  "channel_id": normalize_channel(channel_id) if channel_id else "",
                  "brand_name": brand_name[:120], "brand_tone": brand_tone[:900],
                  "scan_minutes": str(scan_minutes)}
        for key, value in values.items():
            put_setting(db, key, value)
        if ai_api_key.strip():
            put_setting(db, "ai_api_key", ai_api_key.strip())
        if telegram_bot_token.strip():
            put_setting(db, "telegram_bot_token", telegram_bot_token.strip())
        db.commit()
        return go("settings", "Настройки сохранены")
    except Exception as exc:
        return go("settings", friendly_error(exc), True)
