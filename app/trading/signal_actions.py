"""Manual exits scoped to one recorded signal and a confirmed broker account."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape

from sqlalchemy import or_, select

from app.config import database
from app.models.risk import RiskEvent
from app.models.signal import Signal, SignalSource
from app.models.trade import Order, Position
from app.telegram.utils import construct_telegram_message_link


class SignalActionsMixin:
    async def _ensure_db_order_cancelled(self, mt5_ticket):
        """Idempotent: True when the stored order is CANCELLED after this call."""
        if await self._update_db_order_status(mt5_ticket, 'CANCELLED'):
            return True
        try:
            async with database.get_session() as session:
                status = await session.scalar(select(Order.status).where(Order.mt5_ticket == mt5_ticket))
        except Exception:
            return False
        return status == 'CANCELLED'

    async def linked_signal_state(self, *, opening_ids=None, since=None):
        positions = await self.broker.get_positions()
        pending = await self.broker.get_pending_orders()
        if opening_ids and since:
            # Resolve a pending order filled since the preview, even before sync
            # has projected its new position ticket into the database.
            deals = await self.broker.get_history_deals(start_time=since)
            self.remember_position_orders([d for d in deals if d.order in opening_ids])
        identities = {p.ticket for p in positions} | {p.identifier for p in positions if p.identifier}
        references = identities | {o.ticket for o in pending}
        references |= {self._position_opening_orders.get(p.identifier or p.ticket, p.ticket) for p in positions}
        rows = []
        if references:
            async with database.get_session() as session:
                rows = (await session.execute(select(Order, Position.mt5_ticket,
                    SignalSource.chat_id, Signal.telegram_message_id)
                    .outerjoin(Position, Position.order_id == Order.id)
                    .outerjoin(Signal, Signal.id == Order.signal_id)
                    .outerjoin(SignalSource, SignalSource.id == Signal.source_id)
                    .where(or_(Order.mt5_ticket.in_(references), Position.mt5_ticket.in_(identities))))).all()
        pos_orders, pending_orders, links = {}, {}, {}
        for item in positions + pending:
            is_position = hasattr(item, 'position_type')
            candidates = {}
            for order, position_ticket, chat_id, message_id in rows:
                if not isinstance(order.signal_id, int) or order.signal_id <= 0 or not order.mt5_ticket:
                    continue
                matched = order.mt5_ticket == item.ticket
                if is_position:
                    matched = matched or (position_ticket is not None and position_ticket in {item.ticket, item.identifier})
                    matched = matched or order.mt5_ticket == self._position_opening_orders.get(item.identifier or item.ticket)
                if matched:
                    candidates[order.mt5_ticket] = order
                    link = construct_telegram_message_link(chat_id, message_id) if chat_id and message_id else None
                    if link:
                        links[order.signal_id] = link
            if len(candidates) != 1:
                continue  # Conflicting/unknown membership never inherits by symbol.
            order = next(iter(candidates.values()))
            if order.symbol != item.symbol:
                continue
            if order.comment and item.comment and order.comment != item.comment:
                continue
            side = item.position_type if is_position else item.order_type.split('_')[0]
            if order.mt5_order_type is not None and side != ('BUY' if order.mt5_order_type % 2 == 0 else 'SELL'):
                continue
            if is_position and order.magic and item.magic and order.magic != item.magic:
                continue
            (pos_orders if is_position else pending_orders)[item.ticket] = order
        return {'positions': positions, 'pending': pending, 'position_orders': pos_orders,
                'pending_orders': pending_orders, 'links': links}

    async def get_setup_cards(self):
        state = await self.linked_signal_state()
        display_positions = await self.group_native_positions(state['positions'])
        groups = {}
        for items, memberships, key in ((display_positions, state['position_orders'], 'positions'),
                                         (state['pending'], state['pending_orders'], 'orders')):
            for item in items:
                order = memberships.get(item.ticket)
                if key == 'positions' and item.members:
                    children = [memberships.get(child.ticket) for child in item.members]
                    if any(o is None or order is None or o.signal_id != order.signal_id for o in children):
                        order = None
                signal_id = order.signal_id if order else None
                group_key = ('signal', signal_id) if signal_id else ('unlinked', item.ticket)
                group = groups.setdefault(group_key, {
                    'signal_id': signal_id, 'name': f'Kèo #{signal_id}' if signal_id else f'Chưa liên kết · #{item.ticket}',
                    'positions': [], 'orders': [], 'message_link': state['links'].get(signal_id),
                    'actions_muted': self.is_signal_muted(signal_id)})
                group[key].append(item)
        return list(groups.values())

    @staticmethod
    def _signal_exit_members(state, signal_id, mode):
        positions = [(p, state['position_orders'][p.ticket]) for p in state['positions']
                     if p.ticket in state['position_orders'] and state['position_orders'][p.ticket].signal_id == signal_id]
        pending = [(p, state['pending_orders'][p.ticket]) for p in state['pending']
                   if p.ticket in state['pending_orders'] and state['pending_orders'][p.ticket].signal_id == signal_id]
        return (positions if mode == 'all' else []), pending

    async def preview_signal_exit(self, signal_id, mode):
        if type(signal_id) is not int or signal_id <= 0 or mode not in {'all', 'pending'}:
            raise ValueError('Phạm vi đóng kèo không hợp lệ')
        account = await self.broker.get_account_info()
        state = await self.linked_signal_state()
        positions, pending = self._signal_exit_members(state, signal_id, mode)
        members = {}
        for item, order in positions + pending:
            key = str(order.mt5_ticket)
            members[key] = str(Decimal(members.get(key, '0')) + item.volume)
        if not members:
            raise ValueError('Signal này không còn lệnh phù hợp đang mở')
        label = 'Đóng vị thế và hủy lệnh chờ' if mode == 'all' else 'Hủy lệnh chờ'
        lines = [f'<b>{label} · Kèo #{signal_id}</b>', 'Chưa thực thi.',
                 f'Tài khoản: {account.login} · {escape(account.server)}',
                 f'{len(positions)} vị thế · {len(pending)} lệnh chờ']
        link = state['links'].get(signal_id)
        if link:
            lines.append(f'<a href="{escape(link, quote=True)}">Xem signal gốc</a>')
        details = [f'Đóng {escape(item.symbol)} {item.position_type} · {item.volume} lot · #{item.ticket}'
                   for item, _ in positions]
        details += [f'Hủy {escape(item.symbol)} {item.order_type} · {item.volume} lot · #{item.ticket}'
                    for item, _ in pending]
        # Telegram message size is bounded even for a signal containing many legs.
        text = '\n'.join(lines + details[:12])
        if len(details) > 12:
            text += '\n… Danh sách rút gọn; xác nhận áp dụng toàn bộ số vị thế/lệnh chờ nêu trên.'
        if mode == 'all':
            text += '\nHủy lệnh chờ trước, sau đó đóng khối lượng còn lại. Lệnh chờ đã khớp từ lúc xem trước cũng thuộc phạm vi đóng.'
        else:
            text += '\nLệnh chờ đã khớp sẽ được giữ dưới dạng vị thế.'
        return {'text': text, 'signal_id': signal_id, 'mode': mode,
                'account': {'login': account.login, 'server': account.server}, 'members': members,
                'created_at': datetime.now(timezone.utc).isoformat()}

    async def execute_signal_exit(self, signal_id, mode, account, members, created_at):
        async with self._execution_lock:
            if type(signal_id) is not int or signal_id <= 0 or mode not in {'all', 'pending'} or not members:
                return '⛔ Phạm vi đóng kèo không hợp lệ.'
            now = datetime.now(timezone.utc)
            since = datetime.fromisoformat(created_at)
            if since.tzinfo is None or not 0 <= (now - since).total_seconds() <= 300:
                return '⏳ Yêu cầu đã hết hạn. Hãy xem trước lại kèo.'
            current = await self.broker.get_account_info()
            if account != {'login': current.login, 'server': current.server}:
                return '⛔ Tài khoản MT5 đã thay đổi. Hãy xem trước lại kèo.'
            ids = {int(key) for key in members}

            async def fresh():
                current = await self.broker.get_account_info()
                if account != {'login': current.login, 'server': current.server}:
                    raise ValueError('Tài khoản MT5 đã thay đổi; đã dừng thao tác')
                state = await self.linked_signal_state(opening_ids=ids, since=since - timedelta(seconds=30))
                positions, pending = self._signal_exit_members(state, signal_id, mode)
                totals = {}
                for item, order in positions + pending:
                    key = str(order.mt5_ticket)
                    if key not in members:
                        raise ValueError('Kèo có thêm lệnh từ lúc xem trước; hãy xem trước lại')
                    totals[key] = totals.get(key, Decimal('0')) + item.volume
                    if totals[key] > Decimal(members[key]):
                        raise ValueError('Khối lượng kèo tăng từ lúc xem trước; hãy xem trước lại')
                return positions, pending

            positions, pending = await fresh()
            if not positions and not pending:
                return 'ℹ️ Signal không còn lệnh trong phạm vi đã chọn.'
            # Commit the manual scope before the first broker mutation.
            async with database.get_session() as session:
                session.add(RiskEvent(signal_id=signal_id, event_type='SIGNAL_MANUAL_EXIT',
                    rule_name='manual_signal_exit', details={'mode': mode, 'account': account,
                    'members': members, 'created_at': created_at}))
            cancelled, closed, failures, stopped = 0, 0, [], False
            attempted = set()
            for previous, _ in pending:
                _, live_pending = await fresh()
                found = next(((p, o) for p, o in live_pending if p.ticket == previous.ticket), None)
                if found is None:
                    continue
                item, order = found
                try:
                    result = await self.broker.cancel_order(item.ticket)
                except BaseException:
                    self.risk_config.enabled = False
                    raise
                attempted.add(('cancel', item.ticket))
                if result.raw.get('uncertain'):
                    self.risk_config.enabled = False
                    stopped = True
                await self._insert_management_action(signal_id, item.ticket, 'CANCEL',
                    {'reason': 'MANUAL_SIGNAL_EXIT', 'opening_order': order.mt5_ticket},
                    'EXECUTED' if result.success and not result.raw.get('uncertain') else 'FAILED',
                    {'comment': result.comment, 'retcode': result.retcode, 'uncertain': bool(result.raw.get('uncertain'))})
                if result.success and not result.raw.get('uncertain'):
                    cancelled += 1
                    # The broker journal already marks the row CANCELLED in the
                    # same transaction as the MT5 acknowledgement, so a zero-row
                    # update is the normal path, not a failure.
                    if not await self._ensure_db_order_cancelled(item.ticket):
                        stopped = True
                        failures.append(f'#{item.ticket}: Đã hủy trên MT5 nhưng chưa cập nhật được database')
                else:
                    # A definitive "already filled" rejection may have raced the
                    # preview. Never retry cancel; the refreshed position is closed below.
                    if not stopped:
                        _, current_pending = await fresh()
                        stopped = any(p.ticket == item.ticket for p, _ in current_pending)
                    if stopped:
                        failures.append(f'#{item.ticket}: {escape(result.comment)}')
                if stopped:
                    break
            # Re-read after cancellation: a pending leg may now be a position.
            while mode == 'all' and not stopped:
                positions, _ = await fresh()
                remaining = [(p, o) for p, o in positions if ('close', p.ticket) not in attempted]
                if not remaining:
                    break
                pos, order = remaining[0]
                attempted.add(('close', pos.ticket))
                try:
                    result = await self.broker.close_position(pos.ticket, pos.volume)
                except BaseException:
                    self.risk_config.enabled = False
                    raise
                if result.raw.get('uncertain'):
                    self.risk_config.enabled = False
                    stopped = True
                    result = replace(result, success=False)
                await self.record_management(signal_id, pos, 'CLOSE', result,
                    reason='MANUAL_CLOSE', scope='SIGNAL', volume=str(pos.volume), opening_order=order.mt5_ticket)
                partial = result.retcode == 10010 or (result.volume is not None and result.volume < pos.volume)
                if result.success and not partial and not result.raw.get('uncertain'):
                    closed += 1
                else:
                    failures.append(f'#{pos.ticket}: ' + ('Chưa đóng hết khối lượng' if partial else escape(result.comment)))
                if not result.success or partial:
                    stopped = True
            message = f'Kèo #{signal_id}: đã hủy {cancelled} lệnh chờ, đóng {closed} vị thế.'
            if failures:
                message += '\nChưa hoàn tất:\n' + '\n'.join(failures[:12])
            if stopped:
                message += '\nĐã dừng các yêu cầu còn lại; hãy đối chiếu MT5 trước khi thao tác tiếp.'
            return message
