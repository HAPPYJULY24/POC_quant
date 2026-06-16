import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime

from src.models import AlignedPayload, BarData, SignalPayload
from src.factors import ZScoreArbitrageFactor, ZeroVarianceException, BaseFactor

class TestAlgorithmicBrain(unittest.TestCase):
    def setUp(self):
        # Configure thresholds: non-symmetrical entry points
        self.lookback = 10
        self.upper_entry = 2.0
        self.lower_entry = -2.5
        self.exit_val = 0.5
        
        self.factor = ZScoreArbitrageFactor(
            name="Test_ZScore_Factor",
            lookback_period=self.lookback,
            upper_entry_threshold=self.upper_entry,
            lower_entry_threshold=self.lower_entry,
            exit_threshold=self.exit_val
        )

    def test_asymmetric_thresholds_mapping(self):
        """Verify that asymmetric thresholds discretize raw Z-scores perfectly without hardcoded symmetries."""
        # 1. Z > upper_entry (2.0) -> Short spread (-1)
        self.assertEqual(self.factor.generate_signal(2.1), -1)
        
        # 2. Z < lower_entry (-2.5) -> Long spread (1)
        self.assertEqual(self.factor.generate_signal(-2.6), 1)
        
        # 3. Z inside exit boundaries [-0.5, 0.5] -> Exit position (0)
        self.assertEqual(self.factor.generate_signal(0.3), 0)
        self.assertEqual(self.factor.generate_signal(-0.3), 0)
        self.assertEqual(self.factor.generate_signal(0.5), 0)
        self.assertEqual(self.factor.generate_signal(-0.5), 0)
        
        # 4. Z in no-man's land [0.5, 2.0] or [-2.5, -0.5] -> Maintain state (99)
        self.assertEqual(self.factor.generate_signal(1.2), 99)
        self.assertEqual(self.factor.generate_signal(-1.5), 99)

    def test_zero_variance_circuit_breaker(self):
        """Verify that identical spread data triggers ZeroVarianceException and fails safe to 0.0."""
        # Warm up factor deque with exactly identical values (std dev = 0.0)
        for _ in range(self.lookback):
            self.factor.memory.append(100.0)
            
        # Assert compute() throws the custom ZeroVarianceException
        with self.assertRaises(ZeroVarianceException):
            self.factor.compute()
            
        # Process an aligned payload and verify the circuit breaker handles it safely
        aligned_bar = AlignedPayload(
            datetime="2026-06-01 10:30:00",
            fcpo_close=4200.0,
            zl_close_usd=60.0,
            fx_rate=4.5,
            zl_close_myr=5952.4,
            spread=100.0
        )
        
        # When processed, standard dev = 0 -> throws exception -> returns raw_score = 0.0 -> signal = 0 (exit/neutral)
        with patch('src.factors.logger.warning') as mock_log_warn:
            payload = self.factor.process(aligned_bar, atr=15.0)
            
            # Assert safety net executed
            self.assertIsNotNone(payload)
            self.assertEqual(payload.raw_score, 0.0)
            self.assertEqual(payload.action_signal, 0)
            mock_log_warn.assert_called_once()
            self.assertIn("Circuit Breaker Triggered", mock_log_warn.call_args[0][0])

    def test_is_ready_boundary_guard(self):
        """Verify that the sliding window defends calculations until lookback period is fully hydrated."""
        factor = ZScoreArbitrageFactor(
            name="Boundary_Guard_Test",
            lookback_period=5,
            upper_entry_threshold=2.0,
            lower_entry_threshold=-2.5,
            exit_threshold=0.5
        )
        
        aligned_bar = AlignedPayload(
            datetime="2026-06-01 10:30:00",
            fcpo_close=4200.0,
            zl_close_usd=60.0,
            fx_rate=4.5,
            zl_close_myr=5952.4,
            spread=10.0
        )
        
        # Feed 4 bars (lookback is 5)
        for _ in range(4):
            payload = factor.process(aligned_bar, atr=10.0)
            self.assertFalse(factor.is_ready())
            self.assertIsNone(payload)
            
        # Feed the 5th bar -> becomes ready, returns SignalPayload
        payload = factor.process(aligned_bar, atr=10.0)
        self.assertTrue(factor.is_ready())
        self.assertIsNotNone(payload)
        self.assertIsInstance(payload, SignalPayload)
        
    def test_zscore_math_accuracy(self):
        """Verify rolling statistical calculations are mathematically precise against standard formulas."""
        # Seed memory with standard values
        # spread = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        # mean = 5.5
        # std_dev = sqrt( ((1-5.5)^2 + ... + (10-5.5)^2) / 10 ) = sqrt(82.5 / 10) = sqrt(8.25) = 2.87228132327
        # Last element = 10
        # Z = (10 - 5.5) / 2.87228132327 = 4.5 / 2.87228132327 = 1.5666989
        for val in range(1, 11):
            self.factor.memory.append(float(val))
            
        self.assertTrue(self.factor.is_ready())
        raw_score = self.factor.compute()
        
        expected_mean = 5.5
        expected_std = (sum((x - expected_mean) ** 2 for x in range(1, 11)) / 10) ** 0.5
        expected_z = (10.0 - expected_mean) / expected_std
        
        self.assertAlmostEqual(raw_score, expected_z, places=5)

    def test_nan_poisoning_defense(self):
        """Verify that AlignedPayload containing NaN or None spread is intercepted and deque is not poisoned."""
        # 1. Test None spread
        none_payload = AlignedPayload(
            datetime="2026-06-01 10:30:00",
            fcpo_close=4200.0,
            zl_close_usd=60.0,
            fx_rate=4.5,
            zl_close_myr=5952.4,
            spread=None  # Dirty!
        )
        
        self.factor.memory.clear()
        with patch('src.factors.logger.warning') as mock_log_warn:
            payload = self.factor.process(none_payload, atr=10.0)
            self.assertIsNone(payload)
            self.assertEqual(len(self.factor.memory), 0)  # Memory NOT poisoned!
            mock_log_warn.assert_called_once()
            
        # 2. Test NaN spread
        import math
        nan_payload = AlignedPayload(
            datetime="2026-06-01 10:45:00",
            fcpo_close=4200.0,
            zl_close_usd=60.0,
            fx_rate=4.5,
            zl_close_myr=5952.4,
            spread=float('nan')  # Dirty!
        )
        
        with patch('src.factors.logger.warning') as mock_log_warn:
            payload = self.factor.process(nan_payload, atr=10.0)
            self.assertIsNone(payload)
            self.assertEqual(len(self.factor.memory), 0)  # Memory NOT poisoned!
            mock_log_warn.assert_called_once()

    def test_async_db_write_exception_handling(self):
        """Verify ThreadPoolExecutor task callbacks capture sqlite3.OperationalError locked exceptions without swallowing."""
        import sqlite3
        from main import db_task_done_callback
        
        # Create a mock future that raises sqlite3.OperationalError when result() is checked
        mock_future = MagicMock()
        mock_future.result.side_effect = sqlite3.OperationalError("database is locked")
        
        with patch('main.logger.error') as mock_log_error:
            db_task_done_callback(mock_future)
            
            # Assert task failed exception was logged
            mock_log_error.assert_called_once()
            log_msg = mock_log_error.call_args[0][0]
            self.assertIn("Asynchronous DB write-back task failed", log_msg)
            self.assertIn("database is locked", log_msg)

    def test_direct_signal_mode_mapping(self):
        """Verify that DynamicExpressionFactor in direct_signal mode maps positive scores to Long (1) and negative scores to Short (-1)."""
        from src.factors import DynamicExpressionFactor
        factor = DynamicExpressionFactor(
            name="Test_Direct_Signal_Factor",
            lookback_period=5,
            expression="0.0",
            upper_entry_threshold=0.5,
            lower_entry_threshold=-0.5,
            exit_threshold=0.1,
            mode="direct_signal"
        )
        # raw_score > upper (0.5) -> Long (1)
        self.assertEqual(factor.generate_signal(1.0), 1)
        # raw_score < lower (-0.5) -> Short (-1)
        self.assertEqual(factor.generate_signal(-1.0), -1)
        # exit window [-0.1, 0.1] -> Exit (0)
        self.assertEqual(factor.generate_signal(0.0), 0)
        # no man's land -> Hold (99)
        self.assertEqual(factor.generate_signal(0.3), 99)

if __name__ == '__main__':
    unittest.main()
