"""Broker-side TP legs, with durable membership in opening-order comments + signal FK.

A group is an exact signal/entry branch, never an inference from price or symbol.
All sends still use the broker's write-ahead command journal.
"""
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
from collections import defaultdict

from app.broker.base import OrderResult

SUFFIXES = (':tp1', ':tp2')
BRANCHES = {'bot-min-entry', 'bot-max-entry', 'bot-override-min', 'bot-override-max', 'bot-market'}


def branch_comment(comment):
    comment = comment or ''
    return comment[:-4] if comment.endswith(SUFFIXES) else comment


def native_comment(comment):
    return bool(comment and comment.endswith(SUFFIXES) and branch_comment(comment) in BRANCHES)


def native_key(order):
    if order is None or not native_comment(getattr(order, 'comment', None)) or not isinstance(order.signal_id, int):
        return None
    branch = branch_comment(order.comment).replace('bot-override-min', 'bot-min-entry').replace('bot-override-max', 'bot-max-entry')
    return order.signal_id, branch, order.symbol


class NativeTPMixin:
    def branch_has_ticket(self, position, tickets):
        return any(p.ticket in tickets or self._position_opening_orders.get(p.identifier or p.ticket) in tickets
                   for p in (position.members or (position,)))

    def native_leg_count(self, tp1, tp2):
        return 2 if (tp1 is not None and tp2 is not None and self.risk_config.tp1_enabled
                     and 0 < self.risk_config.default_partial_close_percent < 100) else 1

    async def prepare_native_orders(self, req):
        if req.tp1 is None or req.tp2 is None or native_comment(req.comment):
            return [req]
        account = await self.broker.get_account_info()
        if account.raw.get('margin_mode') != 2:
            raise ValueError('TP1/TP2 trên broker cần tài khoản hedging được MT5 xác nhận (margin_mode=2)')
        spec = await self.broker.get_symbol_info(req.symbol)
        step, minimum = spec.volume_step, spec.volume_min
        if step <= 0 or minimum <= 0 or req.volume % step or not minimum <= req.volume <= spec.volume_max:
            raise ValueError('Khối lượng/bước lot không hợp lệ để tách TP1/TP2')
        ratio = self.risk_config.default_partial_close_percent if self.risk_config.tp1_enabled else Decimal('0')
        first = (req.volume * ratio / 100 / step).to_integral_value(rounding=ROUND_DOWN) * step
        second = req.volume - first
        if 0 < ratio < 100 and (first < minimum or second < minimum):
            raise ValueError('Không đủ lot để tách TP1/TP2 theo tỷ lệ đã cài; chưa mở nhánh lệnh')
        legs = []
        for suffix, volume, tp in [(':tp1', first, req.tp1), (':tp2', second, req.tp2)]:
            if volume > 0:
                legs.append(replace(req, volume=volume, tp=tp, comment=req.comment + suffix))
        return legs

    async def open_entry_branch(self, req):
        """Sequential sends. Stop on any failure; never replay a partially opened branch."""
        legs = await self.prepare_native_orders(req)
        if len(legs) == 1 and legs[0] is req:
            return await self.broker.open_order(req)
        # Durable crash guard: a restart between child sends must not silently
        # resume opening new risk. The broker journal remains authoritative per send.
        guarded = len(legs) > 1
        if guarded and not await self.save_risk_config_to_db(
                updated_by='NATIVE_TP_BEGIN', config=self.risk_config.with_updates({'enabled': False})):
            self.risk_config.enabled = False
            return OrderResult(False, comment='Không lưu được trạng thái bảo vệ trước khi tách TP; chưa gửi MT5')
        executed = []
        for leg in legs:
            if not self.risk_config.enabled:
                break
            try:
                result = await self.broker.open_order(leg)
            except BaseException:
                self.risk_config.enabled = False
                raise
            if result.success:
                executed.append((leg, result))
                await self._record_db_order(req.signal_id, result, leg,
                    status='FILLED' if leg.order_type in {'BUY', 'SELL'} else 'PLACED')
            # IOC partial fills cannot preserve the planned split. No automatic top-up.
            if not result.success or result.retcode == 10010 or (leg.order_type in {'BUY', 'SELL'}
                    and result.volume is not None and result.volume != leg.volume):
                if guarded or executed or result.raw.get('uncertain'):
                    self.risk_config.enabled = False
                return OrderResult(False, ticket=result.ticket, retcode=result.retcode,
                    comment='Nhánh TP chưa hoàn tất; đã dừng gửi thêm. ' + result.comment,
                    raw={'native_children': executed, 'uncertain': bool(result.raw.get('uncertain')), 'partial_branch': bool(executed)})
        if len(executed) != len(legs):
            self.risk_config.enabled = False
            return OrderResult(False, comment='Bot đã dừng khi đang mở nhánh TP', raw={'native_children': executed, 'partial_branch': bool(executed)})
        volume = sum((r.volume or leg.volume for leg, r in executed), Decimal('0'))
        price = sum(((r.price or leg.price) * (r.volume or leg.volume) for leg, r in executed), Decimal('0')) / volume
        if guarded and not await self.save_risk_config_to_db(updated_by='NATIVE_TP_COMPLETE'):
            self.risk_config.enabled = False
            return OrderResult(False, ticket=executed[0][1].ticket, volume=volume,
                comment='Đã mở các phần TP nhưng chưa khôi phục được cấu hình; đã dừng mở mới',
                raw={'native_children': executed, 'partial_branch': True})
        return OrderResult(True, ticket=executed[0][1].ticket, price=price, volume=volume,
                           comment='Đã đặt TP trực tiếp trên broker', raw={'native_children': executed})

    async def group_native_positions(self, positions):
        if not positions:
            return positions
        orders = await self.position_orders(positions)
        signal_ids = {o.signal_id for o in orders.values() if native_key(o)}
        siblings = list(orders.values())
        if signal_ids:
            from sqlalchemy import select
            from app.models.trade import Order
            from app.config.database import get_session
            async with get_session() as session:
                siblings = (await session.scalars(select(Order).where(Order.signal_id.in_(signal_ids)))).all()
        groups = defaultdict(list)
        for pos in positions:
            order = orders.get(pos.ticket)
            key = native_key(order)
            # DB and live broker labels must agree when broker retains a label.
            if key and pos.comment and pos.comment != order.comment:
                key = None
            groups[(key, pos.position_type) if key else ('ticket', pos.ticket)].append(pos)
        result = []
        for children in groups.values():
            children.sort(key=lambda p: p.ticket)
            total = sum((p.volume for p in children), Decimal('0'))
            first = children[0]
            if not native_key(orders.get(first.ticket)):
                result.extend(children)
                continue
            mean = lambda name: sum((getattr(p, name) * p.volume for p in children), Decimal('0')) / total
            result.append(replace(first, volume=total, open_price=mean('open_price'),
                profit=sum((p.profit for p in children), Decimal('0')),
                swap=sum((p.swap for p in children), Decimal('0')),
                commission=sum((p.commission for p in children), Decimal('0')),
                sl=first.sl if all(p.sl == first.sl for p in children) else None,
                tp=None, members=tuple(children),
                initial_volume=sum((o.volume for o in siblings if native_key(o) == native_key(orders[first.ticket])
                                    and o.volume and o.status not in {'CANCELLED', 'REJECTED', 'ERROR'}), Decimal('0'))))
        return result

    async def get_open_positions(self):
        return await self.group_native_positions(await self.broker.get_positions())

    async def close_native_fraction(self, pos, percentage):
        pct = Decimal(str(percentage))
        if not pct.is_finite() or not 0 <= pct <= 100:
            return OrderResult(False, ticket=pos.ticket, comment='Tỷ lệ chốt phải từ 0 đến 100%'), Decimal('0')
        spec = await self.broker.get_symbol_info(pos.symbol)
        step, minimum = spec.volume_step, spec.volume_min
        if step <= 0 or minimum <= 0:
            return OrderResult(False, ticket=pos.ticket, comment='Bước lot không hợp lệ'), Decimal('0')
        target = pos.volume if pct == 100 else (pos.volume * pct / 100 / step).to_integral_value(rounding=ROUND_DOWN) * step
        if target == 0:
            return OrderResult(False, ticket=pos.ticket, comment='Không có khối lượng cần chốt', raw={'skipped': pct == 0}), Decimal('0')
        # Allocate the total once, rather than rounding a percentage independently per leg.
        remaining, plan = target, []
        for child in sorted(pos.members, key=lambda p: (not p.comment.endswith(':tp1'), p.ticket)):
            amount = min(remaining, child.volume)
            if 0 < child.volume - amount < minimum:
                amount = max(Decimal('0'), child.volume - minimum)
            if amount >= minimum:
                plan.append((child, amount))
                remaining -= amount
        if remaining != 0:
            return OrderResult(False, ticket=pos.ticket, comment='Không đủ lot để chốt đúng tỷ lệ; chưa đóng nhánh'), Decimal('0')
        results, closed = [], Decimal('0')
        for child, amount in plan:
            result = await self.broker.close_position(child.ticket, amount)
            results.append((child, result, amount))
            if result.success:
                closed += result.volume if result.volume is not None else amount
                await self.cancel_dual_entry_pending_orders_on_close(child.ticket, reason='Nhánh entry đã có lần chốt')
            if not result.success or result.retcode == 10010:
                if result.raw.get('uncertain'):
                    self.risk_config.enabled = False
                break
        success = closed == target and all(r.success for _, r, _ in results)
        return OrderResult(success, ticket=pos.ticket, volume=closed,
            comment=f'Đã chốt {closed}/{target} lot của nhánh', raw={'member_results': results,
                'uncertain': any(r.raw.get('uncertain') for _, r, _ in results)}), closed

    async def native_tp_breakeven(self, positions, orders):
        """Only a broker TP deal on the TP1 leg can trigger BE on its TP2 sibling."""
        if not self.risk_config.auto_breakeven_on_partial:
            return
        native = [p for p in positions if native_key(orders.get(p.ticket))]
        if not native:
            return
        from sqlalchemy import select
        from app.models.trade import Order, Trade
        from app.config.database import get_session
        signal_ids = {orders[p.ticket].signal_id for p in native}
        async with get_session() as session:
            rows = (await session.scalars(select(Order).where(Order.signal_id.in_(signal_ids)))).all()
            first_tickets = [o.mt5_ticket for o in rows if native_key(o) and o.comment.endswith(':tp1')]
            identities = (await session.execute(select(Trade.mt5_order_ticket, Trade.mt5_position_id)
                .where(Trade.mt5_order_ticket.in_(first_tickets), Trade.mt5_position_id.isnot(None)))).all()
        identifiers = dict(identities)
        _, completed = await self.management_history(native)
        live_opening_orders = {self._position_opening_orders.get(p.identifier or p.ticket, p.ticket) for p in positions}
        by_key = defaultdict(list)
        for order in rows:
            if native_key(order):
                by_key[native_key(order)].append(order)
        for pos in native:
            order = orders[pos.ticket]
            if not order.comment.endswith(':tp2') or pos.ticket in self._tp1_completed_tickets or pos.ticket in completed:
                continue
            first_legs = [o for o in by_key[native_key(order)] if o.comment.endswith(':tp1')]
            reached = False
            for first in first_legs:
                if first.mt5_ticket in live_opening_orders:
                    continue
                # Resolve using official position IDs; opening ticket isn't always the position ticket.
                deal_history = await self.broker.get_history_deals(position_id=identifiers.get(first.mt5_ticket, first.mt5_ticket))
                self.remember_position_orders(deal_history)
                reached = any(d.entry in {'OUT', 'OUT_BY'} and d.reason == 5
                    and self._position_opening_orders.get(d.position_id) == first.mt5_ticket for d in deal_history)
                if reached:
                    break
            if reached:
                result = await self.apply_stop(pos, self.breakeven_target(pos, positions, {}))
                await self.record_management(order.signal_id, pos, 'MOVE_SL', result, reason='TP1_BREAKEVEN', sl=str(result.price))
                if result.success:
                    await self.record_management(order.signal_id, pos, 'TP1_COMPLETE', OrderResult(True), reason='TP1_COMPLETE')
                    self._tp1_completed_tickets.add(pos.ticket)

    async def group_native_history(self, history, active_positions, positions_available):
        if not history:
            return history
        from sqlalchemy import select
        from app.models.trade import Order
        from app.config.database import get_session
        tickets = {t for row in history for t in row.opening_order_tickets}
        if not tickets:
            return history
        async with get_session() as session:
            matched = (await session.scalars(select(Order).where(Order.mt5_ticket.in_(tickets)))).all()
            signal_ids = {o.signal_id for o in matched if native_key(o)}
            if not signal_ids:
                return history
            siblings = (await session.scalars(select(Order).where(Order.signal_id.in_(signal_ids)))).all()
        orders = {o.mt5_ticket: o for o in siblings if native_key(o)}
        rows_by_key = defaultdict(list)
        result = []
        for row in history:
            keys = {native_key(orders[t]) for t in row.opening_order_tickets if t in orders}
            if len(keys) == 1:
                rows_by_key[(keys.pop(), row.position_type)].append(row)
            else:
                result.append(row)
        for (key, side), rows in rows_by_key.items():
            members = [o for o in orders.values() if native_key(o) == key]
            opening = {o.mt5_ticket for o in members}
            live = [p for p in active_positions
                    if self._position_opening_orders.get(p.identifier or p.ticket, p.ticket) in opening]
            first = min(rows, key=lambda r: r.ticket)
            closed = sum((r.volume for r in rows), Decimal('0'))
            total = sum((o.volume for o in members if o.volume is not None and o.status not in {'CANCELLED', 'REJECTED', 'ERROR'}), Decimal('0'))
            known_prices = [o for o in members if o.price is not None and o.volume]
            basis = sum((o.volume for o in known_prices), Decimal('0'))
            entry_prices = {t: row.open_price for row in rows if row.open_price is not None for t in row.opening_order_tickets}
            entry_prices.update({self._position_opening_orders.get(p.identifier or p.ticket, p.ticket): p.open_price for p in live})
            result.append(replace(first, ticket=min(opening), volume=closed, total_volume=max(total, closed),
                open_price=(sum((entry_prices.get(o.mt5_ticket, o.price) * o.volume for o in known_prices), Decimal('0')) / basis) if basis else first.open_price,
                close_price=sum((r.close_price * r.volume for r in rows), Decimal('0')) / closed,
                profit=sum((r.profit for r in rows), Decimal('0')),
                swap=sum((r.swap for r in rows), Decimal('0')),
                commission=sum((r.commission for r in rows), Decimal('0')),
                net_pnl=sum((r.net_pnl for r in rows), Decimal('0')),
                open_time=min((r.open_time for r in rows if r.open_time), default=None),
                close_time=max(r.close_time for r in rows), deals_count=sum(r.deals_count for r in rows),
                is_fully_closed=(not live if positions_available else None),
                opening_order_tickets=tuple(sorted(opening)),
                partial_deals=sorted([d for r in rows for d in r.partial_deals], key=lambda d: d.time)))
        return result
