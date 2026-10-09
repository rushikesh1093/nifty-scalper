"""
Replay engine (design §14, bug 11): drives the production run_cycle /
handle_signal / risk_gate / option_selector / fills code over stored history
under a replay RuntimeContext. Nothing under app/services/trading/,
app/strategies/ or app/services/metrics/ imports this package, and no decision
code branches on replay (tests/test_no_replay_branch.py). PAPER ONLY.
"""
