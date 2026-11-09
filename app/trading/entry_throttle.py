"""Account-wide, transactional limits on admitting consecutive new signals."""
from datetime import datetime, timedelta
from hashlib import blake2b
from math import ceil

import structlog
from sqlalchemy import func, select, text

from app.config import database
from app.models.entry_admission import EntryAdmission

logger = structlog.get_logger(__name__)


def throttle_enabled(config):
    return config.entry_cooldown_seconds > 0 or config.max_entries_per_window > 0


def throttle_problem(config, now, admissions):
    """Evaluate a rolling window; timestamps at the exact boundary have expired."""
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('Không có thời gian database hợp lệ để kiểm tra tần suất vào lệnh')
    ages = sorted((now - stamp).total_seconds() for stamp in admissions)
    if ages and ages[0] < 0:
        return 'Lịch sử vào lệnh nằm trong tương lai; cần kiểm tra đồng hồ database'
    reasons = []
    if config.entry_cooldown_seconds and ages and ages[0] < config.entry_cooldown_seconds:
        wait = ceil(config.entry_cooldown_seconds - ages[0])
        reasons.append(f'Khoảng nghỉ vào lệnh {config.entry_cooldown_seconds} giây: còn {wait} giây')
    recent = [age for age in ages if age < config.entry_window_seconds]
    cap = config.max_entries_per_window
    if cap and len(recent) >= cap:
        wait = ceil(config.entry_window_seconds - recent[cap - 1])
        reasons.append(f'Đã đủ {cap} kèo trong {config.entry_window_seconds} giây; còn {wait} giây để có lượt mới')
    return '; '.join(reasons) or None


class EntryThrottleStore:
    async def _history(self, session, account_key, config):
        now = await session.scalar(select(func.clock_timestamp()))
        lookback = max(config.entry_cooldown_seconds,
                       config.entry_window_seconds if config.max_entries_per_window else 0)
        rows = await session.scalars(select(EntryAdmission.admitted_at).where(
            EntryAdmission.account_key == account_key,
            EntryAdmission.admitted_at > now - timedelta(seconds=lookback))
            .order_by(EntryAdmission.admitted_at.desc()).limit(max(1, config.max_entries_per_window)))
        return now, list(rows.all())

    async def check(self, account_key, config):
        async with database.get_session() as session:
            now, history = await self._history(session, account_key, config)
            return throttle_problem(config, now, history)

    async def reserve(self, account_key, signal_id, config):
        if type(signal_id) is not int or signal_id <= 0:
            raise ValueError('Thiếu signal ID để giữ lượt mở kèo')
        lock_key = int.from_bytes(blake2b(('entry-throttle:' + account_key).encode(), digest_size=8).digest(),
                                  'big', signed=True)
        async with database.get_session() as session:
            # Serialize check+insert across processes; use the DB clock after
            # waiting for the lock. The reservation commits before any MT5 send.
            await session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
            existing = await session.scalar(select(EntryAdmission.id).where(
                EntryAdmission.account_key == account_key, EntryAdmission.signal_id == signal_id))
            if existing is not None:
                return 'Kèo đã được cấp lượt mở trước đó; không gửi lại yêu cầu'
            now, history = await self._history(session, account_key, config)
            problem = throttle_problem(config, now, history)
            if problem:
                return problem
            session.add(EntryAdmission(account_key=account_key, signal_id=signal_id, admitted_at=now,
                policy={name: getattr(config, name) for name in
                    ('entry_cooldown_seconds', 'max_entries_per_window', 'entry_window_seconds')}))
        return None


class EntryThrottleMixin:
    async def entry_throttle_problem(self, account, *, reserve=False, signal_id=None):
        if not throttle_enabled(self.risk_config):
            return None
        if not account.login or not account.server:
            return 'Không xác định được tài khoản MT5 để kiểm tra tần suất mở kèo'
        key = f'{account.server}:{account.login}'
        try:
            if reserve:
                return await self.entry_throttle_store.reserve(key, signal_id, self.risk_config)
            return await self.entry_throttle_store.check(key, self.risk_config)
        except Exception as exc:
            logger.error('entry_throttle_store_failed', error=str(exc))
            return 'Không kiểm tra/lưu được lượt mở kèo vào database; chưa gửi lệnh mới'
