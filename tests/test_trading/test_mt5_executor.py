import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal
from datetime import datetime

from app.broker.mt5.executor import MT5Executor
from app.broker.base import OrderRequest


@pytest.fixture
def mock_mt5_client():
    client = MagicMock()
    # run_in_thread executes synchronously or mock
    async def mock_run(func, *args, **kwargs):
        return func(*args, **kwargs)

    client.run_in_thread = AsyncMock(side_effect=mock_run)
    return client


@pytest.fixture
def executor(mock_mt5_client):
    exec_inst = MT5Executor()
    exec_inst._client = mock_mt5_client
    return exec_inst


@pytest.mark.asyncio
async def test_resolve_symbol_exact_match(executor):
    with patch("app.broker.mt5.executor.mt5") as mock_mt5:
        sym_obj = MagicMock()
        sym_obj.name = "XAUUSD"
        mock_mt5.symbol_info.return_value = sym_obj

        resolved = await executor.resolve_symbol("XAUUSD")
        assert resolved == "XAUUSD"
        assert executor._symbol_cache["XAUUSD"] == "XAUUSD"


@pytest.mark.asyncio
async def test_resolve_symbol_candidate_exness_suffix_m(executor):
    with patch("app.broker.mt5.executor.mt5") as mock_mt5:
        def symbol_info_side_effect(name):
            if name == "XAUUSDm":
                sym = MagicMock()
                sym.name = "XAUUSDm"
                return sym
            return None

        mock_mt5.symbol_info.side_effect = symbol_info_side_effect

        resolved = await executor.resolve_symbol("XAUUSD")
        assert resolved == "XAUUSDm"
        assert executor._symbol_cache["XAUUSD"] == "XAUUSDm"

        # Subsequent call should use cache without calling mt5.symbol_info again
        mock_mt5.symbol_info.reset_mock()
        cached = await executor.resolve_symbol("XAUUSD")
        assert cached == "XAUUSDm"
        mock_mt5.symbol_info.assert_not_called()


@pytest.mark.asyncio
async def test_get_positions_matches_broker_suffix(executor):
    with patch("app.broker.mt5.executor.mt5") as mock_mt5:
        # Mock open position with symbol "XAUUSDm"
        pos_raw = MagicMock()
        pos_raw.ticket = 12345
        pos_raw.symbol = "XAUUSDm"
        pos_raw.volume = 0.01
        pos_raw.price_open = 4125.0
        pos_raw.price_current = 4120.0
        pos_raw.sl = 4140.0
        pos_raw.tp = 0.0
        pos_raw.profit = 5.0
        pos_raw.swap = 0.0
        pos_raw.commission = 0.0
        pos_raw.type = 1  # SELL
        pos_raw.time = 1700000000
        pos_raw.magic = 234000
        pos_raw.comment = "test"

        mock_mt5.positions_get.return_value = [pos_raw]
        # mt5.symbol_info returns XAUUSDm
        sym_m = MagicMock()
        sym_m.name = "XAUUSDm"
        mock_mt5.symbol_info.side_effect = lambda n: sym_m if n == "XAUUSDm" else None

        # Query with standard symbol "XAUUSD"
        positions = await executor.get_positions("XAUUSD")
        assert len(positions) == 1
        assert positions[0].symbol == "XAUUSDm"
        assert positions[0].ticket == 12345

        # Query with "#XAU"
        pos_xau = await executor.get_positions("#XAU")
        assert len(pos_xau) == 1
        assert pos_xau[0].symbol == "XAUUSDm"


@pytest.mark.asyncio
async def test_open_order_resolves_symbol(executor):
    with patch("app.broker.mt5.executor.mt5") as mock_mt5:
        sym_m = MagicMock()
        sym_m.name = "XAUUSDm"
        mock_mt5.symbol_info.side_effect = lambda n: sym_m if n == "XAUUSDm" else None

        tick_raw = MagicMock()
        tick_raw.bid = 4125.0
        tick_raw.ask = 4125.5
        tick_raw.time = 1700000000
        mock_mt5.symbol_info_tick.return_value = tick_raw

        order_res_raw = MagicMock()
        order_res_raw.retcode = 10009  # DONE
        order_res_raw.order = 99999
        order_res_raw.deal = 88888
        order_res_raw.volume = 0.01
        order_res_raw.price = 4125.0
        order_res_raw.bid = 4125.0
        order_res_raw.ask = 4125.5
        order_res_raw.comment = "Request executed"
        order_res_raw.request_id = 1
        order_res_raw.retcode_external = 0
        mock_mt5.order_send.return_value = order_res_raw

        req = OrderRequest(
            symbol="XAUUSD",
            order_type="SELL",
            volume=Decimal("0.01"),
            price=Decimal("4125.0"),
        )
        # This unit test checks routing only; real journal transactions have separate PG tests.
        async def journal(client, api, request, **kwargs):
            return await client.run_in_thread(api.order_send, request)
        with patch('app.broker.mt5.executor.send_journaled', side_effect=journal):
            res = await executor.open_order(req)
        assert res.success is True
        assert res.ticket == 99999

        # Ensure order_send received resolved symbol "XAUUSDm"
        mock_mt5.order_send.assert_called_once()
        sent_dict = mock_mt5.order_send.call_args[0][0]
        assert sent_dict["symbol"] == "XAUUSDm"


def test_mappers_handle_none_fields_safely():
    from types import SimpleNamespace
    from app.broker.mt5.mapper import (
        map_deal_info,
        map_position_info,
        map_pending_order_info,
        map_account_info,
        map_symbol_info,
        map_order_result,
    )

    mock_none_obj = SimpleNamespace(
        type=None,
        entry=None,
        time=None,
        ticket=None,
        order=None,
        position=None,
        position_id=None,
        symbol=None,
        volume=None,
        price=None,
        profit=None,
        swap=None,
        commission=None,
        reason=None,
        magic=None,
        comment=None,
        sl=None,
        tp=None,
        time_setup=None,
        volume_initial=None,
        price_open=None,
        price_current=None,
        trade_mode=None,
        login=None,
        balance=None,
        equity=None,
        margin=None,
        margin_free=None,
        leverage=None,
        server=None,
        currency=None,
        digits=None,
        name=None,
        point=None,
        volume_min=None,
        volume_max=None,
        volume_step=None,
        trade_contract_size=None,
        retcode=None,
    )

    deal = map_deal_info(mock_none_obj)
    assert deal.ticket == 0
    assert deal.order == 0
    assert deal.position_id == 0
    assert deal.reason == 0
    assert deal.magic == 0

    pos = map_position_info(mock_none_obj)
    assert pos.ticket == 0
    assert pos.magic == 0

    po = map_pending_order_info(mock_none_obj)
    assert po.ticket == 0

    acc = map_account_info(mock_none_obj)
    assert acc.login == 0
    assert acc.leverage == 1

    sym = map_symbol_info(mock_none_obj)
    assert sym.digits == 2

    res = map_order_result(mock_none_obj)
    assert res.success is False
    assert res.ticket is None

