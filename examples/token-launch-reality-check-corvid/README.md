# Token launch reality check — Corvid (Corvid by Opus 5) on Base

**Status:** public-data demonstration on a live token that launched the same day.
Not an audit, certification, safety verdict, investment recommendation, or token
promotion.

## Verdict

`manual_review_required` — two verifiable launch-reality flags: the contract is a
**minimal delegatecall proxy** (mutable token logic) and `owner()` returns a
**non-zero address** (ownership authority not renounced). On-chain metadata
matches the indexer's claims.

## Target

- **Token:** Corvid by Opus 5 — `Corvid`
- **Address:** `0xd8c33062a461251cf5669b0490a88f023bf45ba3` (Base, chain 8453)
- **Liquidity (context only):** Corvid / WETH pool created `2026-08-14T14:02:57Z`
  (≈ $11.4k reserve at capture; liquidity pairing not checked by the checker)
- **Market context at capture (GeckoTerminal):** FDV ≈ $11.4k, 24h volume ≈ $3.4k,
  no website / Twitter / Telegram / Discord listed
- **Captured:** 2026-08-14T14:11:41Z, pinned block 49963650, bounded Transfer
  window blocks 49963450–49963650

## Claim vs observation

| Field | Claimed (GeckoTerminal) | Observed (on-chain) | Result |
|---|---|---|---|
| chain_id | 8453 | 8453 | match |
| address | `0xd8c330…45ba3` | `0xd8c330…45ba3` | match |
| symbol | `Corvid` | `Corvid` | match |
| decimals | 18 | 18 | match |
| name | Corvid by Opus 5 | Corvid by Opus 5 | match |
| total_supply | 100,000,000,000 (1e29 wei) | 1e29 wei | match |

## Control surfaces — two flags

1. **`owner()` returns a non-zero address** — `0x660eaaed…a8d12`. Ownership
   authority is held by a single address and has not been renounced; whatever
   owner-gated functions the contract exposes (typically mint / fee / transfer
   restrictions) remain callable by that address.

2. **Minimal delegatecall proxy (Solady LibClone pattern).** Runtime code is 44
   bytes (`sha256 42478fba…fce4d`) and delegatecalls to an implementation at
   `0xdb7b520b…be87` embedded in the bytecode. The standard EIP-1967
   implementation slot is empty, so a slot-only upgradeability read would miss
   this — but the token's logic lives in a swappable implementation, i.e. it is
   **mutable**.

## Bounded Transfer flow (200 blocks)

75 Transfer events, 9 distinct non-zero addresses. Net positive flow is
**99.95% concentrated in a single address** (`0x498581ff…2b2b`) — the token
supply is effectively in one account at launch. This is event-flow evidence
only; it does not identify beneficial ownership or a complete holder snapshot.

## Limitations

- A clean read proves nothing about source correctness, admin intent, solvency,
  or safety; a flagged read is an observation, not a fraud determination.
- Permission checks cover common `owner()`/`minter()`/`paused()` surfaces and
  EIP-1967 slots, not every custom control path.
- Minimal-proxy detection matches only well-known bytecode prefixes (EIP-1167
  and Solady LibClone); other delegatecall/clone patterns are not detected.
- Transfer concentration is bounded to the configured 200-block window.

## Reproduce

```sh
python3 tools/token_reality_check.py \
  --input examples/token-launch-reality-check-corvid/manifest.json \
  --output examples/token-launch-reality-check-corvid/report.json \
  --format text
```

## Sources

- https://api.geckoterminal.com/api/v2/networks/base/tokens/0xd8c33062a461251cf5669b0490a88f023bf45ba3
- https://api.geckoterminal.com/api/v2/networks/base/tokens/0xd8c33062a461251cf5669b0490a88f023bf45ba3/pools
- https://base.blockscout.com/address/0xd8c33062a461251cf5669b0490a88f023bf45ba3
