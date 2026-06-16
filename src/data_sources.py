import time
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple, Any
import pandas as pd
import pytz
from datetime import datetime

from src.models import BarData
from src.utils import setup_logger, to_kl_time, format_datetime

logger = setup_logger("data_sources")

# Try importing tvdatafeed
try:
    from tvDatafeed import TvDatafeed, Interval as TvInterval
    TV_AVAILABLE = True
except ImportError:
    TV_AVAILABLE = False
    logger.warning("tvdatafeed is not installed. TradingView data source will be unavailable.")

class BaseDataSource(ABC):
    @abstractmethod
    def fetch_data(self, symbol_config: dict, limit: int = 54) -> List[BarData]:
        """Fetch historical K-lines for the given asset up to limit."""
        pass

class TVDataSource(BaseDataSource):
    def __init__(self):
        self.tv = None
        if TV_AVAILABLE:
            try:
                # Initialize anonymous TvDatafeed
                self.tv = TvDatafeed()
                logger.info("TradingView anonymous client initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize TradingView client: {e}")
                self.tv = None

    def _parse_ticker(self, ticker: str) -> Tuple[str, str]:
        """Split a ticker like 'MYX:FCPO1!' into exchange ('MYX') and symbol ('FCPO1!')."""
        if ":" in ticker:
            parts = ticker.split(":", 1)
            return parts[0], parts[1]
        return "", ticker

    def _get_interval(self, poll_interval: int) -> Any:
        if not TV_AVAILABLE:
            return None
        # Map poll interval seconds to TV Interval enum
        if poll_interval <= 60:
            return TvInterval.in_1_minute
        elif poll_interval <= 300:
            return TvInterval.in_5_minute
        elif poll_interval <= 900:
            return TvInterval.in_15_minute
        elif poll_interval <= 1800:
            return TvInterval.in_30_minute
        elif poll_interval <= 3600:
            return TvInterval.in_1_hour
        else:
            return TvInterval.in_daily

    def fetch_data(self, symbol_config: dict, limit: int = 54) -> List[BarData]:
        if not TV_AVAILABLE or self.tv is None:
            raise RuntimeError("TradingView client is not available.")

        tv_ticker = symbol_config.get("tv_ticker")
        poll_interval = symbol_config.get("poll_interval_seconds", 900)
        
        exchange, symbol = self._parse_ticker(tv_ticker)
        tv_interval = self._get_interval(poll_interval)
        
        logger.info(f"Fetching TradingView data for {exchange}:{symbol} (interval={tv_interval}, limit={limit})...")
        
        # Retry mechanism: 3 attempts, 1s delay
        last_error = None
        for attempt in range(1, 4):
            try:
                df = self.tv.get_hist(
                    symbol=symbol,
                    exchange=exchange,
                    interval=tv_interval,
                    n_bars=limit
                )
                if df is not None and not df.empty:
                    return self._process_dataframe(tv_ticker, df)
                else:
                    raise ValueError(f"Empty data returned for {tv_ticker}")
            except Exception as e:
                last_error = e
                logger.warning(f"TV fetch attempt {attempt}/3 failed for {tv_ticker}: {e}")
                if attempt < 3:
                    time.sleep(1.0)
        
        raise RuntimeError(f"Failed to fetch TradingView data after 3 attempts: {last_error}")

    def _process_dataframe(self, symbol: str, df: pd.DataFrame) -> List[BarData]:
        """Convert TradingView DataFrame to uniform BarData list."""
        bars = []
        # TV returns a DataFrame with index 'datetime' which is timezone-naive (often local to the exchange or UTC depending on feed)
        # We localize it and convert to Asia/Kuala_Lumpur
        for index, row in df.iterrows():
            # Localize index
            dt = index
            if dt.tzinfo is None:
                # If naive, localize to UTC first, then convert to KL
                dt = pytz.utc.localize(dt)
            kl_dt = to_kl_time(dt)
            dt_str = format_datetime(kl_dt)
            
            bars.append(BarData(
                symbol=symbol,
                datetime=dt_str,
                open=float(row['open']),
                high=float(row['high']),
                low=float(row['low']),
                close=float(row['close']),
                volume=int(row['volume'])
            ))
        return bars

class YFinanceDataSource(BaseDataSource):
    def fetch_data(self, symbol_config: dict, limit: int = 54) -> List[BarData]:
        import yfinance as yf
        yf_ticker = symbol_config.get("yf_ticker")
        poll_interval = symbol_config.get("poll_interval_seconds", 900)
        
        # Map poll interval to yfinance interval string
        if poll_interval <= 60:
            yf_interval = "1m"
            period = "1d"
        elif poll_interval <= 300:
            yf_interval = "5m"
            period = "5d"
        elif poll_interval <= 900:
            yf_interval = "15m"
            period = "5d"
        elif poll_interval <= 1800:
            yf_interval = "30m"
            period = "1mo"
        elif poll_interval <= 3600:
            yf_interval = "60m"
            period = "1mo"
        else:
            yf_interval = "1d"
            period = "3mo"
            
        logger.info(f"Fetching yfinance data for {yf_ticker} (interval={yf_interval}, limit={limit})...")
        
        last_error = None
        for attempt in range(1, 4):
            try:
                ticker = yf.Ticker(yf_ticker)
                # We request enough periods to cover the limit
                df = ticker.history(interval=yf_interval, period=period)
                if df is not None and not df.empty:
                    # Sort chronological just in case
                    df = df.sort_index()
                    # Take the last 'limit' records
                    df = df.tail(limit)
                    return self._process_dataframe(yf_ticker, df)
                else:
                    raise ValueError(f"Empty data returned from yfinance for {yf_ticker}")
            except Exception as e:
                last_error = e
                logger.warning(f"yfinance fetch attempt {attempt}/3 failed for {yf_ticker}: {e}")
                if attempt < 3:
                    time.sleep(1.0)
                    
        raise RuntimeError(f"Failed to fetch yfinance data after 3 attempts: {last_error}")

    def _process_dataframe(self, symbol: str, df: pd.DataFrame) -> List[BarData]:
        """Convert yfinance DataFrame to uniform BarData list."""
        bars = []
        for index, row in df.iterrows():
            # yfinance returns index as DatetimeIndex (usually timezone aware)
            kl_dt = to_kl_time(index)
            dt_str = format_datetime(kl_dt)
            
            bars.append(BarData(
                symbol=symbol,
                datetime=dt_str,
                open=float(row['Open']),
                high=float(row['High']),
                low=float(row['Low']),
                close=float(row['Close']),
                volume=int(row['Volume'])
            ))
        return bars

class FallbackDataSource(BaseDataSource):
    """Orchestrates primary TV fetch with transparent failover to yfinance."""
    def __init__(self):
        self.primary = TVDataSource()
        self.fallback = YFinanceDataSource()

    def fetch_data(self, symbol_config: dict, limit: int = 54) -> List[BarData]:
        # Append poll interval globally to config dict for source mapping
        symbol_config = dict(symbol_config)
        
        # Check if primary TV data source can be used
        if TV_AVAILABLE and self.primary.tv is not None:
            try:
                return self.primary.fetch_data(symbol_config, limit)
            except Exception as e:
                logger.warning(f"Primary TradingView fetch failed for {symbol_config.get('tv_ticker')}: {e}. Switching to yfinance fallback.")
        
        # Fallback to yfinance
        try:
            return self.fallback.fetch_data(symbol_config, limit)
        except Exception as e:
            logger.error(f"Fallback yfinance fetch failed for {symbol_config.get('yf_ticker')}: {e}")
            raise e
