import sqlite3
import os
from typing import List, Optional, Dict, Any
from src.models import BarData, AlignedPayload
from src.utils import get_kl_time, format_datetime

def _parse_db_path(db_uri: str) -> str:
    """Parse sqlite:///bursa_poc.db to a standard file path."""
    if db_uri.startswith("sqlite:///"):
        path = db_uri[10:]
        return path
    return db_uri

def get_connection(db_uri: str) -> sqlite3.Connection:
    """Get sqlite connection with auto type-conversions and dict factory."""
    db_path = _parse_db_path(db_uri)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_uri: str) -> None:
    """Initialize database tables and indexes."""
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    
    # 1. market_data_15m Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS market_data_15m (
        symbol TEXT NOT NULL,
        datetime TEXT NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume INTEGER NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (symbol, datetime)
    );
    """)
    
    # Index on datetime for fast queries
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_md_datetime ON market_data_15m (datetime);
    """)
    
    # 2. aligned_factor_stream Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS aligned_factor_stream (
        datetime TEXT PRIMARY KEY,
        fcpo_close REAL NOT NULL,
        zl_close_USD REAL NOT NULL,
        fx_rate REAL NOT NULL,
        zl_close_myr REAL NOT NULL,
        spread REAL NOT NULL,
        factor_score REAL,
        signal INTEGER DEFAULT 0
    );
    """)
    
    # 3. data_sync_metadata Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS data_sync_metadata (
        symbol TEXT PRIMARY KEY,
        last_sync_time TEXT NOT NULL,
        is_active INTEGER DEFAULT 1
    );
    """)
    
    # 4. portfolio_state Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS portfolio_state (
        strategy_id TEXT PRIMARY KEY,
        current_capital REAL NOT NULL,
        position_direction INTEGER DEFAULT 0,
        position_lots INTEGER DEFAULT 0,
        average_entry_price REAL,
        last_updated TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    conn.commit()
    conn.close()

def save_market_data(db_uri: str, bars: List[BarData]) -> int:
    """Save a list of raw K-line bars. Uses INSERT OR IGNORE for idempotency."""
    if not bars:
        return 0
    
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    
    rows = [
        (b.symbol, b.datetime, b.open, b.high, b.low, b.close, b.volume)
        for b in bars
    ]
    
    cursor.executemany("""
    INSERT OR IGNORE INTO market_data_15m 
    (symbol, datetime, open, high, low, close, volume)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, rows)
    
    inserted = cursor.rowcount
    conn.commit()
    conn.close()
    return inserted

def get_last_sync_time(db_uri: str, symbol: str) -> Optional[str]:
    """Retrieve last synchronized timestamp for a symbol."""
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT last_sync_time FROM data_sync_metadata WHERE symbol = ?", 
        (symbol,)
    )
    row = cursor.fetchone()
    conn.close()
    return row["last_sync_time"] if row else None

def save_sync_metadata(db_uri: str, symbol: str, sync_time: str) -> None:
    """Upsert last synchronized timestamp for a symbol."""
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO data_sync_metadata (symbol, last_sync_time, is_active)
    VALUES (?, ?, 1)
    ON CONFLICT(symbol) DO UPDATE SET 
        last_sync_time = excluded.last_sync_time
    """, (symbol, sync_time))
    conn.commit()
    conn.close()

def save_aligned_data(db_uri: str, aligned: AlignedPayload) -> None:
    """Save or update aligned K-line and calculated factor information."""
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO aligned_factor_stream 
    (datetime, fcpo_close, zl_close_USD, fx_rate, zl_close_myr, spread, factor_score, signal)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(datetime) DO UPDATE SET
        fcpo_close = excluded.fcpo_close,
        zl_close_USD = excluded.zl_close_USD,
        fx_rate = excluded.fx_rate,
        zl_close_myr = excluded.zl_close_myr,
        spread = excluded.spread,
        factor_score = excluded.factor_score,
        signal = excluded.signal
    """, (
        aligned.datetime, aligned.fcpo_close, aligned.zl_close_usd, 
        aligned.fx_rate, aligned.zl_close_myr, aligned.spread,
        aligned.factor_score, aligned.signal
    ))
    conn.commit()
    conn.close()

def get_historical_bars(db_uri: str, symbol: str, limit: int = 54) -> List[BarData]:
    """Get the latest K-line bars for a symbol from SQLite (newest first)."""
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT symbol, datetime, open, high, low, close, volume 
    FROM market_data_15m 
    WHERE symbol = ? 
    ORDER BY datetime DESC 
    LIMIT ?
    """, (symbol, limit))
    rows = cursor.fetchall()
    conn.close()
    
    # Reverse to return in chronological order (oldest first)
    bars = [
        BarData(
            symbol=row["symbol"],
            datetime=row["datetime"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"]
        )
        for row in reversed(rows)
    ]
    return bars

def get_aligned_history(db_uri: str, limit: int = 40) -> List[AlignedPayload]:
    """Retrieve historical aligned records for warm up (oldest first)."""
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT datetime, fcpo_close, zl_close_USD, fx_rate, zl_close_myr, spread, factor_score, signal
    FROM aligned_factor_stream 
    ORDER BY datetime DESC 
    LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    
    aligned_list = [
        AlignedPayload(
            datetime=row["datetime"],
            fcpo_close=row["fcpo_close"],
            zl_close_usd=row["zl_close_USD"],
            fx_rate=row["fx_rate"],
            zl_close_myr=row["zl_close_myr"],
            spread=row["spread"],
            factor_score=row["factor_score"],
            signal=row["signal"]
        )
        for row in reversed(rows)
    ]
    return aligned_list

def load_portfolio_state(db_uri: str, strategy_id: str, default_capital: float) -> Dict[str, Any]:
    """Load portfolio state for 'strategy_id'. Initializes state if row does not exist."""
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT current_capital, position_direction, position_lots, average_entry_price, last_updated "
        "FROM portfolio_state WHERE strategy_id = ?",
        (strategy_id,)
    )
    row = cursor.fetchone()
    
    if row:
        state = {
            "current_capital": row["current_capital"],
            "position_direction": row["position_direction"],
            "position_lots": row["position_lots"],
            "average_entry_price": row["average_entry_price"],
            "last_updated": row["last_updated"]
        }
    else:
        # Initialize
        now_str = format_datetime(get_kl_time())
        cursor.execute("""
        INSERT INTO portfolio_state 
        (strategy_id, current_capital, position_direction, position_lots, average_entry_price, last_updated)
        VALUES (?, ?, 0, 0, NULL, ?)
        """, (strategy_id, default_capital, now_str))
        conn.commit()
        
        state = {
            "current_capital": default_capital,
            "position_direction": 0,
            "position_lots": 0,
            "average_entry_price": None,
            "last_updated": now_str
        }
        
    conn.close()
    return state

def save_portfolio_state(
    db_uri: str, 
    strategy_id: str, 
    capital: float, 
    direction: int, 
    lots: int, 
    entry_price: Optional[float]
) -> None:
    """Persist portfolio status into the database."""
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    now_str = format_datetime(get_kl_time())
    
    cursor.execute("""
    INSERT INTO portfolio_state 
    (strategy_id, current_capital, position_direction, position_lots, average_entry_price, last_updated)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(strategy_id) DO UPDATE SET
        current_capital = excluded.current_capital,
        position_direction = excluded.position_direction,
        position_lots = excluded.position_lots,
        average_entry_price = excluded.average_entry_price,
        last_updated = excluded.last_updated
    """, (strategy_id, capital, direction, lots, entry_price, now_str))
    
    conn.commit()
    conn.close()

def update_factor_signal(db_uri: str, dt_str: str, score: float, signal: int) -> None:
    """Update factor score and signal in aligned_factor_stream for a specific timestamp (Write-back)."""
    conn = get_connection(db_uri)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE aligned_factor_stream
    SET factor_score = ?, signal = ?
    WHERE datetime = ?
    """, (score, signal, dt_str))
    conn.commit()
    conn.close()

