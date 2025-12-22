# IBKR cost basis, assignment detection, and fee ingestion (new issue)

**Context:** Commit `eda4138` introduced IBKR Greeks/IV sourcing and UI source tagging. The following IBKR-powered workflows still need implementation in separate follow-up work.

## Pending scope
- Build IBKR transaction ingestion to compute **true cost basis** for option trades.
- Detect and record **assignments/exercises** so Wheeler positions match IBKR.
- Import **transaction fees** per trade/assignment/expiration and surface them in P/L.

## Notes
- Track alongside existing Greeks/IV integration; can reuse the IBKR service plumbing already in place.
- Align outputs with the acceptance criteria in the open “Integrate IBKR Option Greeks, Volatility Surface, Cost Basis, Assignment, and Fee Support” issue.
