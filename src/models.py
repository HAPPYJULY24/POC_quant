from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import math

def safe_round(val, decimals):
    if val is None:
        return None
    try:
        if math.isnan(val):
            return val
    except TypeError:
        pass
    return round(val, decimals)

@dataclass
class BarData:
    symbol: str
    datetime: str  # Format: "YYYY-MM-DD HH:MM:SS" (Kuala Lumpur Time)
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self):
        # Round prices to standard 4 decimal places for consistency
        self.open = safe_round(self.open, 4)
        self.high = safe_round(self.high, 4)
        self.low = safe_round(self.low, 4)
        self.close = safe_round(self.close, 4)

@dataclass
class AlignedPayload:
    datetime: str  # Format: "YYYY-MM-DD HH:MM:SS" (Kuala Lumpur Time)
    fcpo_close: float
    zl_close_usd: float
    fx_rate: float
    zl_close_myr: float
    spread: float
    factor_score: Optional[float] = None
    signal: int = 0  # 1: Long, -1: Short, 0: Neutral/Exit

    def __post_init__(self):
        self.fcpo_close = safe_round(self.fcpo_close, 4)
        self.zl_close_usd = safe_round(self.zl_close_usd, 4)
        self.fx_rate = safe_round(self.fx_rate, 6)
        self.zl_close_myr = safe_round(self.zl_close_myr, 4)
        self.spread = safe_round(self.spread, 4)
        if self.factor_score is not None:
            self.factor_score = safe_round(self.factor_score, 4)

@dataclass
class SignalPayload:
    timestamp: str  # Format: "YYYY-MM-DD HH:MM:SS"
    factor_name: str
    raw_score: float
    action_signal: int  # 1, -1, 0
    current_price: float
    volatility_metric: float  # e.g., ATR value
    symbol: str = "FCPO"  # Primary traded asset, defaults to FCPO
    is_emergency_rollover: bool = False
