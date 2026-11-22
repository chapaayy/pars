from __future__ import annotations

import logging
import time
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .db import SessionLocal, Job, Topic, initialize_database, utcnow
from .logic import scan_sources, analyze_pending, make_draft, poll_telegram
from .core import get_setting

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("newsroom.worker")


def process_job(db, job):
    if job.kind == "scan":
        count, errors = scan_sources(db, force=True)
        return f"Загружено {count}. " + "; ".join(errors[:3])
    if job.kind == "analyze":
        count, _ = analyze_pending(db)
        return f"Проанализировано {count} материалов; остальные — по расписанию"
    if job.kind == "full":
        count, errors = scan_sources(db, force=True)
        count += poll_telegram(db)
        analyzed, _ = analyze_pending(db)
        return f"Новых {count}, проанализировано {analyzed}; остальные — по расписанию. " + "; ".join(errors[:3])
    if job.kind == "draft":
        topic = db.get(Topic, int(job.details))
        if not topic:
            raise ValueError("Тема не найдена")
        draft = make_draft(db, topic)
        return f"Черновик #{draft.id} готов"
    raise ValueError("Неизвестное задание")


_next_ai_attempt = 0.0
_next_bot_attempt = 0.0


def worker_tick(db):
    global _next_ai_attempt, _next_bot_attempt
    job = db.scalar(select(Job).where(Job.status == "queued").order_by(Job.id).limit(1))
    if job:
        job.status = "running"
        db.commit()
        try:
            job.details = process_job(db, job)
            if job.kind in ("analyze", "full"):
                _next_ai_attempt = time.monotonic() + 65
            job.status = "done"
            logger.info("job #%s %s complete", job.id, job.kind)
        except Exception as exc:
            logger.exception("Job #%s failed", job.id)
            job.status = "failed"
            job.details = (type(exc).__name__ + ": " + str(exc))[:350]
        job.finished_at = utcnow()
        db.commit()
    if time.monotonic() >= _next_bot_attempt:
        try:
            n = poll_telegram(db)
            _next_bot_attempt = time.monotonic() + 10
            if n:
                logger.info("telegram: new items=%s", n)
        except Exception as exc:
            _next_bot_attempt = time.monotonic() + 120
            logger.warning("Telegram polling failed: %s", type(exc).__name__)
    try:
        count, errors = scan_sources(db)
        if count:
            logger.info("sources: new items=%s", count)
        for error in errors:
            logger.warning("sources: %s", error[:200])
    except Exception:
        logger.exception("Source scan failed")
    if time.monotonic() >= _next_ai_attempt:
        try:
            if get_setting(db, "ai_api_key"):
                analyzed, _ = analyze_pending(db)
                _next_ai_attempt = time.monotonic() + 65
                if analyzed:
                    logger.info("ai: analyzed=%s", analyzed)
        except Exception as exc:
            _next_ai_attempt = time.monotonic() + 300
            logger.warning("AI analysis failed: %s", type(exc).__name__)


def main():
    for retry in range(90):
        try:
            with SessionLocal() as db:
                db.scalar(select(Job.id).limit(1))
            break
        except SQLAlchemyError:
            logger.info("Database not ready, retry %s", retry + 1)
            time.sleep(2)
    else:
        raise RuntimeError("Database unavailable")
    with SessionLocal() as db:
        interrupted = db.scalars(select(Job).where(Job.status == "running")).all()
        for job in interrupted:
            job.status = "failed"
            job.details = "Задание прервано перезапуском. Проверьте результаты перед повтором."
            job.finished_at = utcnow()
        if interrupted:
            db.commit()
            logger.warning("Marked %d interrupted jobs as failed", len(interrupted))
    logger.info("VPN Newsroom worker started")
    while True:
        try:
            with SessionLocal() as db:
                worker_tick(db)
        except Exception:
            logger.exception("Worker iteration failed")
        time.sleep(12)


if __name__ == "__main__":
    main()
