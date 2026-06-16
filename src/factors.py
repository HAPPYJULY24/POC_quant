import collections
import numpy as np
from abc import ABC, abstractmethod
from typing import Optional

from src.models import AlignedPayload, SignalPayload
from src.utils import setup_logger

logger = setup_logger("factors")

class ZeroVarianceException(Exception):
    """Custom exception raised when the standard deviation of a rolling window is zero."""
    pass

class BaseFactor(ABC):
    """Abstract Polymorphic Base Class for all quant factor computations."""
    def __init__(self, name: str, lookback_period: int, symbol: str = "FCPO"):
        self.name = name
        self.lookback_period = lookback_period
        self.symbol = symbol
        # Deque for holding the variable to calculate (e.g., spread)
        self.memory = collections.deque(maxlen=lookback_period)

    def update_data(self, aligned_bar: AlignedPayload) -> None:
        """Append the primary variable of the aligned K-line to memory."""
        # For Z-Score arbitrage, we append the spread
        self.memory.append(aligned_bar.spread)

    def is_ready(self) -> bool:
        """Return True if enough history has been loaded to compute the factor."""
        return len(self.memory) >= self.lookback_period

    @abstractmethod
    def compute(self) -> float:
        """Calculate the raw factor score. Must be implemented by subclasses."""
        pass

    @abstractmethod
    def generate_signal(self, raw_score: float) -> int:
        """Map raw score to discrete trading signal (1, -1, 0). Must be implemented by subclasses."""
        pass

    def process(self, aligned_bar: AlignedPayload, atr: float) -> Optional[SignalPayload]:
        """Orchestrate state updates, calculations, signal mappings, and packaging."""
        # Clean data validation first to prevent deque poisoning
        if aligned_bar.spread is None or np.isnan(aligned_bar.spread):
            logger.warning(
                f"Dirty payload detected for factor {self.name} (spread={aligned_bar.spread}). "
                f"Intercepting to prevent deque poisoning."
            )
            return None
            
        self.update_data(aligned_bar)
        
        # Defensive boundary checking
        if not self.is_ready():
            logger.info(f"Factor {self.name} is warming up ({len(self.memory)}/{self.lookback_period} bars). Skipping.")
            return None

        # Execute Layer 2: Polymorphic Engine with ZeroVariance Protection
        try:
            raw_score = self.compute()
        except ZeroVarianceException as e:
            logger.warning(f"Circuit Breaker Triggered in {self.name}: {e}. Returning safe score 0.0.")
            raw_score = 0.0

        # Execute Layer 3: Signal Discretization
        signal = self.generate_signal(raw_score)

        # Execute Layer 4: Standardized Payload Routing
        payload = SignalPayload(
            timestamp=aligned_bar.datetime,
            factor_name=self.name,
            raw_score=raw_score,
            action_signal=signal,
            current_price=aligned_bar.fcpo_close,
            volatility_metric=atr,
            symbol=self.symbol
        )
        return payload

class ZScoreArbitrageFactor(BaseFactor):
    """Concrete factor class calculating Z-Score spreads with non-symmetrical entry boundaries."""
    def __init__(
        self, 
        name: str, 
        lookback_period: int, 
        upper_entry_threshold: float, 
        lower_entry_threshold: float, 
        exit_threshold: float,
        symbol: str = "FCPO"
    ):
        super().__init__(name, lookback_period, symbol)
        self.upper_entry_threshold = upper_entry_threshold
        self.lower_entry_threshold = lower_entry_threshold
        self.exit_threshold = exit_threshold
        
        logger.info(
            f"Initialized {name} (Lookback={lookback_period}, "
            f"UpperEntry={upper_entry_threshold}, LowerEntry={lower_entry_threshold}, Exit={exit_threshold})"
        )

    def compute(self) -> float:
        """Compute rolling Z-Score. Raises ZeroVarianceException if std is zero.
        
        This online streaming calculation maps exactly to your batch research formulas:
        # 核心代码一：计算价差 (calculated and normalized in data_ingestion.py)
        # spread = df['myx-fcpo1!_close'] - df['zl1!_close']
        
        # 核心代码二：计算 Z-Score 因子
        # df['factor'] = (spread - spread.rolling(40).mean()) / spread.rolling(40).std()
        """
        # 核心代码一：计算价差 (spreads rolling window is maintained in self.memory)
        spreads = list(self.memory)
        
        # 核心代码二：计算 Z-Score 因子 (rolling mean & rolling standard deviation over lookback_period)
        mean = float(np.mean(spreads))
        std = float(np.std(spreads))
        
        if std == 0.0:
            raise ZeroVarianceException("Standard deviation of the spread window is exactly 0.0.")
            
        current_spread = spreads[-1]
        z_score = (current_spread - mean) / std
        return z_score

    def generate_signal(self, raw_score: float) -> int:
        """Map raw Z-Score to signal using strict asymmetric boundaries."""
        # Short trigger: Spread is exceptionally high (Z > upper) -> Sell spread (Signal -1)
        if raw_score > self.upper_entry_threshold:
            return -1
        # Long trigger: Spread is exceptionally low (Z < lower) -> Buy spread (Signal 1)
        elif raw_score < self.lower_entry_threshold:
            return 1
        # Exit trigger: Spread has reverted back near mean (-exit <= Z <= exit) -> Close (Signal 0)
        elif -self.exit_threshold <= raw_score <= self.exit_threshold:
            return 0
        
        # Else: In "no-man's land" between entry and exit thresholds.
        # We return a standard placeholder value of 99 to indicate "maintain previous position state".
        # This prevents stateless factor from outputting forced close signals prematurely.
        return 99


class DynamicExpressionFactor(BaseFactor):
    """Concrete factor class that dynamically evaluates a mathematical expression string from config."""
    def __init__(
        self, 
        name: str, 
        lookback_period: int, 
        expression: str,
        upper_entry_threshold: float, 
        lower_entry_threshold: float, 
        exit_threshold: float,
        symbol: str = "FCPO",
        mode: str = "mean_reversion"
    ):
        super().__init__(name, lookback_period, symbol)
        self.expression = expression
        self.upper_entry_threshold = upper_entry_threshold
        self.lower_entry_threshold = lower_entry_threshold
        self.exit_threshold = exit_threshold
        self.mode = mode
        
        logger.info(
            f"Initialized {name} (Dynamic Expression='{expression}', Lookback={lookback_period}, "
            f"UpperEntry={upper_entry_threshold}, LowerEntry={lower_entry_threshold}, Exit={exit_threshold}, Mode={mode})"
        )

    def compute(self) -> float:
        """Evaluate the expression string dynamically with safe locals."""
        spreads = list(self.memory)
        if len(spreads) == 0:
            return 0.0
            
        local_dict = {
            "spreads": spreads,
            "mean": lambda x: float(np.mean(x)),
            "std": lambda x: float(np.std(x)) if float(np.std(x)) != 0.0 else raise_zero_variance(),
            "abs": abs,
            "np": np
        }
        
        def raise_zero_variance():
            raise ZeroVarianceException("Standard deviation of the spread window is exactly 0.0.")
            
        local_dict["raise_zero_variance"] = raise_zero_variance
        
        # Guard std
        std_val = float(np.std(spreads))
        if std_val == 0.0:
            raise ZeroVarianceException("Standard deviation of the spread window is exactly 0.0.")
            
        try:
            # Evaluate the user expression securely (restricted globals and custom math locals)
            raw_score = eval(self.expression, {"__builtins__": None}, local_dict)
            return float(raw_score)
        except ZeroDivisionError:
            raise ZeroVarianceException("Division by zero in dynamic expression.")
        except Exception as e:
            logger.error(f"Failed to evaluate dynamic expression: {self.expression}. Error: {e}")
            raise

    def generate_signal(self, raw_score: float) -> int:
        """Map raw score to signal using mode-aware boundaries."""
        if self.mode == "direct_signal":
            # Direct Signal Mode: positive triggers Long (1), negative triggers Short (-1)
            if raw_score > self.upper_entry_threshold:
                return 1
            elif raw_score < self.lower_entry_threshold:
                return -1
            elif -self.exit_threshold <= raw_score <= self.exit_threshold:
                return 0
            return 99
        else:
            # Mean Reversion Mode (default): positive triggers Short (-1), negative triggers Long (1)
            if raw_score > self.upper_entry_threshold:
                return -1
            elif raw_score < self.lower_entry_threshold:
                return 1
            elif -self.exit_threshold <= raw_score <= self.exit_threshold:
                return 0
            return 99

