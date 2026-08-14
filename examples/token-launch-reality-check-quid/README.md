# Token launch reality check — QUID (Squid) on Base

**Status:** public-data demonstration on a live token. Not an audit, certification, safety verdict, investment recommendation, or token promotion.

## Verdict

`no_observed_mismatch` — the token's on-chain metadata matches the indexer claims
checked. One unresolved control-surface question is flagged below.

## Target

- **Token:** Squid — `QUID`
- **Address:** `0x1a44233fae8d50f1aeb3a5d58dd426ff4814cb53` (Base, chain 8453)
- **Liquidity (context only):** QUID / USDC 0.01% on PancakeSwap v3 Base
  (`0x07c4bc0f…ccc4`, created 2026-08-03)
- **Market context at capture (GeckoTerminal):** FDV ≈ $72M, 24h volume ≈ $13.5M
- **Captured:** 2026-08-14T13:40Z, bounded Transfer window blocks 49962538–49962738

## Claim vs observation

| Field | Claimed (GeckoTerminal) | Observed (on-chain) | Result |
|---|---|---|---|
| chain_id | 8453 | 8453 | match |
| address | `0x1a4423…cb53` | `0x1a4423…cb53` | match |
| symbol | `QUID` | `QUID` | match |
| decimals | 18 | 18 | match |
| name | Squid | Squid | match |
| total_supply | 1,000,000,000 (1e27 wei) | 1e27 wei | match |

## Control surfaces

- `owner()` — **not exposed** (no standard getter)
- `minter()` — **not exposed**
- `paused()` — **not exposed**
- EIP-1967 implementation/admin slots — **empty** (no proxy observed; immutable
  deployment, runtime code 3,760 bytes, sha256
  `112c2e38…dc6206`)

**Unresolved disclosure question:** the contract returns no standard
`owner()`/`minter()`/`paused()` getters, so minting/transfer controls (if any)
are implemented through a non-standard path. This is not a mismatch — it means
the standard permission surfaces cannot be read this way. A project operator
would confirm access control from the verified source, which this public-data
check does not audit.

## Bounded Transfer flow (200 blocks)

623 Transfer events, 52 distinct non-zero addresses. Top net-positive-flow
receivers in the window:

| Address | Share of positive flow |
|---|---|
| `0x8b289ed9…c96` | 34.1% |
| `0x97b9d210…689` | 14.3% |
| `0xefc68ed0…2dd` | 9.4% |
| `0xbf123060…f26` | 9.4% |
| `0x60b9319e…b3a` | 9.0% |
| `0x63d8ce8c…7c8` | 8.9% |
| `0x07c4bc0f…ccc4` (pool) | 8.2% |

This is event-flow evidence only — it does not identify beneficial ownership or
a complete holder snapshot.

## Limitations

- A clean read proves nothing about source correctness, admin intent, solvency,
  or safety.
- Permission checks cover only common `owner()`/`minter()`/`paused()` surfaces
  and EIP-1967 slots, not custom control paths.
- Transfer concentration is bounded to the configured 200-block window.
- Liquidity was not checked (no pool configuration supplied to the checker).

## Reproduce

```sh
python3 tools/token_reality_check.py \
  --input examples/token-launch-reality-check-quid/manifest.json \
  --output examples/token-launch-reality-check-quid/report.json \
  --format text
```

## Sources

- https://api.geckoterminal.com/api/v2/networks/base/tokens/0x1a44233fae8d50f1aeb3a5d58dd426ff4814cb53
- https://api.geckoterminal.com/api/v2/networks/base/pools/0x07c4bc0f5fb6cb069124df3e1ae0b8fd8148ccc4
- https://dexscreener.com/base/0x07c4bc0f5fb6cb069124df3e1ae0b8fd8148ccc4
- https://base.blockscout.com/address/0x1a44233fae8d50f1aeb3a5d58dd426ff4814cb53
