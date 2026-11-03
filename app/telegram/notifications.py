"""Persist before returning to trading; deliver and retry outside its execution lane.

Delivery is at least once: Telegram can accept a send whose acknowledgement is lost.
Such a delivery may be repeated; trading actions are never repeated by this worker.
"""
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Awaitable, Callable
from uuid import uuid4

import structlog
from sqlalchemy import delete, select, update
from telegram.error import BadRequest, Forbidden, RetryAfter

from app.config import database
from app.models.notification import TelegramNotification

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class NotificationClaim:
    id: int
    chat_id: int
    text: str
    reply_markup: dict | None
    token: str
    attempts: int


class NotificationStore:
    def __init__(self, bot_token: str, chat_id: int):
        # Never persist credentials. A changed bot/admin must not inherit someone else's queue.
        self.bot_key = sha256(bot_token.encode()).hexdigest()
        self.chat_id = chat_id

    async def enqueue(self, text, reply_markup=None):
        markup = reply_markup.to_dict() if reply_markup is not None else None
        async with database.get_session() as session:
            session.add(TelegramNotification(bot_key=self.bot_key, chat_id=self.chat_id,
                                             text=text, reply_markup=markup))

    async def claim(self):
        now = datetime.now(timezone.utc)
        async with database.get_session() as session:
            row = await session.scalar(select(TelegramNotification).where(
                TelegramNotification.bot_key == self.bot_key,
                TelegramNotification.chat_id == self.chat_id,
                TelegramNotification.status.in_(['PENDING', 'SENDING']))
                .order_by(TelegramNotification.id).limit(1).with_for_update())
            # Preserve FIFO even during rate-limit backoff or an unexpired delivery lease.
            if row is None or row.available_at > now:
                return None
            row.status = 'SENDING'
            row.attempts += 1
            row.claim_token = uuid4().hex
            row.available_at = now + timedelta(seconds=60)
            return NotificationClaim(row.id, row.chat_id, row.text, row.reply_markup,
                                     row.claim_token, row.attempts)

    async def finish(self, claim, *, message_id=None, error=None, retry_delay=None):
        now = datetime.now(timezone.utc)
        values = {'claim_token': None, 'last_error': error}
        if error is None:
            values.update(status='SENT', sent_at=now, telegram_message_id=message_id)
        elif retry_delay is None:
            values.update(status='FAILED')
        else:
            values.update(status='PENDING', available_at=now + timedelta(seconds=retry_delay))
        async with database.get_session() as session:
            await session.execute(update(TelegramNotification).where(
                TelegramNotification.id == claim.id, TelegramNotification.claim_token == claim.token,
                TelegramNotification.status == 'SENDING').values(**values))

    async def cleanup(self):
        async with database.get_session() as session:
            await session.execute(delete(TelegramNotification).where(
                TelegramNotification.bot_key == self.bot_key,
                TelegramNotification.chat_id == self.chat_id,
                TelegramNotification.status == 'SENT',
                TelegramNotification.sent_at < datetime.now(timezone.utc) - timedelta(days=3)))


class NotificationWorker:
    def __init__(self, store: NotificationStore, sender: Callable[[NotificationClaim], Awaitable[int | None]],
                 *, poll_interval=2, send_timeout=20):
        self.store = store
        self.sender = sender
        self.poll_interval = poll_interval
        self.send_timeout = send_timeout
        self._wake = asyncio.Event()
        self._task = None
        self._draining = False

    async def enqueue(self, text, reply_markup=None):
        await self.store.enqueue(text, reply_markup)
        self._wake.set()

    def start(self):
        if self._task is None or self._task.done():
            self._draining = False
            self._task = asyncio.create_task(self._run(), name='telegram-notification-worker')

    async def stop(self, grace_seconds=5):
        task = self._task
        if task is None:
            return
        self._draining = True
        self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace_seconds)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise
        finally:
            self._task = None

    async def _deliver(self, claim):
        try:
            async with asyncio.timeout(self.send_timeout):
                message_id = await self.sender(claim)
        except asyncio.CancelledError:
            # Leave the durable lease to expire; shutdown must never lose the row.
            raise
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'[:1200]
            delay = min(2 ** min(claim.attempts, 6), 60)
            if isinstance(exc, RetryAfter):
                retry_after = exc.retry_after
                seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else retry_after
                delay = max(1, seconds)
            elif isinstance(exc, (BadRequest, Forbidden)):
                delay = None
            logger.error('telegram_notification_delivery_failed', notification_id=claim.id,
                         error=error, retry_delay=delay)
            await self.store.finish(claim, error=error, retry_delay=delay)
        else:
            await self.store.finish(claim, message_id=message_id)

    async def _run(self):
        next_cleanup = 0
        while True:
            self._wake.clear()
            try:
                loop = asyncio.get_running_loop()
                if loop.time() >= next_cleanup:
                    next_cleanup = loop.time() + 3600
                    await self.store.cleanup()
                claim = await self.store.claim()
                if claim is not None:
                    await self._deliver(claim)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('telegram_notification_worker_failed', error=str(exc))
            if self._draining:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass
