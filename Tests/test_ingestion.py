import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
from datetime import datetime, timedelta

from src.models import BarData, AlignedPayload
from src.config import ConfigManager
from src.data_ingestion import IngestionPipeline, calculate_atr_latest, calculate_tr

class TestDataIngestion(unittest.TestCase):
    def setUp(self):
        # Create a mock ConfigManager
        self.mock_config = MagicMock(spec=ConfigManager)
        self.mock_config.system = {
            "database_path": "sqlite:///:memory:",  # Use in-memory SQLite for testing!
            "poll_interval_seconds": 900
        }
        self.mock_config.strategy = {
            "strategy_id": "TEST_STRATEGY",
            "lookback_period": 10,
            "assets": {
                "FCPO": {"tv_ticker": "MYX:FCPO1!", "yf_ticker": "FCPO.KL"},
                "ZL": {"tv_ticker": "CBOT:ZL1!", "yf_ticker": "ZL=F"},
                "USDMYR": {"tv_ticker": "FX_IDC:USDMYR", "yf_ticker": "MYR=X"}
            }
        }
        self.mock_config.account = {"initial_capital_rm": 100000.0}
        self.mock_config.is_market_open.return_value = True

    def test_tr_calculation(self):
        # Test basic True Range calculation
        # TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
        tr = calculate_tr(high=100.0, low=90.0, prev_close=95.0)
        self.assertEqual(tr, 10.0)

        tr2 = calculate_tr(high=110.0, low=105.0, prev_close=100.0)
        self.assertEqual(tr2, 10.0)  # high - prev_close = 10

    def test_atr_latest(self):
        # Create a list of bars with constant high/low/close diff of 10
        bars = []
        base_time = datetime(2026, 6, 1, 10, 0, 0)
        for i in range(20):
            bars.append(BarData(
                symbol="TEST",
                datetime=(base_time + timedelta(minutes=15*i)).strftime("%Y-%m-%d %H:%M:%S"),
                open=100.0,
                high=110.0,
                low=100.0,
                close=105.0,
                volume=100
            ))
        atr = calculate_atr_latest(bars, period=14)
        # TR for index 1..19 is max(10, abs(110-105)=5, abs(100-105)=5) = 10
        self.assertAlmostEqual(atr, 10.0)

    def test_unit_normalization(self):
        # Test the conversion of soybean oil to MYR/metric ton
        # Formula:
        # ZL_USD_Per_Ton = (ZL_Raw / 100) * 2204.62
        # ZL_MYR_Per_Ton = ZL_USD_Per_Ton * FX_Rate
        # Spread = FCPO - ZL_MYR_Per_Ton
        
        # Test values:
        # ZL Close: 60.00 cents/lb
        # USDMYR Rate: 4.5000
        # FCPO Close: 4200.0
        
        zl_raw_close = 60.00
        fx_rate = 4.5000
        fcpo_close = 4200.0
        
        zl_usd_per_ton = (zl_raw_close / 100.0) * 2204.62  # (60 / 100) * 2204.62 = 0.6 * 2204.62 = 1322.772
        zl_close_myr = zl_usd_per_ton * fx_rate            # 1322.772 * 4.5 = 5952.474
        expected_spread = fcpo_close - zl_close_myr        # 4200 - 5952.474 = -1752.474
        
        pipeline = IngestionPipeline(self.mock_config)
        
        # Prepare test lists
        fetched_data = {
            "FCPO": [BarData("MYX:FCPO1!", "2026-06-01 10:30:00", 4200, 4210, 4190, fcpo_close, 100)],
            "ZL": [BarData("CBOT:ZL1!", "2026-06-01 10:30:00", 60.0, 60.5, 59.8, zl_raw_close, 100)],
            "USDMYR": [BarData("FX_IDC:USDMYR", "2026-06-01 10:30:00", 4.5, 4.51, 4.49, fx_rate, 100)]
        }
        
        with patch('src.database.save_market_data'), \
             patch('src.database.save_aligned_data') as mock_save_aligned, \
             patch('src.database.save_sync_metadata'), \
             patch('src.database.get_historical_bars', return_value=[]):
            
            pipeline._align_and_persist_batch(fetched_data)
            
            # Retrieve the aligned payload sent to database
            mock_save_aligned.assert_called_once()
            aligned_arg = mock_save_aligned.call_args[0][1]
            
            self.assertEqual(aligned_arg.datetime, "2026-06-01 10:30:00")
            self.assertAlmostEqual(aligned_arg.fcpo_close, fcpo_close)
            self.assertAlmostEqual(aligned_arg.zl_close_usd, zl_raw_close)
            self.assertAlmostEqual(aligned_arg.fx_rate, fx_rate)
            self.assertAlmostEqual(aligned_arg.zl_close_myr, round(zl_close_myr, 4))
            self.assertAlmostEqual(aligned_arg.spread, round(expected_spread, 4))

    def test_alignment_with_forward_fill(self):
        # Test that missing values (due to CBOT or FX market close) are filled correctly
        pipeline = IngestionPipeline(self.mock_config)
        
        # We have 3 FCPO bars, but only 2 ZL and USDMYR bars (missing at index 1)
        fcpo_bars = [
            BarData("MYX:FCPO1!", "2026-06-01 10:30:00", 4200, 4200, 4200, 4200, 100),
            BarData("MYX:FCPO1!", "2026-06-01 10:45:00", 4210, 4210, 4210, 4210, 100),
            BarData("MYX:FCPO1!", "2026-06-01 11:00:00", 4220, 4220, 4220, 4220, 100)
        ]
        zl_bars = [
            BarData("CBOT:ZL1!", "2026-06-01 10:30:00", 60.0, 60.0, 60.0, 60.0, 100),
            # Missing 10:45:00!
            BarData("CBOT:ZL1!", "2026-06-01 11:00:00", 61.0, 61.0, 61.0, 61.0, 100)
        ]
        usdmyr_bars = [
            BarData("FX_IDC:USDMYR", "2026-06-01 10:30:00", 4.5, 4.5, 4.5, 4.5, 100),
            # Missing 10:45:00!
            BarData("FX_IDC:USDMYR", "2026-06-01 11:00:00", 4.52, 4.52, 4.52, 4.52, 100)
        ]
        
        fetched_data = {
            "FCPO": fcpo_bars,
            "ZL": zl_bars,
            "USDMYR": usdmyr_bars
        }
        
        saved_payloads = []
        def mock_save(db_path, payload):
            saved_payloads.append(payload)
            
        with patch('src.database.save_market_data'), \
             patch('src.database.save_aligned_data', side_effect=mock_save), \
             patch('src.database.save_sync_metadata'), \
             patch('src.database.get_historical_bars', return_value=[]):
            
            pipeline._align_and_persist_batch(fetched_data)
            
            # Assert 3 rows were aligned
            self.assertEqual(len(saved_payloads), 3)
            
            # Row 0: 10:30:00
            self.assertEqual(saved_payloads[0].datetime, "2026-06-01 10:30:00")
            self.assertEqual(saved_payloads[0].zl_close_usd, 60.0)
            self.assertEqual(saved_payloads[0].fx_rate, 4.5)
            
            # Row 1: 10:45:00 should be forward-filled using 10:30:00 data!
            self.assertEqual(saved_payloads[1].datetime, "2026-06-01 10:45:00")
            self.assertEqual(saved_payloads[1].zl_close_usd, 60.0)
            self.assertEqual(saved_payloads[1].fx_rate, 4.5)
            
            # Row 2: 11:00:00
            self.assertEqual(saved_payloads[2].datetime, "2026-06-01 11:00:00")
            self.assertEqual(saved_payloads[2].zl_close_usd, 61.0)
            self.assertEqual(saved_payloads[2].fx_rate, 4.52)

    def test_rollover_detection_trigger(self):
        # 1. Test ATR rollover trigger detection logic
        pipeline = IngestionPipeline(self.mock_config)
        
        # Prepare 15 bars with stable range of 10, so ATR is 10.0
        base_time = datetime(2026, 6, 1, 10, 0, 0)
        for i in range(15):
            pipeline.memory["FCPO"].append(BarData(
                symbol="MYX:FCPO1!",
                datetime=(base_time + timedelta(minutes=15*i)).strftime("%Y-%m-%d %H:%M:%S"),
                open=4200.0,
                high=4210.0,
                low=4200.0,
                close=4205.0,
                volume=100
            ))
            
        # Create a new bar with a huge jump of 100 points (> 5x ATR of 10.0)
        latest_bar = BarData(
            symbol="MYX:FCPO1!",
            datetime="2026-06-01 14:00:00",
            open=4200.0,
            high=4310.0,
            low=4300.0,
            close=4305.0,
            volume=100
        )
        
        with patch('src.data_ingestion.logger.warning') as mock_log_warn:
            pipeline._check_rollover_alert(latest_bar)
            # Verify rollover warning was logged
            mock_log_warn.assert_called_once()
            log_msg = mock_log_warn.call_args[0][0]
            self.assertIn("ROLLOVER DETECTED", log_msg)

    def test_fallback_data_source_flow(self):
        # 2. Test FallbackDataSource resilience when primary TV fails
        from src.data_sources import FallbackDataSource
        
        fallback_ds = FallbackDataSource()
        
        # Mock primary fetch to raise error
        fallback_ds.primary.fetch_data = MagicMock(side_effect=RuntimeError("TradingView connection timed out"))
        
        # Mock fallback fetch to succeed
        mock_bar = BarData("ZL=F", "2026-06-01 10:30:00", 60.0, 60.0, 60.0, 60.0, 100)
        fallback_ds.fallback.fetch_data = MagicMock(return_value=[mock_bar])
        
        symbol_cfg = {"tv_ticker": "CBOT:ZL1!", "yf_ticker": "ZL=F", "poll_interval_seconds": 900}
        
        with patch('src.data_sources.TV_AVAILABLE', True):
            # Fallback client should catch primary error and invoke fallback
            bars = fallback_ds.fetch_data(symbol_cfg, limit=1)
            
            # Assert primary was tried, fallback was executed, and data returned correctly
            fallback_ds.primary.fetch_data.assert_called_once()
            fallback_ds.fallback.fetch_data.assert_called_once()
            self.assertEqual(len(bars), 1)
            self.assertEqual(bars[0].symbol, "ZL=F")

    def test_cold_start_hydration_when_last_sync_time_empty(self):
        # 3. Test Cold-Start bootstrap when local database is empty
        pipeline = IngestionPipeline(self.mock_config)
        
        mock_bars = [
            BarData("MYX:FCPO1!", f"2026-06-01 10:{i:02d}:00", 4200, 4200, 4200, 4200, 100)
            for i in range(54)
        ]
        
        # Mock API fetch
        pipeline.data_source.fetch_data = MagicMock(return_value=mock_bars)
        
        with patch('src.database.get_historical_bars', return_value=[]), \
             patch('src.database.save_market_data') as mock_save_md, \
             patch('src.database.save_sync_metadata') as mock_save_sync, \
             patch('src.database.init_db'), \
             patch('src.database.load_portfolio_state'):
            
            pipeline._preload_historical_memory()
            
            # Verify pipeline fetched from API, persisted, and hydrated memory
            pipeline.data_source.fetch_data.assert_called()
            mock_save_md.assert_called()
            mock_save_sync.assert_called()
            self.assertEqual(len(pipeline.memory["FCPO"]), pipeline.warmup_limit)
            self.assertEqual(pipeline.memory["FCPO"][-1].datetime, mock_bars[-1].datetime)

if __name__ == '__main__':
    unittest.main()

