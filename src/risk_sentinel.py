import math
import sqlite3
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
import pytz

from src.models import SignalPayload
from src.config import ConfigManager
from src.utils import setup_logger, get_kl_time, format_datetime
import src.database as db

logger = setup_logger("risk_sentinel")

class RiskSentinel:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.db_uri = self.config.system.get("database_path", "sqlite:///bursa_poc.db")
        self.strategy_id = self.config.strategy.get("strategy_id", "POC_ZSCORE_FCPO")
        self.initial_capital = self.config.account.get("initial_capital_rm", 100000.0)
        self.risk_pct = self.config.account.get("risk_per_trade_pct", 0.01)
        self.max_position_lots = self.config.account.get("max_position_lots", 20)
        
        # Load asset config metadata
        self.assets = self.config.strategy.get("assets", {})
        self.fcpo_cfg = self.assets.get("FCPO", {})
        self.multiplier = self.fcpo_cfg.get("multiplier", 25.0)
        self.tick_size = self.fcpo_cfg.get("tick_size", 1.0)
        self.margin_per_lot = 8000.0  # Safe default margin requirement per lot (RM)
        self.rollover_settings = self.config.strategy.get("rollover_settings", {})

    def clean_price(self, raw_price: float, tick: float) -> float:
        """Round the price strictly to the exchange's minimum tick-size boundary."""
        if raw_price is None or math.isnan(raw_price):
            return 0.0
        return round(raw_price / tick) * tick

    def is_rollover_period(self, symbol: str, timestamp_str: str) -> Tuple[bool, Optional[str], Optional[dict]]:
        """Check if the current signal time falls within the configured rollover risk window (timezone-aware)."""
        roll_cfg = self.rollover_settings.get(symbol)
        if not roll_cfg:
            return False, None, None
            
        try:
            # 1. Parse timezone from configuration
            tz_name = self.config.sessions.get("timezone", "Asia/Kuala_Lumpur")
            local_tz = pytz.timezone(tz_name)
            
            # 2. Parse timestamp string to naive datetime and localize it to Asia/Kuala_Lumpur
            naive_dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            localized_dt = local_tz.localize(naive_dt)
            signal_date = localized_dt.date()
        except Exception as e:
            logger.error(f"Failed to parse signal timestamp {timestamp_str}: {e}")
            return False, None, None
            
        roll_dates = roll_cfg.get("rollover_dates", [])
        days_before = roll_cfg.get("buffer_days_before", 2)
        days_after = roll_cfg.get("buffer_days_after", 1)
        
        for r_date_str in roll_dates:
            try:
                # 3. Parse rollover date (assumed to be in local timezone)
                r_date = datetime.strptime(r_date_str, "%Y-%m-%d").date()
                start_date = r_date - timedelta(days=days_before)
                end_date = r_date + timedelta(days=days_after)
                if start_date <= signal_date <= end_date:
                    return True, r_date_str, roll_cfg
            except Exception as e:
                logger.error(f"Failed to parse rollover date {r_date_str}: {e}")
                
        return False, None, None

    def process_signal(self, signal: SignalPayload) -> Optional[Dict[str, Any]]:
        """Orchestrate the 5 layers of Risk Sentinel logic. Returns trade instructions or None."""
        logger.info(f"Risk Sentinel processing signal from {signal.factor_name} (Action={signal.action_signal}).")

        # Dynamic loading of asset configuration parameters
        symbol = getattr(signal, "symbol", "FCPO")
        asset_cfg = self.assets.get(symbol, {})
        multiplier = asset_cfg.get("multiplier", 25.0)
        tick_size = asset_cfg.get("tick_size", 1.0)
        margin_per_lot = asset_cfg.get("margin_per_lot", 8000.0)
        
        # Display name positive naming correction
        asset_display = f"{symbol} (美豆油 CBOT)" if symbol == "ZL" else f"{symbol} (大马棕榈油 FCPO)"
        if symbol == "FKLI":
            asset_display = f"{symbol} (大马综指期货 FKLI)"

        # ==========================================
        # Layer 1: Position State Awareness & Anti-Fool
        # ==========================================
        state = db.load_portfolio_state(self.db_uri, self.strategy_id, self.initial_capital)
        current_capital = state["current_capital"]
        pos_direction = state["position_direction"]  # -1, 0, 1
        pos_lots = state["position_lots"]
        
        # Rollover Risk State Machine Interception
        in_rollover, roll_date, roll_cfg = self.is_rollover_period(symbol, signal.timestamp)
        is_forced_close = False
        is_emergency = getattr(signal, "is_emergency_rollover", False)
        
        # Local copy to prevent mutating incoming payload object references
        action_signal = signal.action_signal
        
        if is_emergency:
            logger.warning("🚨 [EMERGENCY ROLLOVER DETECTED] Dynamic price jump triggered callback.")
            if pos_direction != 0:
                logger.warning("FORCED LIQUIDATION: Override signal to force-close active position immediately.")
                action_signal = 0
                is_forced_close = True
            else:
                logger.info("Emergency rollover signal received but position is already flat. Ignoring.")
                return None
        elif in_rollover:
            policy = roll_cfg.get("policy", "reject")
            logger.warning(
                f"🚨 [ROLLOVER RISK WINDOW ACTIVE] Signal time {signal.timestamp} is within "
                f"rollover period (Date: {roll_date}, Policy: {policy})."
            )
            
            if policy == "reject":
                if pos_direction != 0:
                    if action_signal != 0:
                        logger.warning("FORCED LIQUIDATION: Override signal to force-close active position.")
                        action_signal = 0
                        is_forced_close = True
                else:
                    if action_signal in [1, -1]:
                        print("\n" + "=" * 70)
                        print("⚠️⚠️⚠️ 【风控拦截：处于换月风险期 - 开仓指令已拒绝】 ⚠️⚠️⚠️")
                        print("=" * 70)
                        print(f"  [时间]   : {signal.timestamp}")
                        print(f"  [标的]   : {asset_display}")
                        print(f"  [换月日] : {roll_date}")
                        print(f"  [说明]   : 处于换月日前后窗口，禁止建立新仓位以规避滑点及流动性风险！")
                        print("=" * 70 + "\n")
                        return None
                        
        # Intercept 99 (Hold/Maintain state) - no signal action required
        if action_signal == 99:
            logger.info("Signal is 99 (Hold). Maintaining current position state. Wind control green light.")
            return None
            
        # Anti-Fool: If signal tries to buy more when already long and full
        if action_signal == 1 and pos_direction == 1 and pos_lots >= self.max_position_lots:
            logger.warning("Anti-Fool Intercept: Already holding maximum long position. Signal blocked.")
            return None
        # Anti-Fool: If signal tries to sell more when already short and full
        if action_signal == -1 and pos_direction == -1 and pos_lots >= self.max_position_lots:
            logger.warning("Anti-Fool Intercept: Already holding maximum short position. Signal blocked.")
            return None

        # Determine if this is a Closing action, Opening action, or Inverse Closing action
        action_type = "STANDBY"
        target_direction = action_signal  # -1, 0, 1
        
        if target_direction == 0:
            if pos_direction == 0:
                logger.info("Portfolio already empty. Exit signal ignored.")
                return None
            action_type = "CLOSE"
        elif pos_direction != 0 and target_direction == -pos_direction:
            # Opposing signal -> One-Step Reverse Switch!
            action_type = "REVERSE_SWITCH"
        elif pos_direction == 0 and target_direction != 0:
            action_type = "OPEN"
        elif pos_direction != 0 and target_direction == pos_direction:
            action_type = "ADD"

        # ==========================================
        # Layer 2: Dynamic Position Sizing (OPEN / REVERSE_SWITCH / ADD)
        # ==========================================
        atr = signal.volatility_metric
        stop_loss_distance = 2.0 * atr
        
        if action_type in ["OPEN", "REVERSE_SWITCH", "ADD"]:
            max_loss_allowed = current_capital * self.risk_pct
            risk_per_lot = stop_loss_distance * multiplier
            
            # Formula: Target_Lots = Floor(Max_Loss / Risk_Per_Lot)
            if risk_per_lot > 0:
                target_lots = math.floor(max_loss_allowed / risk_per_lot)
            else:
                target_lots = 1
                
            # Enforce at least 1 lot to trade
            target_lots = max(1, target_lots)
        else:
            target_lots = pos_lots  # Closing out everything

        # ==========================================
        # Layer 3: Compliance & Hard Interception
        # ==========================================
        if action_type in ["OPEN", "REVERSE_SWITCH", "ADD"]:
            # Hard limit truncation
            final_lots = min(target_lots, self.max_position_lots)
            
            # Apply rollover reduction if active and policy is reduce
            if in_rollover and not is_emergency and roll_cfg.get("policy") == "reduce":
                reduce_ratio = roll_cfg.get("reduce_ratio", 0.5)
                old_lots = final_lots
                reduced_lots = math.floor(final_lots * reduce_ratio)
                if reduced_lots == 0:
                    # Small capital trap fallback to reject
                    print("\n" + "=" * 70)
                    print("⚠️⚠️⚠️ 【风控拦截：降仓不足开仓 - 指令已拦截】 ⚠️⚠️⚠️")
                    print("=" * 70)
                    print(f"  [时间]   : {signal.timestamp}")
                    print(f"  [标的]   : {asset_display}")
                    print(f"  [换月日] : {roll_date}")
                    print(f"  [说明]   : 降仓后手数为 0 (原 {old_lots} 手 * {reduce_ratio})，资金规模不足以在换月期安全建仓，拒绝该笔交易。")
                    print("=" * 70 + "\n")
                    return None
                else:
                    final_lots = reduced_lots
                    logger.info(f"Rollover Risk Sizing: Position reduced from {old_lots} to {final_lots} lots due to reduce policy.")
            
            # Margin Check:
            required_margin = final_lots * margin_per_lot
            if required_margin > current_capital:
                logger.warning(
                    f"Margin Warning: Required margin RM {required_margin:.1f} exceeds capital RM {current_capital:.1f}. "
                    f"Truncating lots."
                )
                final_lots = int(current_capital // margin_per_lot)
                if final_lots <= 0:
                    logger.error("Margin Check Failed: Insufficient capital to open even 1 lot! Signal rejected.")
                    return None
        else:
            final_lots = pos_lots  # Close all lots

        # ==========================================
        # Layer 4: Output Routing & Tick Rounding
        # ==========================================
        raw_entry_price = signal.current_price
        
        # Calculate clean entry & stop loss prices
        entry_price = self.clean_price(raw_entry_price, tick_size)
        if target_direction == 1:  # Long
            stop_loss = self.clean_price(entry_price - stop_loss_distance, tick_size)
            action_name = "BUY LONG (做多开仓)"
        elif target_direction == -1:  # Short
            stop_loss = self.clean_price(entry_price + stop_loss_distance, tick_size)
            action_name = "SELL SHORT (做空开仓)"
        else:  # Close
            stop_loss = 0.0
            action_name = "EXIT CLOSE (平仓出局)"
            if pos_direction == 1:
                action_name = "SELL EXIT (平多出局)"
            elif pos_direction == -1:
                action_name = "BUY EXIT (平空出局)"
                
            if is_forced_close:
                if is_emergency:
                    action_name = "EMERGENCY ROLLOVER LIQUIDATION (紧急换月强制平仓)"
                else:
                    action_name = "FORCED ROLLOVER LIQUIDATION (换月强制清仓)"
                
        if action_type == "REVERSE_SWITCH":
            stop_loss = self.clean_price(entry_price - stop_loss_distance if target_direction == 1 else entry_price + stop_loss_distance, tick_size)
            suggested_lots_to_trade = pos_lots + final_lots
            action_name = "REVERSE SWITCH (反手开仓交易 - 一步翻仓)"
            
            # Print detailed flip warning
            print("\n" + "=" * 70)
            print("🚨🚨🚨 【风控系统：反向翻仓审核绿灯 - 交易指令下达】 🚨🚨🚨")
            print("=" * 70)
            print(f"  [时间]   : {signal.timestamp}")
            print(f"  [标的]   : {asset_display}")
            print(f"  [类型]   : REVERSE_SWITCH ({action_name})")
            print(f"  [平仓旧仓] : {pos_lots} 手 (原方向: {'做多' if pos_direction == 1 else '做空'})")
            print(f"  [开立新仓] : {final_lots} 手 (新方向: {'做多' if target_direction == 1 else '做空'})")
            print(f"  [物理下单] : 请前往软件执行 【{'卖出' if target_direction == -1 else '买入'}】 {suggested_lots_to_trade} 手")
            print(f"  [建议价] : RM {entry_price}")
            print(f"  [新止损价] : RM {stop_loss} (硬性止损！请在交易端同时挂单！)")
            print("-" * 70)
            print("  请立即前往 Bursa derivatives 虚拟交易软件执行上述翻仓操作！")
            print("  执行完毕后，请在下方交互栏中如实输入实际成交数据进行状态确认。")
            print("=" * 70 + "\n")
        else:
            suggested_lots_to_trade = final_lots
            # High-Contrast large ASCII display for other actions
            print("\n" + "=" * 70)
            print("🚨🚨🚨 【风控拦截系统：审核绿灯通过 - 交易指令下达】 🚨🚨🚨")
            print("=" * 70)
            print(f"  [时间]   : {signal.timestamp}")
            print(f"  [标的]   : {asset_display}")
            print(f"  [类型]   : {action_type} ({action_name})")
            print(f"  [手数]   : {final_lots} 手")
            print(f"  [建议价] : RM {entry_price}")
            if stop_loss > 0:
                print(f"  [止损价] : RM {stop_loss} (硬性止损！请在交易端同时挂单！)")
            print("-" * 70)
            print("  请立即前往 Bursa derivatives 虚拟交易软件执行上述操作！")
            print("  执行完毕后，请在下方交互栏中如实输入实际成交数据进行状态确认。")
            print("=" * 70 + "\n")
        
        # Audio alert: raise terminal bell
        print("\a", end="")

        # Return structured instruction details for Layer 5 CLI processing
        return {
            "action_type": action_type,
            "target_direction": target_direction,
            "suggested_lots": final_lots,
            "suggested_lots_to_trade": suggested_lots_to_trade,
            "suggested_price": entry_price,
            "suggested_stop": stop_loss,
            "pos_direction": pos_direction,
            "pos_lots": pos_lots,
            "current_capital": current_capital,
            "signal_timestamp": signal.timestamp,
            "tick_size": tick_size,
            "multiplier": multiplier,
            "asset_display": asset_display
        }

    def execute_feedback_loop(self, instruction: Dict[str, Any]) -> None:
        """Layer 5: Thread suspension, input validation loop, slippage logs and DB state synchronization."""
        # 1. Thread safe CLI validation loop
        filled_lots = 0
        filled_price = 0.0
        
        # Validation for actual filled lots
        suggested_lots_to_trade = instruction.get("suggested_lots_to_trade", instruction.get("suggested_lots", 1))
        while True:
            try:
                lots_input = input(f"👉 [请输入实际成交手数 (建议: {suggested_lots_to_trade} 手) | 输入 0 放弃本次交易]: ").strip()
                if not lots_input:
                    print("⚠️ 输入不能为空，请重新输入。")
                    continue
                filled_lots = int(lots_input)
                if filled_lots < 0:
                    print("⚠️ 手数不能为负数，请重新输入。")
                    continue
                break
            except ValueError:
                print("⚠️ 输入格式错误！请输入合法的整数手数（例如 2）。")

        # 2. Escape Trade Abort Mechanism
        if filled_lots == 0:
            logger.warning(
                f"\n❌ [TRADE ABORTED - 交易放弃机制激活] ❌\n"
                f"User requested to abort signal for {instruction['signal_timestamp']}.\n"
                f"SQLite portfolio_state remains completely unchanged. Resuming scanning cycle.\n"
            )
            return

        # Validation for actual execution price (TCA Slippage logs)
        while True:
            try:
                price_input = input(f"👉 [请输入实际成交价格 (建议: RM {instruction['suggested_price']})]: ").strip()
                if not price_input:
                    print("⚠️ 输入不能为空，请重新输入。")
                    continue
                filled_price = float(price_input)
                if filled_price <= 0.0:
                    print("⚠️ 价格必须大于 0，请重新输入。")
                    continue
                break
            except ValueError:
                print("⚠️ 输入格式错误！请输入合法的价格浮点数（例如 4550.0）。")

        # Retrieve dynamic parameters with safe fallbacks for backwards compatibility
        tick_size = instruction.get("tick_size", getattr(self, "tick_size", 1.0))
        multiplier = instruction.get("multiplier", getattr(self, "multiplier", 25.0))
        
        # Round filled price to exchange tick just to ensure DB is clean
        filled_price = self.clean_price(filled_price, tick_size)
        
        # Calculate capital and position updates based on filled values
        actual_direction = instruction["target_direction"]
        current_capital = instruction["current_capital"]
        prev_direction = instruction["pos_direction"]
        prev_lots = instruction["pos_lots"]
        action_type = instruction["action_type"]
        
        # Slippage computation & Logging
        suggested_price = instruction["suggested_price"]
        slippage = filled_price - suggested_price
        # Adjust slippage sign based on direction (buying higher or selling lower is negative slippage)
        if actual_direction == 1:  # Long
            slippage_cost = slippage
        elif actual_direction == -1:  # Short
            slippage_cost = -slippage
        else:
            slippage_cost = 0.0
            
        logger.info(
            f"TCA Slippage Audit: Suggested Price = RM {suggested_price} | "
            f"Actual Price = RM {filled_price} | Slippage Cost = {slippage_cost:+.1f} points."
        )

        # Update portfolio parameters
        if action_type == "CLOSE":
            # standard closing trade (flatting position)
            # PnL = (Exit_Price - Entry_Price) * direction * lots * Multiplier
            entry_price = db.load_portfolio_state(self.db_uri, self.strategy_id, self.initial_capital)["average_entry_price"]
            if entry_price is not None:
                pnl = (filled_price - entry_price) * prev_direction * filled_lots * multiplier
                new_capital = current_capital + pnl
                logger.info(f"Position exited. Realized PnL = RM {pnl:+.1f}. Capital updated to RM {new_capital:.1f}.")
            else:
                new_capital = current_capital
                logger.warning("No entry price record found. Capital unchanged.")
                
            new_direction = 0
            new_lots = 0
            new_entry_price = None
            
        elif action_type == "REVERSE_SWITCH":
            # 一步翻仓 realized PnL 结算:
            # We flat prev_lots at filled_price, then open a new position of size (filled_lots - prev_lots)
            # Realized PnL is calculated exactly on the prev_lots flatting:
            entry_price = db.load_portfolio_state(self.db_uri, self.strategy_id, self.initial_capital)["average_entry_price"]
            if entry_price is not None:
                # Flat PnL calculation strictly incorporates direction:
                # PnL = (filled_price - entry_price) * prev_direction * prev_lots * Multiplier
                pnl = (filled_price - entry_price) * prev_direction * prev_lots * multiplier
                new_capital = current_capital + pnl
                logger.info(f"Position reversed. Old position of {prev_lots} lots flatted. Realized PnL = RM {pnl:+.1f}. Capital updated to RM {new_capital:.1f}.")
            else:
                new_capital = current_capital
                logger.warning("No entry price record found for old position. Capital unchanged.")
                
            # New position parameters:
            new_direction = actual_direction
            new_lots = max(1, filled_lots - prev_lots)  # The new position size
            new_entry_price = filled_price
            logger.info(f"New opposite position of {new_lots} lots established at RM {new_entry_price}.")
            
        elif action_type == "ADD":
            # VWAP Position Accumulation:
            entry_price = db.load_portfolio_state(self.db_uri, self.strategy_id, self.initial_capital)["average_entry_price"]
            if entry_price is not None:
                new_lots = prev_lots + filled_lots
                new_direction = actual_direction
                new_capital = current_capital
                # VWAP:
                new_entry_price = ((prev_lots * entry_price) + (filled_lots * filled_price)) / new_lots
                logger.info(f"Position added. Total Lots = {new_lots}. VWAP Entry Price = RM {new_entry_price:.2f}.")
            else:
                new_lots = filled_lots
                new_direction = actual_direction
                new_capital = current_capital
                new_entry_price = filled_price
                logger.warning("No previous average entry price found. Setting new entry price.")
                
        else: # action_type == "OPEN"
            # Opening a new trade
            new_lots = filled_lots
            new_direction = actual_direction
            new_capital = current_capital
            new_entry_price = filled_price

        # Update portfolio state database (Write-back with ThreadPoolExecutor safety)
        from main import db_executor
        try:
            future = db_executor.submit(
                db.save_portfolio_state,
                self.db_uri,
                self.strategy_id,
                new_capital,
                new_direction,
                new_lots,
                new_entry_price
            )
            # Safe blocking execution with exception safety wrapping!
            future.result()
            logger.info("Successfully persisted portfolio state update to database.")
        except Exception as e:
            logger.critical(f"🚨 [CRITICAL DATABASE WRITE ERROR] SQLite state write-back failed: {e}")
            print(f"❌ [数据写入失败] 无法安全同步本地持仓状态: {e}，请手动保留成交记录！")
            
        logger.info(
            f"Portfolio state successfully synced in SQLite: "
            f"Capital = RM {new_capital:.1f} | Position = {new_direction} ({new_lots} lots) | "
            f"Avg Price = {new_entry_price}"
        )
        print("✅ [交易状态同步成功] 正在返回前台扫描死循环...\n")
