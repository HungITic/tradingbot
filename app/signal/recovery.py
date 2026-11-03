"""Single-entry fallback for rejected gold signals. SL/TP are never inferred."""
from decimal import Decimal

from app.signal.models import ParsedNewOrder
from app.signal.validator import SignalValidator


class SignalRecoveryError(ValueError):
    pass


def entry_recovery_candidate(order, config):
    entries = [e for e in (order.entry_min, order.entry_max) if e is not None]
    return (config.signal_recovery_enabled and order.symbol.upper().startswith('XAUUSD')
            and len(entries) == 2 and all(e.is_finite() and e > 0 for e in entries)
            and abs(entries[0] - entries[1]) > config.recovery_max_entry_gap)


def recover_order(order: ParsedNewOrder, quote: Decimal, config, point: Decimal = Decimal('.01')):
    """Return (execution order, decision) only for an invalid source signal.

    Entry errors discard the distant entry, retaining an original price.
    SL/TP errors are rejected for the user to send corrected levels.
    """
    validator = SignalValidator(config.require_sl, config.require_tp)
    valid, reason = validator.validate_new_order(order)
    if valid:
        return order, None
    if not entry_recovery_candidate(order, config):
        raise SignalRecoveryError(reason)
    entries = list(dict.fromkeys(e for e in (order.entry_min, order.entry_max) if e is not None))
    prices = entries + [p for p in (order.stop_loss, order.take_profit, order.take_profit_2) if p is not None]
    if (not entries or any(not p.is_finite() or p <= 0 for p in prices)
            or not quote.is_finite() or quote <= 0 or not point.is_finite() or point <= 0):
        raise SignalRecoveryError(reason)
    if ((config.require_sl and order.stop_loss is None)
            or (config.require_tp and order.take_profit is None and not order.raw_tp_text)):
        raise SignalRecoveryError(reason)

    def acceptable(candidate):
        if not validator.validate_new_order(candidate)[0]:
            return False
        remaining = [p for p in (candidate.entry_min, candidate.entry_max) if p is not None]
        if max(remaining) - min(remaining) > config.recovery_max_entry_gap:
            return False
        for name, limit in (('stop_loss', config.recovery_max_sl_distance),
                            ('take_profit', config.recovery_max_tp_distance),
                            ('take_profit_2', config.recovery_max_tp_distance)):
            level = getattr(candidate, name)
            if level is not None and any(abs(level - entry) > limit for entry in remaining):
                return False
        return True

    nearest = sorted(entries, key=lambda entry: abs(entry - quote))
    if abs(nearest[0] - quote) > config.recovery_max_quote_distance:
        raise SignalRecoveryError('Không có entry gốc đủ gần giá hiện tại để xử lý ngoại lệ')
    if len(entries) == 2 and abs(entries[0] - entries[1]) > config.recovery_max_entry_gap:
        if abs(nearest[0] - quote) == abs(nearest[1] - quote):
            raise SignalRecoveryError('Hai entry cách giá hiện tại bằng nhau; chưa chọn entry')
        candidate = order.model_copy(update={'entry_min': nearest[0], 'entry_max': None})
        if nearest[0] % point:
            raise SignalRecoveryError('Entry gần nhất không đúng bước giá của symbol')
        if not acceptable(candidate):
            raise SignalRecoveryError('Entry gần nhất vẫn không phù hợp với SL/TP; chưa thực thi')
    else:
        raise SignalRecoveryError(reason)
    decision = {'mode': 'single_entry', 'original': order.model_dump(mode='json'),
                'order': candidate.model_dump(mode='json'), 'reason': reason}
    return candidate, decision


def recoverable_source(order, config):
    """Classify reply roots only; execution still requires a fresh broker quote."""
    for anchor in (order.entry_min, order.entry_max):
        if anchor is None:
            continue
        try:
            _, decision = recover_order(order, anchor, config)
            if decision:
                return True
        except SignalRecoveryError:
            pass
    return False
