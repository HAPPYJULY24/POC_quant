import collections
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timedelta

from src.models import BarData, AlignedPayload
from src.config import ConfigManager
from src.utils import setup_logger, get_kl_time, parse_datetime, format_datetime, to_kl_time
from src.data_sources import FallbackDataSource
import src.database as db

logger = setup_logger("data_ingestion")

def calculate_tr(high: float, low: float, prev_close: float) -> float:
    """Calculate the True Range (TR)."""
    return max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close)
    )

def calculate_atr_latest(bars: List[BarData], period: int = 14) -> float:
    """Calculate the latest Average True Range (ATR) from a list of BarData."""
    if len(bars) < period + 1:
        # Fallback to standard high-low standard deviation if not enough data
        closes = [b.close for b in bars]
        if len(closes) > 1:
            return float(np.std(closes))
        return 1.0  # Safe default to avoid division by zero
    
    tr_list = []
    for i in range(1, len(bars)):
        tr = calculate_tr(bars[i].high, bars[i].low, bars[i-1].close)
        tr_list.append(tr)
        
    # Standard ATR is often calculated as smoothed moving average,
    # but simple SMA of TR over the period is very stable and robust.
    return float(np.mean(tr_list[-period:]))

class IngestionPipeline:
    def __init__(self, config: ConfigManager, data_source: Optional[FallbackDataSource] = None):
        self.config = config
        self.db_uri = self.config.system.get("database_path", "sqlite:///bursa_poc.db")
        self.data_source = data_source or FallbackDataSource()
        
        # Strategy variables
        self.lookback_period = self.config.strategy.get("lookback_period", 40)
        self.warmup_limit = self.lookback_period + 14  # 54 periods
        
        # Deque memories for ATR and factor preparation (thread-safe, sliding)
        self.memory: Dict[str, collections.deque] = {
            "FCPO": collections.deque(maxlen=self.warmup_limit),
            "ZL": collections.deque(maxlen=self.warmup_limit),
            "USDMYR": collections.deque(maxlen=self.warmup_limit)
        }
        
        # Dispatch callback triggered when new aligned data is persisted
        self.dispatcher_callback: Optional[Callable[[AlignedPayload], None]] = None
        
        # Callbacks triggered when contract rollover is detected
        self.rollover_callbacks: List[Callable[[], None]] = []

    def register_dispatcher(self, callback: Callable[[AlignedPayload], None]) -> None:
        """Register downstream handler (the algorithm brain) to be woken upon aligned data write."""
        self.dispatcher_callback = callback

    def bootstrap(self) -> None:
        """Execute bootstrapping stage: initialize schemas, restore portfolio, preload history, catch up gaps."""
        logger.info("Initializing Database schemas...")
        db.init_db(self.db_uri)
        
        # Ensure portfolio state is initialized
        initial_cap = self.config.account.get("initial_capital_rm", 100000.0)
        strategy_id = self.config.strategy.get("strategy_id", "POC_ZSCORE_FCPO")
        db.load_portfolio_state(self.db_uri, strategy_id, initial_cap)
        logger.info(f"Portfolio state initialized/loaded for {strategy_id}.")
        
        # Preload history to warm up memory deques
        self._preload_historical_memory()
        
        # Execute catch-up logic
        self.catch_up_gaps()
        
        # Hydrate aligned stream on cold boots if it is empty
        aligned_check = db.get_aligned_history(self.db_uri, limit=1)
        if not aligned_check:
            logger.info("Database aligned stream is empty on bootstrap. Performing historical alignment...")
            historical_data = {
                key: list(self.memory[key])
                for key in ["FCPO", "ZL", "USDMYR"]
            }
            if historical_data["FCPO"]:
                self._align_and_persist_batch(historical_data)
                logger.info(f"Historical alignment of {len(historical_data['FCPO'])} bars completed.")

    def _preload_historical_memory(self) -> None:
        """Warms up local memory with at least 54 periods of historical K-lines from DB or API."""
        logger.info(f"Warming up sliding memory (target limit={self.warmup_limit} periods)...")
        
        assets = self.config.strategy.get("assets", {})
        for key, asset_cfg in assets.items():
            symbol = asset_cfg.get("tv_ticker")
            
            # 1. Attempt load from local database
            local_bars = db.get_historical_bars(self.db_uri, symbol, limit=self.warmup_limit)
            if len(local_bars) >= self.warmup_limit:
                logger.info(f"Successfully preloaded {len(local_bars)} periods for {symbol} from local SQLite.")
                self.memory[key].extend(local_bars)
            else:
                # 2. Insufficient DB data -> Fetch from external API
                logger.warning(f"Insufficient local data for {symbol} (Found {len(local_bars)}/{self.warmup_limit}). Fetching from API...")
                try:
                    # Request historical bars (we request a bit more to ensure we have enough)
                    api_bars = self.data_source.fetch_data(asset_cfg, limit=self.warmup_limit)
                    if api_bars:
                        # Persist to database
                        db.save_market_data(self.db_uri, api_bars)
                        # Set sync time to the latest bar
                        latest_time = api_bars[-1].datetime
                        db.save_sync_metadata(self.db_uri, symbol, latest_time)
                        
                        self.memory[key].extend(api_bars)
                        logger.info(f"Successfully fetched and persisted {len(api_bars)} historical periods for {symbol} from API.")
                    else:
                        raise ValueError(f"No historical bars returned from API for {symbol}")
                except Exception as e:
                    logger.error(f"Critical error during boot preloading for {symbol}: {e}")
                    # If both fail and we have SOME local bars, we fallback to using whatever we have
                    if local_bars:
                        self.memory[key].extend(local_bars)
                        logger.warning(f"Using partial local data ({len(local_bars)} bars) as last resort.")
                    else:
                        raise RuntimeError(f"Database and API both failed to hydrate {symbol}. Cannot bootstrap!")

    def catch_up_gaps(self) -> None:
        """Detects time gaps and pulls missing historical bars sequentially (断线追赶机制)."""
        logger.info("Checking for data sync gaps (断线追赶机制)...")
        assets = self.config.strategy.get("assets", {})
        
        # Primary reference asset is FCPO
        fcpo_cfg = assets.get("FCPO")
        fcpo_symbol = fcpo_cfg.get("tv_ticker")
        
        last_sync = db.get_last_sync_time(self.db_uri, fcpo_symbol)
        if not last_sync:
            logger.info("No last sync time found in metadata. Gap catch-up skipped (history already preloaded).")
            if self.memory["FCPO"]:
                db.save_sync_metadata(self.db_uri, fcpo_symbol, self.memory["FCPO"][-1].datetime)
            return
            
        last_sync_dt = parse_datetime(last_sync)
        current_time = get_kl_time()
        
        # Gap is calculated in terms of 15m intervals
        time_diff = current_time - last_sync_dt
        interval_seconds = self.config.system.get("poll_interval_seconds", 900)
        
        # Allow 5 seconds latency margin
        if time_diff.total_seconds() > (interval_seconds + 5):
            gaps_count = int(time_diff.total_seconds() // interval_seconds)
            logger.info(f"Data gap detected! Last sync was {last_sync} ({gaps_count} bars behind). Initiating catch-up fetch...")
            
            # Fetch batch historical bars covering the gap duration
            try:
                # Add buffer to ensure overlap and no gaps
                fetch_limit = min(gaps_count + 10, 200) 
                
                # Fetch all assets
                fetched_data: Dict[str, List[BarData]] = {}
                for key, cfg in assets.items():
                    symbol = cfg.get("tv_ticker")
                    bars = self.data_source.fetch_data(cfg, limit=fetch_limit)
                    # Filter bars that are strictly newer than last_sync
                    new_bars = [b for b in bars if parse_datetime(b.datetime) > last_sync_dt]
                    fetched_data[key] = new_bars
                    logger.info(f"Fetched {len(new_bars)} new catch-up K-lines for {symbol}.")
                
                # Align and save the catch-up data in batch
                self._align_and_persist_batch(fetched_data)
                
            except Exception as e:
                logger.error(f"Failed to execute gap catch-up: {e}. Real-time poller will attempt recovery.")
        else:
            logger.info("No sync gap detected. Database is up to date.")

    def run_ingest_cycle(self) -> None:
        """Real-time poll cycle executed every 15 minutes by scheduler."""
        logger.info("=== Starting Data Ingestion Cycle ===")
        if not self.config.is_market_open():
            logger.info("Market is closed. Sleeping cycle.")
            return
            
        assets = self.config.strategy.get("assets", {})
        cycle_data: Dict[str, BarData] = {}
        
        try:
            for key, cfg in assets.items():
                symbol = cfg.get("tv_ticker")
                # Pull latest K-line (request last 2 bars to ensure we have the fully closed one)
                bars = self.data_source.fetch_data(cfg, limit=2)
                if not bars:
                    raise ValueError(f"No bars returned for {symbol}")
                
                # Get latest closed bar (index -1)
                latest_bar = bars[-1]
                cycle_data[key] = latest_bar
                logger.info(f"Polled latest K-line for {symbol}: {latest_bar.datetime} Close={latest_bar.close}")
                
            # Execute processing
            self._process_single_aligned_cycle(cycle_data)
            
        except Exception as e:
            logger.error(f"Error during polling ingestion cycle: {e}")

    def _process_single_aligned_cycle(self, latest_bars: Dict[str, BarData]) -> None:
        """Process a single real-time bar for all symbols (Layer 1.1 - Layer 4)."""
        fcpo_bar = latest_bars.get("FCPO")
        zl_bar = latest_bars.get("ZL")
        usdmyr_bar = latest_bars.get("USDMYR")
        
        if not fcpo_bar or not zl_bar or not usdmyr_bar:
            logger.error("Missing bar data in processing cycle. Alignment skipped.")
            return
            
        # Strict timestamp alignment check for holidays and session desynchronization
        if fcpo_bar.datetime != zl_bar.datetime or fcpo_bar.datetime != usdmyr_bar.datetime:
            logger.warning(
                f"Market desynchronization detected. Holiday or session gap! "
                f"FCPO={fcpo_bar.datetime}, ZL={zl_bar.datetime}, USDMYR={usdmyr_bar.datetime}. "
                f"Skipping aligned cycle processing."
            )
            return

        # --- Layer 1.1: Rollover Detection ---
        self._check_rollover_alert(fcpo_bar)

        # Update memory deques
        self.memory["FCPO"].append(fcpo_bar)
        self.memory["ZL"].append(zl_bar)
        self.memory["USDMYR"].append(usdmyr_bar)
        
        # Write raw bars to market_data_15m
        db.save_market_data(self.db_uri, [fcpo_bar, zl_bar, usdmyr_bar])

        # --- Layer 2: Cleaning, Alignment & Normalization ---
        # Real-time data is already synchronized in timestamp from the poll.
        # Check if timestamps are exactly matching. If not, use standard forward fill (ffill).
        # We perform the unit normalization:
        zl_raw_close = zl_bar.close
        fx_rate = usdmyr_bar.close
        
        # Formula: ZL_USD_Per_Ton = (ZL_Raw / 100) * 2204.62
        zl_usd_per_ton = (zl_raw_close / 100.0) * 2204.62
        zl_close_myr = zl_usd_per_ton * fx_rate
        spread = fcpo_bar.close - zl_close_myr
        
        aligned_payload = AlignedPayload(
            datetime=fcpo_bar.datetime,
            fcpo_close=fcpo_bar.close,
            zl_close_usd=zl_raw_close,
            fx_rate=fx_rate,
            zl_close_myr=zl_close_myr,
            spread=spread
        )

        # --- Layer 3: Persistence ---
        db.save_aligned_data(self.db_uri, aligned_payload)
        
        # Update sync metadata
        assets = self.config.strategy.get("assets", {})
        db.save_sync_metadata(self.db_uri, assets["FCPO"]["tv_ticker"], fcpo_bar.datetime)
        logger.info(f"Aligned stream saved for {fcpo_bar.datetime}. Spread={spread:.2f} (FCPO={fcpo_bar.close:.1f}, CBOT美豆油_MYR={zl_close_myr:.1f})")

        # --- Layer 4: Dispatch/Trigger down-stream ---
        if self.dispatcher_callback:
            logger.info("Waking up down-stream algorithm brain (Dispatcher Layer)...")
            self.dispatcher_callback(aligned_payload)

    def _align_and_persist_batch(self, fetched_data: Dict[str, List[BarData]]) -> None:
        """Handles Pandas-based multi-asset left joins, forward-fills, and unit normalization in batch."""
        fcpo_bars = fetched_data.get("FCPO", [])
        zl_bars = fetched_data.get("ZL", [])
        usdmyr_bars = fetched_data.get("USDMYR", [])
        
        if not fcpo_bars:
            logger.info("No new FCPO data to align.")
            return
            
        # Standardize raw bars insertion to DB
        all_bars = fcpo_bars + zl_bars + usdmyr_bars
        db.save_market_data(self.db_uri, all_bars)

        # Convert to Pandas DataFrames for left join on FCPO
        df_fcpo = pd.DataFrame([b.__dict__ for b in fcpo_bars]).rename(columns={"close": "fcpo_close"})
        df_zl = pd.DataFrame([b.__dict__ for b in zl_bars]).rename(columns={"close": "zl_close_USD"})
        df_usdmyr = pd.DataFrame([b.__dict__ for b in usdmyr_bars]).rename(columns={"close": "fx_rate"})
        
        # Left-join alignment on datetime
        df_align = pd.merge(df_fcpo[["datetime", "fcpo_close"]], df_zl[["datetime", "zl_close_USD"]], on="datetime", how="left")
        df_align = pd.merge(df_align, df_usdmyr[["datetime", "fx_rate"]], on="datetime", how="left")
        
        # Forward fill missing proxy values (e.g. if CBOT/FX closed but FCPO is running)
        df_align["zl_close_USD"] = df_align["zl_close_USD"].ffill()
        df_align["fx_rate"] = df_align["fx_rate"].ffill()
        
        # If still has NaNs at the beginning, we fill using database or default values
        if df_align["zl_close_USD"].isna().any() or df_align["fx_rate"].isna().any():
            # Get latest available close from SQLite history
            assets = self.config.strategy.get("assets", {})
            latest_db_zl = db.get_historical_bars(self.db_uri, assets["ZL"]["tv_ticker"], limit=1)
            latest_db_rate = db.get_historical_bars(self.db_uri, assets["USDMYR"]["tv_ticker"], limit=1)
            
            fill_zl = latest_db_zl[0].close if latest_db_zl else 60.0 # Default fallback soybean cents
            fill_rate = latest_db_rate[0].close if latest_db_rate else 4.45 # Default USDMYR rate fallback
            
            df_align["zl_close_USD"] = df_align["zl_close_USD"].fillna(fill_zl)
            df_align["fx_rate"] = df_align["fx_rate"].fillna(fill_rate)

        # Apply Unit Normalization Formulas
        # ZL_USD_Per_Ton = (ZL_USD_Raw / 100) * 2204.62
        # ZL_MYR_Per_Ton = ZL_USD_Per_Ton * FX_Rate
        # Spread = FCPO - ZL_MYR_Per_Ton
        df_align["zl_usd_per_ton"] = (df_align["zl_close_USD"] / 100.0) * 2204.62
        df_align["zl_close_myr"] = df_align["zl_usd_per_ton"] * df_align["fx_rate"]
        df_align["spread"] = df_align["fcpo_close"] - df_align["zl_close_myr"]

        # Persist rows to database
        for _, row in df_align.iterrows():
            payload = AlignedPayload(
                datetime=row["datetime"],
                fcpo_close=row["fcpo_close"],
                zl_close_usd=row["zl_close_USD"],
                fx_rate=row["fx_rate"],
                zl_close_myr=row["zl_close_myr"],
                spread=row["spread"]
            )
            db.save_aligned_data(self.db_uri, payload)
            
        # Update memory deques with standard list items
        # Re-fetch latest to ensure sliding memory contains the newest aligned bars
        assets = self.config.strategy.get("assets", {})
        for key in ["FCPO", "ZL", "USDMYR"]:
            ticker = assets[key]["tv_ticker"]
            latest_bars = db.get_historical_bars(self.db_uri, ticker, limit=self.warmup_limit)
            self.memory[key].clear()
            self.memory[key].extend(latest_bars)

        # Update sync metadata to the last aligned row
        latest_aligned_time = df_align.iloc[-1]["datetime"]
        db.save_sync_metadata(self.db_uri, assets["FCPO"]["tv_ticker"], latest_aligned_time)
        
        logger.info(f"Batch gap alignment completed up to {latest_aligned_time}. Synced {len(df_align)} aligned records.")

    def _check_rollover_alert(self, latest_bar: BarData) -> None:
        """Layer 1.1: Checks if price diff matches contract rollover (>5x ATR) and logs warning/resets."""
        if not self.memory["FCPO"]:
            return
            
        prev_bar = self.memory["FCPO"][-1]
        price_diff = abs(latest_bar.close - prev_bar.close)
        
        # Calculate ATR-14 from current memory
        atr = calculate_atr_latest(list(self.memory["FCPO"]), period=14)
        
        # Rollover check
        if price_diff > (5.0 * atr):
            logger.warning(
                f"\n⚠️⚠️⚠️ [ROLLOVER DETECTED - 换月预警] ⚠️⚠️⚠️\n"
                f"FCPO price jump of {price_diff:.1f} exceeds 5x ATR ({5.0 * atr:.1f}).\n"
                f"Previous Bar ({prev_bar.datetime}): {prev_bar.close:.1f}\n"
                f"Current Bar ({latest_bar.datetime}): {latest_bar.close:.1f}\n"
                f"Clearing pipeline memory deques and triggering brain reset callbacks.\n"
            )
            # 1. Clear pipeline memory deques
            for key in self.memory:
                self.memory[key].clear()
                
            # 2. Trigger registered callbacks (e.g. to clear factor brain)
            for cb in self.rollover_callbacks:
                try:
                    import inspect
                    sig = inspect.signature(cb)
                    if len(sig.parameters) > 0:
                        cb(latest_bar)
                    else:
                        cb()
                except Exception as e:
                    logger.error(f"Error in rollover callback: {e}")
