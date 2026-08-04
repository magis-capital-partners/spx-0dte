from datetime import date

from calendar_sizing import CalendarAdjustedPolicy, is_last_weekday_of_month, is_monthly_opex
from mbh_simulator import StrategyConfig


class FixedPolicy:
    def contracts(self, _signal, _config):
        return 10


class Signal:
    def __init__(self, day):
        from datetime import datetime
        self.timestamp = datetime.combine(day, datetime.min.time())


def test_calendar_dates_and_multiplier_order():
    assert is_monthly_opex(date(2025, 5, 16))
    assert is_last_weekday_of_month(date(2025, 5, 30))
    policy = CalendarAdjustedPolicy(FixedPolicy(), {"2025-05-16", "2025-05-30"})
    cfg = StrategyConfig()
    assert policy.contracts(Signal(date(2025, 5, 16)), cfg) == 20
    assert policy.contracts(Signal(date(2025, 5, 30)), cfg) == 5
