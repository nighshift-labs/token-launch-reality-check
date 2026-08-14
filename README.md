# token-launch-reality-check

Deterministic, public-data-only reality checks for token launches on Base. Reads
a token contract, an optional liquidity pool, and a bounded Transfer-event range
over public JSON-RPC. It never signs, sends transactions, asks for wallet access,
or claims to be an audit, a safety verdict, or investment advice.

## What it checks

- **Claim vs observation** — reconcile supplied claims (chain id, address,
  symbol, decimals) against what the contract actually returns.
- **Control surfaces** — `owner()` / `minter()` / `paused()` getters.
- **Upgradeability** — EIP-1967 implementation/admin slots **and** minimal
  delegatecall proxies (EIP-1167 + Solady LibClone) detected from runtime
  bytecode.
- **Liquidity** — verify a supplied pool pairs the token with the expected quote
  asset (Uniswap-v3-like and Uniswap-v4 Initialize-event checks).
- **Bounded Transfer flow** — net-positive-flow concentration over a configured
  block range.

## Run

```sh
python3 tools/token_reality_check.py \
  --input examples/token-launch-reality-check-corvid/manifest.json \
  --output /tmp/report.json \
  --format text
```

The manifest pins the token address, claims to reconcile, the bounded block
window, and source URLs. See `examples/token-launch-reality-check-corvid/` and
`examples/token-launch-reality-check-quid/` for two live runs (one flagged, one
clean).

## Tests

```sh
cd tools && python3 -m unittest test_token_reality_check -v
```

## Limitations

- A clean read proves nothing about source correctness, admin intent, solvency,
  or safety; a flagged read is an observation, not a fraud determination.
- Permission checks cover common `owner()`/`minter()`/`paused()` surfaces and
  EIP-1967 slots, not every custom control path.
- Minimal-proxy detection matches only well-known bytecode prefixes (EIP-1167
  and Solady LibClone).
- Transfer concentration is bounded to the configured block range and does not
  identify beneficial ownership.

## Disclaimer

Not an audit, certification, investment recommendation, or token promotion.
Public chain-state reads only. No custody, no signing, no fund movement.

Operated as a disclosed AI-assisted project. License: MIT.
