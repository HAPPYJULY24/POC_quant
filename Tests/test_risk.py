import unittest
from unittest.mock import MagicMock, patch
import math
import sqlite3

from src.models import SignalPayload
from src.config import ConfigManager
from src.risk_sentinel import RiskSentinel
import src.database as db

class TestRiskSentinel(unittest.TestCase):
    def setUp(self):
        # Create a mock ConfigManager
        self.mock_config = MagicMock(spec=ConfigManager)
        self.mock_config.system = {
            "database_path": "sqlite:///:memory:",  # In-memory DB
            "poll_interval_seconds": 900
        }
        self.mock_config.strategy = {
            "strategy_id": "TEST_RISK_STRATEGY",
            "lookback_period": 10,
            "assets": {
                "FCPO": {
                    "multiplier": 25.0,
                    "tick_size": 1.0,
                    "margin_per_lot": 8000.0,
                    "tv_ticker": "MYX:FCPO1!",
                    "yf_ticker": "FCPO.KL"
                },
                "FKLI": {
                    "multiplier": 50.0,
                    "tick_size": 0.5,
                    "margin_per_lot": 4000.0,
                    "tv_ticker": "MYX:FKLI1!",
                    "yf_ticker": "FKLI.KL"
                }
            }
        }
        self.mock_config.account = {
            "initial_capital_rm": 100000.0,
            "risk_per_trade_pct": 0.01,
            "max_position_lots": 20
        }
        
        self.sentinel = RiskSentinel(self.mock_config)
        db.init_db("sqlite:///:memory:")

    def test_price_tick_cleaning(self):
        """Layer 4: Verify price roundings strictly match exchange tick boundaries (FCPO 1.0, FKLI 0.5)."""
        # FCPO round check
        self.assertEqual(self.sentinel.clean_price(4250.37, tick=1.0), 4200.0 if 0 else 4250.0)
        self.assertEqual(self.sentinel.clean_price(4250.87, tick=1.0), 4251.0)
        
        # FKLI round check
        self.assertEqual(self.sentinel.clean_price(1600.8, tick=0.5), 1601.0)
        self.assertEqual(self.sentinel.clean_price(1600.24, tick=0.5), 1600.0)
        self.assertEqual(self.sentinel.clean_price(1600.26, tick=0.5), 1600.5)

    def test_position_sizing_math(self):
        """Layer 2: Verify dynamic sizing ATR floor formula is mathematically exact."""
        # Risk Cap = 100,000 * 0.01 = RM 1,000
        # Risk per lot = (2.0 * 20.0 ATR) * 25.0 multiplier = 40.0 * 25.0 = RM 1,000
        # Target Lots = Floor(1000 / 1000) = 1 lot
        signal = SignalPayload(
            timestamp="2026-06-01 10:30:00",
            factor_name="ZScore_FCPO_ZL",
            raw_score=2.5,
            action_signal=-1,
            current_price=4500.0,
            volatility_metric=20.0  # ATR
        )
        
        # Mock database portfolio state pulling
        state = {
            "current_capital": 100000.0,
            "position_direction": 0,
            "position_lots": 0,
            "average_entry_price": None,
            "last_updated": "2026-06-01 10:00:00"
        }
        
        with patch('src.database.load_portfolio_state', return_value=state):
            instruction = self.sentinel.process_signal(signal)
            self.assertIsNotNone(instruction)
            self.assertEqual(instruction["suggested_lots"], 1)

        # Double capital -> Target Lots = Floor(2000 / 1000) = 2 lots
        state["current_capital"] = 200000.0
        with patch('src.database.load_portfolio_state', return_value=state):
            instruction = self.sentinel.process_signal(signal)
            self.assertEqual(instruction["suggested_lots"], 2)

    def test_compliance_ceilings_truncation(self):
        """Layer 3: Verify order lot calculations are truncated to max_position_lots ceiling boundaries."""
        # Risk Cap = 5,000,000 * 0.01 = RM 50,000
        # Risk per lot = (2 * 5 ATR) * 25.0 = RM 250
        # Sizing Lots = Floor(50000 / 250) = 200 lots!
        signal = SignalPayload(
            timestamp="2026-06-01 10:30:00",
            factor_name="ZScore_FCPO_ZL",
            raw_score=2.5,
            action_signal=-1,
            current_price=4500.0,
            volatility_metric=5.0  # ATR
        )
        
        state = {
            "current_capital": 5000000.0,
            "position_direction": 0,
            "position_lots": 0,
            "average_entry_price": None,
            "last_updated": "2026-06-01 10:00:00"
        }
        
        with patch('src.database.load_portfolio_state', return_value=state):
            instruction = self.sentinel.process_signal(signal)
            # Truncated to max_position_lots (20)
            self.assertEqual(instruction["suggested_lots"], 20)

    def test_inverse_close_priority(self):
        """Layer 1: Verify opposing signals trigger risk-flat exits first."""
        # Mock active position: LONG +2 lots
        # Signal is -1 (SHORT)
        signal = SignalPayload(
            timestamp="2026-06-01 10:30:00",
            factor_name="ZScore_FCPO_ZL",
            raw_score=-2.5,
            action_signal=-1,
            current_price=4500.0,
            volatility_metric=20.0
        )
        
        state = {
            "current_capital": 100000.0,
            "position_direction": 1,  # Long
            "position_lots": 2,
            "average_entry_price": 4400.0,
            "last_updated": "2026-06-01 10:00:00"
        }
        
        with patch('src.database.load_portfolio_state', return_value=state):
            instruction = self.sentinel.process_signal(signal)
            self.assertIsNotNone(instruction)
            # Target instruction action should be REVERSE_SWITCH flatting the 2 Long lots first!
            self.assertEqual(instruction["action_type"], "REVERSE_SWITCH")
            self.assertEqual(instruction["suggested_lots"], 1)
            self.assertEqual(instruction["suggested_lots_to_trade"], 3)

    def test_trade_abort_escape_path(self):
        """Layer 5: Verify that entering 0 lots aborts the transaction cleanly without updating states."""
        instruction = {
            "action_type": "OPEN",
            "target_direction": 1,
            "suggested_lots": 2,
            "suggested_price": 4550.0,
            "suggested_stop": 4510.0,
            "pos_direction": 0,
            "pos_lots": 0,
            "current_capital": 100000.0,
            "signal_timestamp": "2026-06-01 10:30:00"
        }
        
        # User enters 0 lots to abort
        with patch('builtins.input', return_value="0"), \
             patch('src.database.save_portfolio_state') as mock_save_state:
             
            self.sentinel.execute_feedback_loop(instruction)
            
            # Assert state write-back was NOT called, leaving DB untouched
            mock_save_state.assert_not_called()

    def test_cli_input_validation_and_slippage_logs(self):
        """Layer 5: Verify HFE input validation loops and accurate TCA slippage logs."""
        instruction = {
            "action_type": "CLOSE",
            "target_direction": 0,
            "suggested_lots": 2,
            "suggested_price": 4550.0,
            "suggested_stop": 0.0,
            "pos_direction": 1,  # Long
            "pos_lots": 2,
            "current_capital": 100000.0,
            "signal_timestamp": "2026-06-01 10:30:00"
        }
        
        # Mock inputs:
        # 1. actual filled lots: first invalid empty string, second invalid letters "abc", third valid "2"
        # 2. actual filled price: first invalid negative price "-4", second valid "4553" (3 points slippage!)
        mock_inputs = ["", "abc", "2", "-4", "4553"]
        
        # Mock DB load state for entry price to calculate realized PnL
        # Entry price: 4500. Realized PnL = (4553 - 4500) * 1 * 2 * 25 = 53 * 2 * 25 = RM 2,650. 
        # New Capital = 100,000 + 2,650 = RM 102,650
        with patch('builtins.input', side_effect=mock_inputs), \
             patch('src.database.save_portfolio_state') as mock_save_state, \
             patch('src.database.load_portfolio_state', return_value={"average_entry_price": 4500.0}):
             
            self.sentinel.execute_feedback_loop(instruction)
            
            # Assert DB write-back is executed with the recalculated realized capital and 0 position
            mock_save_state.assert_called_once()
            args = mock_save_state.call_args[0]
            
            # args: (db_uri, strategy_id, new_capital, direction, lots, entry_price)
            self.assertEqual(args[2], 102650.0) # RM 102,650 capital
            self.assertEqual(args[3], 0)        # Direction 0 (Flat)
            self.assertEqual(args[4], 0)        # Lots 0
            self.assertIsNone(args[5])          # Entry price is None

    def test_margin_deficiency_interception(self):
        """Layer 3: Verify available margin deficiency strictly truncates lots (RM 50,000 cap / RM 8,000 margin = max 6 lots)."""
        # We construct signal with ATR = 1.0 so that math sizing:
        # Risk Cap = 50,000 * 0.01 = RM 500
        # Risk per lot = (2.0 * 1.0 ATR) * 25.0 multiplier = RM 50
        # Math Sizing Lots = Floor(500 / 50) = 10 lots.
        signal = SignalPayload(
            timestamp="2026-06-01 10:30:00",
            factor_name="ZScore_FCPO_ZL",
            raw_score=2.5,
            action_signal=-1,
            current_price=4500.0,
            volatility_metric=1.0  # ATR = 1.0
        )
        
        state = {
            "current_capital": 50000.0,
            "position_direction": 0,
            "position_lots": 0,
            "average_entry_price": None,
            "last_updated": "2026-06-01 10:00:00"
        }
        
        with patch('src.database.load_portfolio_state', return_value=state):
            instruction = self.sentinel.process_signal(signal)
            self.assertIsNotNone(instruction)
            # Truncated from 10 lots to 6 lots because of margin limit
            self.assertEqual(instruction["suggested_lots"], 6)

    def test_vwap_calculation(self):
        """Layer 2: Verify that multiple ADD executions accumulate lots and compute volume-weighted average price (VWAP) correctly."""
        instruction = {
            "action_type": "ADD",
            "target_direction": 1,
            "suggested_lots": 1,
            "suggested_lots_to_trade": 1,
            "suggested_price": 4500.0,
            "suggested_stop": 4460.0,
            "pos_direction": 1,
            "pos_lots": 2,
            "current_capital": 100000.0,
            "signal_timestamp": "2026-06-01 10:30:00",
            "tick_size": 1.0,
            "multiplier": 25.0
        }
        
        # User adds 1 lot at price RM 4600.0
        # Prev lots = 2, Prev Avg Price = 4400.0
        # New lots = 3, New Avg Price = (2 * 4400 + 1 * 4600) / 3 = 13400 / 3 = 4466.6667
        mock_inputs = ["1", "4600"]
        with patch('builtins.input', side_effect=mock_inputs), \
             patch('src.database.save_portfolio_state') as mock_save_state, \
             patch('src.database.load_portfolio_state', return_value={"average_entry_price": 4400.0}):
             
            self.sentinel.execute_feedback_loop(instruction)
            
            mock_save_state.assert_called_once()
            args = mock_save_state.call_args[0]
            # args: (db_uri, strategy_id, new_capital, direction, lots, entry_price)
            self.assertEqual(args[3], 1)  # Direction Long
            self.assertEqual(args[4], 3)  # Lots 3 (2 + 1)
            self.assertAlmostEqual(args[5], 4466.6667, places=3)  # VWAP Avg Price

    def test_reverse_switch_lots_long_to_short(self):
        """Layer 5: Verify Reverse Switch flatting Long +2 lots and establishing Short 1 lot with correct realized PnL."""
        signal = SignalPayload(
            timestamp="2026-06-01 10:30:00",
            factor_name="ZScore_FCPO_ZL",
            raw_score=-2.5,
            action_signal=-1,  # Short signal
            current_price=4500.0,
            volatility_metric=20.0,
            symbol="FCPO"
        )
        
        state = {
            "current_capital": 100000.0,
            "position_direction": 1,  # Long
            "position_lots": 2,
            "average_entry_price": 4400.0,
            "last_updated": "2026-06-01 10:00:00"
        }
        
        # First verify process_signal outputs correct REVERSE_SWITCH payload
        with patch('src.database.load_portfolio_state', return_value=state):
            instruction = self.sentinel.process_signal(signal)
            self.assertEqual(instruction["action_type"], "REVERSE_SWITCH")
            self.assertEqual(instruction["suggested_lots"], 1)  # New Short lots
            self.assertEqual(instruction["suggested_lots_to_trade"], 3)  # Trade 2 Long + 1 Short
            
        # Execute feedback loop flatting Long 2 lots at 4500.0, establishing Short 1 lot
        # PnL = (4500 - 4400) * 1 (prev_direction) * 2 * 25 = RM +5,000. New Capital = RM 105,000.
        mock_inputs = ["3", "4500"]
        with patch('builtins.input', side_effect=mock_inputs), \
             patch('src.database.save_portfolio_state') as mock_save_state, \
             patch('src.database.load_portfolio_state', return_value={"average_entry_price": 4400.0}):
             
            self.sentinel.execute_feedback_loop(instruction)
            
            mock_save_state.assert_called_once()
            args = mock_save_state.call_args[0]
            self.assertEqual(args[2], 105000.0)  # RM 105,000 capital
            self.assertEqual(args[3], -1)        # Direction Short
            self.assertEqual(args[4], 1)         # New Short size is 1 lot (3 - 2)
            self.assertEqual(args[5], 4500.0)    # Entry price is 4500.0

    def test_reverse_switch_lots_short_to_long(self):
        """Layer 5: Verify Reverse Switch flatting Short -2 lots at 4600.0, establishing Long 1 lot at 4500.0 with correct directional realized PnL."""
        instruction = {
            "action_type": "REVERSE_SWITCH",
            "target_direction": 1,  # Long
            "suggested_lots": 1,
            "suggested_lots_to_trade": 3,
            "suggested_price": 4500.0,
            "suggested_stop": 4460.0,
            "pos_direction": -1,  # Short
            "pos_lots": 2,
            "current_capital": 100000.0,
            "signal_timestamp": "2026-06-01 10:30:00",
            "tick_size": 1.0,
            "multiplier": 25.0
        }
        
        # User executes 3 lots trade at 4500.0
        # PnL = (4500 - 4600) * -1 (prev_direction) * 2 * 25 = -100 * -1 * 2 * 25 = RM +5,000.
        # New Capital = 100,000 + 5000 = RM 105,000.
        mock_inputs = ["3", "4500"]
        with patch('builtins.input', side_effect=mock_inputs), \
             patch('src.database.save_portfolio_state') as mock_save_state, \
             patch('src.database.load_portfolio_state', return_value={"average_entry_price": 4600.0}):
             
            self.sentinel.execute_feedback_loop(instruction)
            
            mock_save_state.assert_called_once()
            args = mock_save_state.call_args[0]
            self.assertEqual(args[2], 105000.0)  # RM 105,000 capital
            self.assertEqual(args[3], 1)         # Direction Long
            self.assertEqual(args[4], 1)         # New Long size is 1 lot (3 - 2)
            self.assertEqual(args[5], 4500.0)    # Entry price is 4500.0

    def test_dynamic_asset_loading(self):
        """Layer 1: Verify dynamic assets loading config parameters dynamically based on signal symbol (FKLI vs. FCPO)."""
        # Testing FKLI (multiplier=50.0, tick_size=0.5, margin_per_lot=4000.0)
        signal = SignalPayload(
            timestamp="2026-06-01 10:30:00",
            factor_name="ZScore_FKLI",
            raw_score=-2.5,
            action_signal=1,  # Long signal
            current_price=1600.2,
            volatility_metric=10.0,  # ATR
            symbol="FKLI"
        )
        
        state = {
            "current_capital": 100000.0,
            "position_direction": 0,
            "position_lots": 0,
            "average_entry_price": None,
            "last_updated": "2026-06-01 10:00:00"
        }
        
        with patch('src.database.load_portfolio_state', return_value=state):
            instruction = self.sentinel.process_signal(signal)
            self.assertIsNotNone(instruction)
            
            # FKLI Tick rounding check: 1600.2 rounded to 0.5 tick = 1600.0
            self.assertEqual(instruction["suggested_price"], 1600.0)
            
            # FKLI multiplier: 50.0. ATR sizing math:
            # Risk Cap = 100,000 * 0.01 = RM 1,000
            # Risk per lot = (2 * 10 ATR) * 50.0 = 20 * 50 = RM 1,000
            # Target lots = Floor(1000 / 1000) = 1 lot
            self.assertEqual(instruction["suggested_lots"], 1)
            self.assertEqual(instruction["multiplier"], 50.0)
            self.assertEqual(instruction["tick_size"], 0.5)

    def test_database_write_error_handling(self):
        """Layer 5: Verify SQLite write OperationalError is captured gracefully and does not crash the CLI loop."""
        instruction = {
            "action_type": "OPEN",
            "target_direction": 1,
            "suggested_lots": 2,
            "suggested_lots_to_trade": 2,
            "suggested_price": 4500.0,
            "suggested_stop": 4460.0,
            "pos_direction": 0,
            "pos_lots": 0,
            "current_capital": 100000.0,
            "signal_timestamp": "2026-06-01 10:30:00",
            "tick_size": 1.0,
            "multiplier": 25.0
        }
        
        # User enters 2 lots at 4500.0
        mock_inputs = ["2", "4500"]
        
        # Make the database save_portfolio_state raise sqlite3.OperationalError ("database is locked")
        with patch('builtins.input', side_effect=mock_inputs), \
             patch('src.database.save_portfolio_state', side_effect=sqlite3.OperationalError("database is locked")), \
             patch('src.database.load_portfolio_state', return_value={"average_entry_price": None}):
             
             # Calling feedback loop should execute cleanly and NOT raise exceptions
             try:
                 self.sentinel.execute_feedback_loop(instruction)
                 success = True
             except Exception:
                 success = False
                 
             self.assertTrue(success)  # Ensure no exception propagates to crash the main thread

if __name__ == '__main__':
    unittest.main()
