"""
Data models and configurations for the Risk Engine.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class RiskStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WARNING = "WARNING"


class RiskResult(BaseModel):
    """Result from evaluating a single risk rule."""

    rule_name: str
    status: RiskStatus
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class RiskDecision(BaseModel):
    """Overall decision produced by the Risk Engine."""

    approved: bool
    results: list[RiskResult] = Field(default_factory=list)
    rejection_reason: str = ""
    calculated_lot: Decimal = Decimal("0.01")


class RiskConfig(BaseModel):
    """Dynamic risk configuration — tunable via Personal Bot at runtime."""

    enabled: bool = True
    max_open_positions: int = 5
    max_lot: Decimal = Decimal("0.05")
    min_lot: Decimal = Decimal("0.01")
    default_lot: Decimal = Decimal("0.01")
    max_daily_loss_percent: Decimal = Decimal("10.0")
    require_sl: bool = True
    require_tp: bool = False
    allowed_symbols: list[str] = Field(default_factory=lambda: ["XAUUSD", "XAUUSDm", "XAUUSD.s", "XAUUSDc", "XAUUSD.c"])
    signal_expire_seconds: int = 300
    duplicate_timeout_seconds: int = 10

    # Position Management Rules (Tunable via Personal Bot)
    default_partial_close_percent: Decimal = Decimal("50.0")
    bad_entry_partial_close_percent: Decimal = Decimal("50.0")
    auto_breakeven_on_partial: bool = True
    breakeven_offset_price: Decimal = Decimal("0.0")
    allow_multiple_partial_closes: bool = False  # False = Chặn chốt từng phần lặp lại cho cùng 1 vị thế
    repeat_partial_close_keywords: list[str] = Field(default_factory=list)

    # Explicit position policy, shared by TP1, commands and Telegram buttons.
    breakeven_mode: Literal["ticket"] = "ticket"
    breakeven_only_improve: bool = True
    require_explicit_close: bool = True
    generic_partial_uses_default: bool = True
    tp1_enabled: bool = True
    tp1_min_volume_action: Literal["breakeven", "skip"] = "breakeven"
    auto_cancel_pending_on_full_close: bool = True

    # Dual Entry Setup Rules (Min Entry + Max Entry)
    enable_dual_entry: bool = True
    lot_min_entry: Decimal = Decimal("0.01")
    lot_max_entry: Decimal = Decimal("0.01")
    max_slippage_entry: Decimal = Decimal("1.0")  # Dung sai tối đa khi giá chạy bất lợi (1 giá / 100 points)

    # Broker Server Timezone Offset
    broker_utc_offset_hours: int = 3  # Chênh lệch UTC của sàn (PUPrime EET/EEST: +3 hè, +2 đông)

    @field_validator('repeat_partial_close_keywords', mode='before')
    @classmethod
    def parse_repeat_keywords(cls, value):
        if isinstance(value, str):
            value = [] if value.strip() == '[]' else re.split(r'[,\n]+', value)
        if isinstance(value, list):
            keywords = []
            for phrase in value:
                if not isinstance(phrase, str):
                    raise ValueError('Từ khóa chốt tiếp phải là chuỗi văn bản')
                phrase = ' '.join(unicodedata.normalize('NFC', phrase).casefold().split())
                if phrase and phrase not in keywords:
                    keywords.append(phrase)
            return keywords
        return value

    @model_validator(mode="after")
    def check_limits(self):
        lots = (self.min_lot, self.max_lot, self.default_lot, self.lot_min_entry, self.lot_max_entry)
        if any(not value.is_finite() or value <= 0 for value in lots):
            raise ValueError("Lot phải là số hữu hạn lớn hơn 0")
        if self.min_lot > self.max_lot:
            raise ValueError("Lot tối thiểu không được lớn hơn lot tối đa")
        if any(not self.min_lot <= v <= self.max_lot for v in lots[2:]):
            raise ValueError("Lot vào lệnh phải nằm trong giới hạn min_lot/max_lot")
        if self.max_open_positions < 1 or self.signal_expire_seconds < 1 or self.duplicate_timeout_seconds < 0:
            raise ValueError("Số vị thế và thời hạn phải dương; thời gian lọc trùng không được âm")
        if not self.max_daily_loss_percent.is_finite() or not 0 < self.max_daily_loss_percent <= 100:
            raise ValueError("Giới hạn lỗ ngày phải lớn hơn 0 và không vượt 100%")
        if not self.default_partial_close_percent.is_finite() or not 0 <= self.default_partial_close_percent <= 100:
            raise ValueError("Chốt một phần phải nằm trong khoảng 0 đến 100%")
        if not self.bad_entry_partial_close_percent.is_finite() or not 0 <= self.bad_entry_partial_close_percent <= 100:
            raise ValueError("Chốt bớt entry xấu phải nằm trong khoảng 0 đến 100%")
        if not self.max_slippage_entry.is_finite() or self.max_slippage_entry < 0:
            raise ValueError("Dung sai giá phải hữu hạn và không âm")
        if not self.breakeven_offset_price.is_finite() or self.breakeven_offset_price < 0 or not -12 <= self.broker_utc_offset_hours <= 14:
            raise ValueError("Offset không hợp lệ")
        if not self.allowed_symbols or any(not str(s).strip() for s in self.allowed_symbols):
            raise ValueError("Phải có ít nhất một mã giao dịch hợp lệ")
        return self

    def with_updates(self, updates: dict[str, Any]) -> "RiskConfig":
        unknown = set(updates) - set(type(self).model_fields)
        if unknown:
            raise ValueError("Cấu hình không tồn tại: " + ", ".join(sorted(unknown)))
        values = self.model_dump()
        values.update(updates)
        if isinstance(values["allowed_symbols"], str):
            values["allowed_symbols"] = [s.strip().upper() for s in values["allowed_symbols"].split(",") if s.strip()]
        return type(self).model_validate(values)

    def update_field(self, key: str, value: Any) -> bool:
        try:
            candidate = self.with_updates({key: value})
        except (ValueError, TypeError):
            return False
        setattr(self, key, getattr(candidate, key))
        if key == "broker_utc_offset_hours":
            from app.core.timezone import set_broker_utc_offset_hours
            set_broker_utc_offset_hours(candidate.broker_utc_offset_hours)
        return True
