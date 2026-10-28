"""Broker-independent position lifecycle. Requests never imply acknowledgements."""
from dataclasses import dataclass, field
from math import isfinite
from indicators import round_to_tick


@dataclass
class PositionEngine:
    direction: str
    requested: float
    initial_stop: float
    one_r: float
    submitted_bar: int
    tick: float = .25
    filled: float = 0
    remaining: float = 0
    average: float = 0
    first_fill_bar: int | None = None
    stop: float | None = None
    protected_qty: float = 0
    pending_stop: float | None = None
    pending_reason: str = ""
    stop_reason: str = "Stop Loss"
    entry_terminal: bool = False
    blocked_reason: str = ""
    seen: set = field(default_factory=set)

    def __post_init__(self):
        if self.direction not in ("LONG", "SHORT") or self.requested <= 0 or self.one_r <= 0:
            raise ValueError("Invalid position intent")

    @property
    def state(self):
        if self.blocked_reason:
            return "BLOCKED"
        if not self.remaining:
            return "CLOSED" if self.entry_terminal else "ENTRY_PENDING"
        if self.protected_qty < self.remaining or self.stop is None:
            return "UNPROTECTED"
        return "AMEND_PENDING" if self.pending_stop is not None else "PROTECTED"

    def fill(self, execution_id, quantity, price, bar, entry=True):
        if execution_id in self.seen:
            return False
        if quantity <= 0 or not isfinite(price):
            raise ValueError("Invalid execution")
        if (entry and self.filled + quantity > self.requested) or (not entry and quantity > self.remaining):
            self.blocked_reason = "Unexpected execution quantity"
            raise ValueError(self.blocked_reason)
        self.seen.add(execution_id)
        if entry:
            self.average = (self.average * self.filled + quantity * price) / (self.filled + quantity)
            self.filled += quantity
            self.remaining += quantity
            if self.first_fill_bar is None:
                self.first_fill_bar = bar
            if self.filled == self.requested:
                self.entry_terminal = True
        else:
            self.remaining -= quantity
        return True

    def acknowledge_stop(self, price, quantity):
        if not isfinite(price) or quantity < 0:
            raise ValueError("Invalid protection acknowledgement")
        self.stop, self.protected_qty = price, quantity
        if self.pending_stop is not None and abs(price - self.pending_stop) < self.tick / 2:
            self.stop_reason = self.pending_reason
            self.pending_stop, self.pending_reason = None, ""

    def reject(self, reason):
        # Keep the last confirmed stop, but never silently continue after rejection.
        self.blocked_reason = reason
        self.pending_stop = None

    def cancel_entry(self):
        self.entry_terminal = True

    def entry_expired(self, bar, timeout):
        return not self.entry_terminal and timeout > 0 and bar - self.submitted_bar >= timeout

    def propose_stop(self, high, low, bar, risk):
        if self.state != "PROTECTED" or self.stop_reason == "Break-Even Stop":
            return None
        sign = 1 if self.direction == "LONG" else -1
        reached = high >= self.average + self.one_r if sign == 1 else low <= self.average - self.one_r
        if risk.breakeven_at_1r and reached:
            candidate = self.average + sign * risk.breakeven_offset_points
            reason = "Break-Even Stop"
        elif (risk.time_decay_bars > 0 and bar - self.first_fill_bar >= risk.time_decay_bars
              and self.stop_reason != "Time-Decay Stop (-0.5R)"):
            candidate = self.average - sign * risk.time_decay_stop_r * self.one_r
            if (sign == 1 and low <= candidate) or (sign == -1 and high >= candidate):
                return None
            reason = "Time-Decay Stop (-0.5R)"
        else:
            return None
        candidate = round_to_tick(candidate, self.tick)
        if sign * (candidate - self.stop) <= 0:
            return None
        self.pending_stop, self.pending_reason = candidate, reason
        return candidate

    def reconcile(self, broker_quantity, open_orders):
        # Restart/disconnect requires explicit reconciliation; never auto-adopt exposure.
        if broker_quantity or open_orders or self.remaining or not self.entry_terminal:
            self.blocked_reason = "Reconciliation required: exposure or working orders"
            return False
        self.blocked_reason = ""
        return True
