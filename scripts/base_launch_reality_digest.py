#!/usr/bin/env python3
"""Sweep today's freshest Base token launches and run a reality check on each.

Read-only, public-data-only. Enumerates GeckoTerminal ``new_pools`` for Base,
dedupes by base token, fetches each token's indexer metadata, builds a
checker manifest, and runs ``tools/token_reality_check.build_report`` with a
throttled JSON-RPC opener so the public Base RPC is not bursted. Emits a
consolidated digest (JSON + Markdown) and individual reports.

Run:  python3 scripts/base_launch_reality_digest.py [--max-tokens N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import token_reality_check as trc  # noqa: E402

RPC_URL = "https://mainnet.base.org"
GT_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/base/new_pools?page={page}"
GT_TOKEN = "https://api.geckoterminal.com/api/v2/networks/base/tokens/{address}"
HOLDER_WINDOW = 200
RPC_DELAY = 1.5  # seconds between JSON-RPC calls; avoids the public RPC's 429 bursts

UA = {"User-Agent": "nightshift-launch-reality-digest", "Accept": "application/json"}


def get_json(url: str, retries: int = 3) -> object:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers=UA)
            with urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def throttled_opener(request: Request, timeout: int | None = None):
    time.sleep(RPC_DELAY)
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return urlopen(request, **kwargs)


def parse_pools(pages: int) -> list[dict]:
    """Return unique base tokens (freshest first) as
    {address, symbol, name, decimals, pool_created_at, pool_name, dex, fdv_usd, reserve_usd}."""
    tokens: dict[str, dict] = {}
    for page in range(1, pages + 1):
        try:
            data = get_json(GT_NEW_POOLS.format(page=page))
        except Exception as exc:  # noqa: BLE001
            print(f"  new_pools page {page} failed: {exc}", file=sys.stderr)
            continue
        for pool in (data.get("data") or []):
            rels = pool.get("relationships") or {}
            base = (rels.get("base_token") or {}).get("data") or {}
            token_id = base.get("id") or ""
            if not token_id.startswith("base_0x"):
                continue
            address = token_id.split("_", 1)[1]  # already "0x…" after the "base_" prefix
            if not trc.ADDRESS_RE.fullmatch(address):
                continue
            attr = pool.get("attributes") or {}
            dex = ((rels.get("dex") or {}).get("data") or {}).get("id", "")
            if address in tokens:
                continue
            tokens[address] = {
                "address": address,
                "pool_created_at": attr.get("pool_created_at"),
                "pool_name": attr.get("name"),
                "dex": dex,
                "fdv_usd": attr.get("fdv_usd"),
                "reserve_in_usd": attr.get("reserve_in_usd"),
                "symbol": None,
                "name": None,
                "decimals": None,
            }
    return list(tokens.values())


def enrich_metadata(tokens: list[dict]) -> list[dict]:
    for tok in tokens:
        try:
            data = get_json(GT_TOKEN.format(address=tok["address"]))
            attr = (data.get("data") or {}).get("attributes") or {}
            tok["symbol"] = attr.get("symbol")
            tok["name"] = attr.get("name")
            tok["decimals"] = attr.get("decimals")
        except Exception as exc:  # noqa: BLE001
            print(f"  token detail {tok['address']} failed: {exc}", file=sys.stderr)
    return tokens


def run_check(tok: dict, latest_block: int) -> dict:
    claims = {"chain_id": 8453, "address": tok["address"]}
    if tok.get("symbol") is not None:
        claims["symbol"] = tok["symbol"]
    if tok.get("decimals") is not None:
        claims["decimals"] = int(tok["decimals"])
    manifest = {
        "rpc_url": RPC_URL,
        "token_address": tok["address"],
        # Pin every state read to the capture block.  Without this, the digest
        # fetches a block number but the report silently falls back to moving
        # `latest`, making the sample impossible to reproduce later.
        "observation_block": latest_block,
        "claims": claims,
        "holder_scan": {
            "from_block": max(0, latest_block - HOLDER_WINDOW),
            "to_block": latest_block,
            "chunk_size": HOLDER_WINDOW,
            "complete": False,
        },
        "source_urls": [
            GT_TOKEN.format(address=tok["address"]),
            f"https://api.geckoterminal.com/api/v2/networks/base/tokens/{tok['address']}/pools",
        ],
    }
    try:
        report = trc.build_report(manifest, opener=throttled_opener)
    except Exception as exc:  # noqa: BLE001
        return {"address": tok["address"], "error": str(exc)}
    return report


def latest_block() -> int:
    return int(
        trc.rpc_call(
            RPC_URL, "eth_blockNumber", [], opener=throttled_opener
        ),
        16,
    )


def summarize(report: dict) -> dict:
    if "error" in report:
        return report
    dist = report.get("distribution") or {}
    top = (dist.get("top_addresses") or [])
    top_share = top[0].get("share_of_positive_flow_pct") if top else None
    return {
        "address": report["token"]["address"],
        "name": (report["token"]["metadata"] or {}).get("name"),
        "symbol": (report["token"]["metadata"] or {}).get("symbol"),
        "review_status": report.get("review_status"),
        "upgradeability": (report.get("upgradeability") or {}).get("status"),
        "owner": (report.get("permissions") or {}).get("owner"),
        "transfer_events": dist.get("transfer_events"),
        "unique_addresses": dist.get("unique_nonzero_addresses"),
        "top_flow_share_pct": top_share,
        "flags": report.get("review_flags") or [],
    }


def render_markdown(digest: dict) -> str:
    lines = [
        "# Base launch reality-check digest — " + digest["date"],
        "",
        "Read-only, public-data observations on the freshest Base token launches",
        f"(pool created on {digest['date']}). Not an audit, certification, safety",
        "verdict, or investment recommendation. Reproduce:",
        "`python3 tools/token_reality_check.py --input <manifest.json> --format text`",
        f"(tool + samples: https://github.com/nighshift-labs/token-launch-reality-check).",
        "",
        f"Tokens checked: {digest['checked']} · flagged (manual_review_required): {digest['flagged']} · "
        f"clean (no_observed_mismatch): {digest['clean']} · errors: {digest['errors']}",
        "",
        "| Token | Address | Launched (UTC) | DEX | FDV USD | Verdict | Top flow share | Flags |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in digest["rows"]:
        name = row.get("name") or "?"
        symbol = row.get("symbol") or "?"
        label = f"{name} ({symbol})" if symbol != "?" else name
        addr = row["address"][:10] + "…"
        flags = row.get("flags") or []
        flag_txt = "; ".join(flags) if flags else "—"
        if row.get("error"):
            lines.append(f"| {label} | {addr} | {row.get('pool_created_at','?')} | {row.get('dex','?')} | {row.get('fdv_usd','?')} | ERROR | — | {row['error']} |")
            continue
        lines.append(
            f"| {label} | {addr} | {row.get('pool_created_at','?')} | {row.get('dex','?')} | "
            f"{row.get('fdv_usd','?')} | {row['review_status']} | "
            f"{row.get('top_flow_share_pct') if row.get('top_flow_share_pct') is not None else '—'}% | {flag_txt} |"
        )
    lines += [
        "",
        "## Notes",
        "",
        "- `manual_review_required` = one or more narrow, observable checks flagged; it is",
        "  not a fraud determination.",
        "- Top flow share = net positive Transfer flow concentrated in one address, bounded",
        "  to the 200-block window ending at capture; not a verified holder snapshot.",
        "- `no_observed_mismatch` = the bounded checks found nothing; it does not prove",
        "  safety, source correctness, or admin intent.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--out-dir", type=Path, default=Path("examples/base-launch-reality-digest"))
    args = ap.parse_args()

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tokens = parse_pools(args.pages)
    if not tokens:
        print("no fresh pools found", file=sys.stderr)
        return 1
    tokens = enrich_metadata(tokens[: args.max_tokens])

    print(f"latest block…")
    lb = latest_block()

    rows = []
    checked = flagged = clean = errors = 0
    for i, tok in enumerate(tokens, 1):
        print(f"check {i}/{len(tokens)} {tok.get('symbol') or tok['address'][:10]}…")
        report = run_check(tok, lb)
        row = summarize(report)
        row["pool_created_at"] = tok.get("pool_created_at")
        row["dex"] = tok.get("dex")
        row["fdv_usd"] = tok.get("fdv_usd")
        row["reserve_in_usd"] = tok.get("reserve_in_usd")
        rows.append(row)
        if row.get("error"):
            errors += 1
        else:
            checked += 1
            if row["review_status"] == "manual_review_required":
                flagged += 1
            else:
                clean += 1
        # persist the individual report for reproducibility
        out_dir = args.out_dir / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = tok["address"][2:]  # full address — unique, avoids prefix collisions
        (out_dir / f"{date}-{safe}.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )

    digest = {
        "date": date,
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "checked": checked,
        "flagged": flagged,
        "clean": clean,
        "errors": errors,
        "rows": rows,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.out_dir / f"{date}.md"
    json_path = args.out_dir / f"{date}.json"
    md_path.write_text(render_markdown(digest))
    json_path.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n")

    print(f"\nchecked={checked} flagged={flagged} clean={clean} errors={errors}")
    print(f"digest: {md_path}")
    print(render_markdown(digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
