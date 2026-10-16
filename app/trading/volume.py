"""Executed lot turnover for a Vietnam calendar month, including both entry and exit deals."""
from decimal import Decimal

from app.core.timezone import to_vietnam_datetime

ZERO = Decimal('0')


def update_ledger(ledger, deal):
    if deal.position_id <= 0:
        return
    previous = ledger.get(deal.position_id, ZERO) if deal.entry == 'IN' else ledger.get(deal.position_id)
    signed = deal.volume if deal.deal_type == 'BUY' else -deal.volume
    if deal.entry == 'INOUT' and (previous is None or previous * signed >= 0 or deal.volume < abs(previous)):
        previous = None
    ledger[deal.position_id] = None if previous is None else previous + signed


def monthly_volume(deals, now, history=None):
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # The broker may pad the history query range; filter and deduplicate actual executions.
    unique = {d.ticket: d for d in deals if d.symbol and d.deal_type in {'BUY', 'SELL'}
              and start <= to_vietnam_datetime(d.time) <= now}
    selected = sorted(unique.values(), key=lambda d: (to_vietnam_datetime(d.time), d.ticket))
    ledger = {}
    if history is not None:
        prior = {d.ticket: d for d in history if d.symbol and d.deal_type in {'BUY', 'SELL'}
                 and to_vietnam_datetime(d.time) < start}
        for d in sorted(prior.values(), key=lambda d: (to_vietnam_datetime(d.time), d.ticket)):
            if d.position_id > 0 and d.entry in {'IN', 'OUT', 'OUT_BY', 'INOUT'}:
                update_ledger(ledger, d)
    opened = closed = unallocated = ZERO
    for d in selected:
        if not d.volume.is_finite() or d.volume < 0:
            raise ValueError('Khối lượng deal không hợp lệ')
        if d.entry == 'IN':
            opened += d.volume
        elif d.entry in {'OUT', 'OUT_BY'}:
            closed += d.volume
        elif d.entry == 'INOUT':
            previous = ledger.get(d.position_id)
            incoming = d.volume if d.deal_type == 'BUY' else -d.volume
            if (d.position_id > 0 and previous is not None and previous * incoming < 0
                    and d.volume >= abs(previous)):
                closed += abs(previous)
                opened += d.volume - abs(previous)
            else:
                # Combined turnover remains exact even if older history is inaccessible.
                unallocated += d.volume
        else:
            raise ValueError('Không xác định được loại deal mở/đóng')
        if d.position_id > 0:
            update_ledger(ledger, d)
    return {'available': True, 'month': start.strftime('%m/%Y'), 'opened': opened,
            'closed': closed, 'unallocated': unallocated, 'total': opened + closed + unallocated}
