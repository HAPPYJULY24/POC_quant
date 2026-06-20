import os
import sys
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from src.config import ConfigManager
from src.utils import setup_logger, get_kl_time, format_datetime
from src.data_ingestion import IngestionPipeline, calculate_atr_latest
from src.models import AlignedPayload, SignalPayload
from src.factors import DynamicExpressionFactor, CustomTrendZScoreFactor
from src.risk_sentinel import RiskSentinel
import src.database as db

# Setup Logger
logger = setup_logger("orchestrator")

# Initialize single-thread background ThreadPoolExecutor for safe SQLite write-backs
db_executor = ThreadPoolExecutor(max_workers=1)

# Thread-safe Communication Queue linking producer (BackgroundScheduler) to consumer (Main CLI Loop)
signal_queue = queue.Queue()

# Dynamic global references
pipeline = None
factor_brain = None

def db_task_done_callback(future) -> None:
    """Callback function attached to ThreadPoolExecutor futures to catch and log exceptions."""
    try:
        future.result()
    except Exception as e:
        logger.error(f"Asynchronous DB write-back task failed with exception: {e}")

def process_aligned_payload(payload: AlignedPayload) -> None:
    """Producer Callback: Triggered in background scheduler thread when aligned K-line is saved."""
    global pipeline, factor_brain
    if pipeline is None or factor_brain is None:
        logger.error("System components not fully initialized in dispatcher callback.")
        return
        
    logger.info("Waking up Algorithmic Brain Module (Layer 2)...")
    
    # 1. Calculate standard ATR volatility metric from ingestion pipeline deques
    atr_val = calculate_atr_latest(list(pipeline.memory["FCPO"]), period=14)
    
    # 2. Process K-line through Polymorphic Factor Brain
    signal_payload = factor_brain.process(payload, atr_val)
    
    if signal_payload is not None:
        logger.info(
            f"Z-Score score computed successfully: {signal_payload.raw_score:.3f} | "
            f"Signal = {signal_payload.action_signal}"
        )
        
        # 3. Asynchronously update DB table aligned_factor_stream (Write-back)
        future = db_executor.submit(
            db.update_factor_signal,
            pipeline.db_uri,
            signal_payload.timestamp,
            signal_payload.raw_score,
            signal_payload.action_signal
        )
        future.add_done_callback(db_task_done_callback)
        logger.debug(f"Asynchronously queued SQLite write-back task for {signal_payload.timestamp}")
        
        # 4. Producer Action: Enqueue the computed signal payload to the thread-safe Queue
        logger.info(f"Enqueueing signal for {signal_payload.timestamp} to foreground consumer queue.")
        signal_queue.put(signal_payload)
    else:
        logger.info("Factor brain not ready yet (sliding memory window warming up).")

def main():
    global pipeline, factor_brain
    
    logger.info("==================================================")
    logger.info("   Starting Bursa derivatives POC Quant System    ")
    logger.info("==================================================")
    
    # 1. Load Configurations
    try:
        config = ConfigManager()
        logger.info("Configuration settings loaded successfully.")
    except Exception as e:
        logger.error(f"Failed to load configurations: {e}")
        sys.exit(1)
        
    # 2. Instantiate Ingestion Pipeline
    pipeline = IngestionPipeline(config)
    
    # 3. Instantiate Factor Brain with asymmetric thresholds from config
    strategy_cfg = config.strategy
    lookback = strategy_cfg.get("lookback_period", 40)
    strategy_id = strategy_cfg.get("strategy_id", "POC_ZSCORE_FCPO")
    
    if strategy_id == "CUSTOM_TREND_ZSCORE":
        factor_brain = CustomTrendZScoreFactor(
            name="CustomTrendZScoreFactor_FCPO",
            lookback_period=lookback,
            symbol="FCPO"
        )
    else:
        # Load thresholds and expression (no magic numbers!)
        upper = strategy_cfg.get("upper_entry_threshold", 2.0)
        lower = strategy_cfg.get("lower_entry_threshold", -2.5)
        exit_val = strategy_cfg.get("exit_threshold", 0.5)
        expression = strategy_cfg.get("factor_expression", "(spreads[-1] - mean(spreads)) / std(spreads)")
        mode = strategy_cfg.get("mode", "mean_reversion")
        
        factor_brain = DynamicExpressionFactor(
            name="Dynamic_ZScore_FCPO_CBOTSoybeanOil",
            lookback_period=lookback,
            expression=expression,
            upper_entry_threshold=upper,
            lower_entry_threshold=lower,
            exit_threshold=exit_val,
            symbol="FCPO",
            mode=mode
        )
    
    # 4. Instantiate Risk Sentinel wind-control layer
    risk_sentinel = RiskSentinel(config)
    
    # 5. Register aligned K-line handler (Producer callback)
    pipeline.register_dispatcher(process_aligned_payload)
    
    # Register rollover brain reset callback
    def on_rollover_detected(latest_bar) -> None:
        logger.warning(f"🚨 [ROLLOVER RESET] Clearing factor brain memory deque due to contract rollover at {latest_bar.datetime}.")
        if hasattr(factor_brain, "memory"):
            factor_brain.memory.clear()
        if hasattr(factor_brain, "spread_memory"):
            factor_brain.spread_memory.clear()
        if hasattr(factor_brain, "close_memory"):
            factor_brain.close_memory.clear()
            
        # Put emergency signal to force liquidation
        emergency_signal = SignalPayload(
            timestamp=latest_bar.datetime,
            factor_name="EMERGENCY_ROLLOVER_DETECTOR",
            raw_score=0.0,
            action_signal=0,  # Force close
            current_price=latest_bar.close,
            volatility_metric=1.0,
            symbol="FCPO",
            is_emergency_rollover=True
        )
        signal_queue.put(emergency_signal)
        logger.warning("🚨 [ROLLOVER RESET] Dispatched emergency rollover signal to the queue.")
        
    pipeline.rollover_callbacks.append(on_rollover_detected)
    
    # 6. Bootstrap Ingestion Pipeline (DDLs, hydration, catch-up gaps)
    try:
        logger.info("Bootstrapping ingestion pipeline...")
        pipeline.bootstrap()
        logger.info("Bootstrap complete!")
    except Exception as e:
        logger.critical(f"Pipeline bootstrapping failed: {e}")
        sys.exit(1)
        
    # 7. Pre-warm Factor Brain Memory sliding window using DB history
    logger.info("Pre-warming factor brain memory deques from SQLite aligned history...")
    try:
        warmup_limit = max(lookback, 65)
        aligned_history = db.get_aligned_history(pipeline.db_uri, limit=warmup_limit)
        for hist in aligned_history:
            factor_brain.update_data(hist)
            
        mem_len = len(factor_brain.memory) if hasattr(factor_brain, "memory") else len(factor_brain.spread_memory)
        logger.info(f"Factor brain memory warmed up with {mem_len} historical records.")
        
        # Synchronize current_signal state with the actual portfolio state direction
        initial_cap = config.account.get("initial_capital_rm", 100000.0)
        state = db.load_portfolio_state(pipeline.db_uri, strategy_id, initial_cap)
        if hasattr(factor_brain, "current_signal"):
            factor_brain.current_signal = state.get("position_direction", 0)
            logger.info(f"Synchronized factor brain state: current_signal={factor_brain.current_signal}")
    except Exception as e:
        logger.error(f"Failed to pre-warm factor brain memory: {e}. Warm up will run incrementally in real-time.")
        
    # 8. Configure Cron-Aligned BackgroundScheduler (+5 seconds offset)
    poll_interval = config.system.get("poll_interval_seconds", 900)
    interval_minutes = poll_interval // 60
    
    scheduler = BackgroundScheduler()
    kl_tz = pytz.timezone(config.sessions.get("timezone", "Asia/Kuala_Lumpur"))
    
    cron_minute = f"*/{interval_minutes}" if interval_minutes < 60 else "0"
    cron_hour = "*" if interval_minutes < 60 else f"*/{interval_minutes // 60}"
    
    trigger = CronTrigger(
        hour=cron_hour,
        minute=cron_minute,
        second="5",
        timezone=kl_tz
    )
    
    scheduler.add_job(
        pipeline.run_ingest_cycle,
        trigger=trigger,
        name="15m_Ingestion_Cycle",
        misfire_grace_time=30
    )
    
    logger.info(f"Cron BackgroundScheduler aligned to {cron_minute}m:05s (Kuala Lumpur Time).")
    logger.info("Starting scheduler thread...")
    scheduler.start()
    
    logger.info("Starting foreground CLI signal consumer loop. Press Ctrl+C to exit.")
    
    # 9. Foreground Consumer Loop
    try:
        while True:
            try:
                # 1. Block foreground main thread for up to 1 second to fetch signals
                # Using a short timeout lets us intercept KeyboardInterrupt smoothly on Windows!
                signal_payload = signal_queue.get(timeout=1.0)
            except queue.Empty:
                continue
                
            # 2. Dequeued signal -> Run through Risk Sentinel Wind Control
            instruction = risk_sentinel.process_signal(signal_payload)
            
            # 3. If compliance green-lights manual instruction, run the interactive feedback loop
            if instruction is not None:
                # This suspends ONLY the main thread while BackgroundScheduler ticks silently!
                risk_sentinel.execute_feedback_loop(instruction)
                
    except (KeyboardInterrupt, SystemExit):
        logger.info("System shutdown requested. Stopping scheduler thread.")
        scheduler.shutdown()
        # Shutdown DB Thread Pool
        db_executor.shutdown(wait=True)
        logger.info("Orchestrator closed successfully.")

if __name__ == "__main__":
    main()
