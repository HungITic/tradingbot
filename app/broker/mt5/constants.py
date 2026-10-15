"""
MT5 Retcodes and Trade Action Constants.
Matches MetaTrader 5 API specifications.
"""

# Trade Actions
TRADE_ACTION_DEAL = 1
TRADE_ACTION_PENDING = 5
TRADE_ACTION_SLTP = 6
TRADE_ACTION_MODIFY = 7
TRADE_ACTION_REMOVE = 8
TRADE_ACTION_CLOSE_BY = 10

# Order Types
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5

# Order Filling
ORDER_FILLING_FOK = 0  # Fill or Kill
ORDER_FILLING_IOC = 1  # Immediate or Cancel
ORDER_FILLING_RETURN = 2

# Order Time
ORDER_TIME_GTC = 0  # Good till cancelled
ORDER_TIME_DAY = 1  # Good till day
ORDER_TIME_SPECIFIED = 2

# Return Codes (Trade Retcodes)
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_CANCEL = 10007
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_ERROR = 10011
TRADE_RETCODE_TIMEOUT = 10012
TRADE_RETCODE_INVALID = 10013
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_TRADE_DISABLED = 10017
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_PRICE_CHANGED = 10020
TRADE_RETCODE_PRICE_OFF = 10021
TRADE_RETCODE_INVALID_EXPIRATION = 10022
TRADE_RETCODE_ORDER_CHANGED = 10023
TRADE_RETCODE_TOO_MANY_REQUESTS = 10024
TRADE_RETCODE_NO_CHANGES = 10025
TRADE_RETCODE_SERVER_DISABLES_AT = 10026
TRADE_RETCODE_CLIENT_DISABLES_AT = 10027
TRADE_RETCODE_LOCKED = 10028
TRADE_RETCODE_FROZEN = 10029
TRADE_RETCODE_INVALID_FILL = 10030
TRADE_RETCODE_CONNECTION = 10031
TRADE_RETCODE_ONLY_REAL = 10032
TRADE_RETCODE_LIMIT_ORDERS = 10033
TRADE_RETCODE_LIMIT_VOLUME = 10034
TRADE_RETCODE_POSITION_CLOSED = 10035

RETCODE_DESCRIPTIONS: dict[int, str] = {
    10004: "Requote",
    10006: "Request rejected",
    10007: "Request canceled by trader",
    10008: "Order placed",
    10009: "Request completed",
    10010: "Only part of request completed",
    10011: "Request processing error",
    10012: "Request canceled by timeout",
    10013: "Invalid request",
    10014: "Invalid volume in request",
    10015: "Invalid price in request",
    10016: "Invalid stops in request",
    10017: "Trade disabled",
    10018: "Market is closed",
    10019: "Not enough money to complete request",
    10020: "Prices changed",
    10021: "There are no quotes to process the request",
    10022: "Invalid order expiration date in request",
    10023: "Order state changed",
    10024: "Too frequent requests",
    10025: "No changes in request",
    10026: "Autotrading disabled by server",
    10027: "Autotrading disabled by client terminal",
    10028: "Request locked for processing",
    10029: "Order or position is frozen",
    10030: "Invalid order filling type",
    10031: "No connection with the trade server",
    10032: "Operation allowed only for live accounts",
    10033: "Number of pending orders has reached the limit",
    10034: "Volume of orders and positions has reached the limit",
    10035: "Position closed",
}

# ENUM_DEAL_REASON — https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties
DEAL_REASON_CLIENT = 0
DEAL_REASON_MOBILE = 1
DEAL_REASON_WEB = 2
DEAL_REASON_EXPERT = 3
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
DEAL_REASON_SO = 6
DEAL_REASON_ROLLOVER = 7
DEAL_REASON_VMARGIN = 8
DEAL_REASON_SPLIT = 9
DEAL_REASON_CORPORATE_ACTION = 10
