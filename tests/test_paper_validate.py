import pytest
from paper_validate import require_paper

@pytest.mark.parametrize('accounts,selected', [(['U123'],'U123'), (['DU123'],'DU999'), (['DU123','DU456'],'')])
def test_unverified_or_live_account_rejected(accounts,selected):
    with pytest.raises(RuntimeError):
        require_paper(accounts,selected)

def test_verified_paper_account():
    assert require_paper(['DU123'],'DU123')=='DU123'

@pytest.mark.parametrize('timestamp,expected', [('2026-10-28T14:00:00Z',False),
    ('2026-10-28T14:12:00Z',True), ('2026-10-28T14:20:00Z',False),
    ('2026-10-28 14:12:00',False)])
def test_live_runner_rejects_delayed_or_ambiguous_bars(timestamp,expected):
    from config import BotConfig
    from ibkr_execution import IBKRNQBotRunner
    runner=IBKRNQBotRunner(BotConfig())
    assert runner._bar_is_fresh(timestamp, '2026-10-28T14:14:00Z') is expected
