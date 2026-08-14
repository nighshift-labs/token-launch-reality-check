#!/usr/bin/env python3
"""Create a deterministic, public-data-only token launch reality check.

The checker reads a token contract, optional liquidity pool, and a bounded
Transfer-event range over JSON-RPC. It never signs, sends transactions, asks
for wallet access, or claims to be an audit or investment recommendation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX_RE = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
WORD_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ZERO_ADDRESS = "0x" + "00" * 20

# Standard public slots and selectors. They are read-only observations, not an
# attempt to enumerate every custom permission or upgrade mechanism.
IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e2ee1178d6a717850b5d6103"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# keccak256("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
V4_INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
# Minimal delegatecall-proxy runtime-code prefixes. These clones store the
# implementation address in their own bytecode (not an EIP-1967 storage slot),
# so a slot check alone misses them. Detected for completeness only.
EIP1167_PREFIX = bytes.fromhex("363d3d373d3d3d363d73")      # standard EIP-1167, impl @ [10:30]
SOLADY_PROXY_PREFIX = bytes.fromhex("3d3d3d3d363d3d37363d73")  # Solady LibClone, impl @ [11:31]
SELECTORS = {
    "name": "0x06fdde03",
    "symbol": "0x95d89b41",
    "decimals": "0x313ce567",
    "total_supply": "0x18160ddd",
    "owner": "0x8da5cb5b",
    "minter": "0x07546172",
    "paused": "0x5c975abb",
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "liquidity": "0x1a686502",
}


def normalize_address(address: str) -> str:
    if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
        raise ValueError("address must be a 20-byte 0x-prefixed hexadecimal address")
    return address.lower()


def parse_block(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative block number")
    if isinstance(value, int):
        block = value
    elif isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        block = int(value, 16)
    else:
        raise ValueError(f"{field} must be a non-negative block number")
    if block < 0:
        raise ValueError(f"{field} must be a non-negative block number")
    return block


def parse_word(value: object, field: str) -> int:
    if not isinstance(value, str) or not WORD_RE.fullmatch(value):
        raise ValueError(f"RPC returned malformed {field}")
    return int(value, 16)


def validate_rpc_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("rpc_url must be an HTTPS URL")
    rpc_url = value.strip()
    try:
        parsed = urlsplit(rpc_url)
        parsed.port
    except ValueError as exc:
        raise ValueError("rpc_url must be an HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("rpc_url must be an HTTPS URL")
    return rpc_url


def is_canonical_address_word(value: object) -> bool:
    return (
        isinstance(value, str)
        and WORD_RE.fullmatch(value) is not None
        and value[2:26] == "0" * 24
    )


def rpc_call(rpc_url: str, method: str, params: list[object], opener=urlopen) -> object:
    rpc_url = validate_rpc_url(rpc_url)
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        separators=(",", ":"),
    ).encode()
    request = Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "nightshift-token-reality-check"},
        method="POST",
    )
    with opener(request, timeout=20) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError(f"RPC {method} returned malformed JSON")
    if "error" in result:
        raise ValueError(f"RPC {method} failed: {result['error']}")
    if "result" not in result:
        raise ValueError(f"RPC {method} response had no result")
    return result["result"]


def eth_call(
    rpc_url: str,
    address: str,
    data: str,
    opener=urlopen,
    block_tag: str = "latest",
) -> object:
    return rpc_call(
        rpc_url,
        "eth_call",
        [{"to": normalize_address(address), "data": data}, block_tag],
        opener,
    )


def optional_eth_call(
    rpc_url: str,
    address: str,
    data: str,
    opener=urlopen,
    block_tag: str = "latest",
) -> object | None:
    try:
        result = eth_call(rpc_url, address, data, opener, block_tag)
    except (OSError, ValueError):
        return None
    if not isinstance(result, str) or result == "0x":
        return None
    return result


def code_summary(code: str) -> dict[str, int | str]:
    if not isinstance(code, str) or not HEX_RE.fullmatch(code):
        raise ValueError("RPC returned malformed runtime bytecode")
    raw = bytes.fromhex(code[2:])
    return {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def detect_minimal_proxy(code: str) -> dict[str, str] | None:
    """Detect a minimal delegatecall proxy from runtime bytecode.

    EIP-1167 and Solady LibClone clones embed their implementation address in
    the bytecode itself (not an EIP-1967 storage slot), so a slot read alone
    reports them as immutable. Returns the implementation address + pattern,
    or None when the bytecode does not match either well-known prefix.
    """
    if not isinstance(code, str) or not HEX_RE.fullmatch(code):
        raise ValueError("RPC returned malformed runtime bytecode")
    raw = bytes.fromhex(code[2:])
    if raw.startswith(EIP1167_PREFIX) and len(raw) >= 32 and raw[30:32] == b"\x5a\xf4":
        return {"pattern": "eip-1167", "implementation": "0x" + raw[10:30].hex()}
    if raw.startswith(SOLADY_PROXY_PREFIX) and len(raw) >= 33 and raw[31:33] == b"\x5a\xf4":
        return {"pattern": "solady-minimal-proxy", "implementation": "0x" + raw[11:31].hex()}
    return None


def decode_address_result(result: object) -> str | None:
    if result is None:
        return None
    if not is_canonical_address_word(result):
        raise ValueError("RPC returned malformed address result")
    address = "0x" + result[-40:]
    if address == ZERO_ADDRESS:
        return ZERO_ADDRESS
    return normalize_address(address)


def decode_uint_result(result: object) -> int | None:
    if not isinstance(result, str) or not WORD_RE.fullmatch(result):
        return None
    return int(result, 16)


def decode_bool_result(result: object) -> bool | None:
    value = decode_uint_result(result)
    return None if value is None else value != 0


def decode_text_result(result: object) -> str | None:
    if not isinstance(result, str) or not HEX_RE.fullmatch(result):
        return None
    raw = bytes.fromhex(result[2:])
    if not raw:
        return None
    # Normal ABI dynamic string: offset, length, bytes.
    if len(raw) == 32:
        # Older ERC-20s sometimes return bytes32 for name/symbol.
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace") or None
    if len(raw) < 64:
        raise ValueError("RPC returned malformed ABI string result")
    offset = int.from_bytes(raw[:32], "big")
    if offset < 32 or offset % 32 or offset + 32 > len(raw):
        raise ValueError("RPC returned malformed ABI string result")
    length = int.from_bytes(raw[offset : offset + 32], "big")
    end = offset + 32 + length
    if end > len(raw):
        raise ValueError("RPC returned malformed ABI string result")
    try:
        return raw[offset + 32 : end].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("RPC returned malformed ABI string result") from exc


def storage_address(value: object) -> str | None:
    if not is_canonical_address_word(value):
        raise ValueError("RPC returned malformed storage word")
    address = "0x" + value[-40:]
    return None if address == ZERO_ADDRESS else normalize_address(address)


def fetch_metadata(
    rpc_url: str,
    token_address: str,
    opener=urlopen,
    block_tag: str = "latest",
) -> dict[str, object | None]:
    decoders = {
        "name": decode_text_result,
        "symbol": decode_text_result,
        "decimals": decode_uint_result,
        "total_supply": decode_uint_result,
    }
    metadata: dict[str, object | None] = {}
    for field, decoder in decoders.items():
        result = optional_eth_call(rpc_url, token_address, SELECTORS[field], opener, block_tag)
        try:
            metadata[field] = decoder(result)
        except (UnicodeDecodeError, ValueError):
            metadata[field] = None
    return metadata


def compare_claims(claims: object, observed: dict[str, object]) -> dict[str, object]:
    if claims is None:
        return {"overall_status": "not_provided", "checks": []}
    if not isinstance(claims, dict):
        raise ValueError("claims must be an object")
    checks: list[dict[str, object]] = []
    for field in ("chain_id", "address", "symbol", "decimals"):
        if field not in claims:
            continue
        claimed = claims[field]
        actual = observed.get(field)
        if field == "address":
            try:
                claimed = normalize_address(claimed)  # type: ignore[arg-type]
            except ValueError:
                pass
        status = "match" if claimed == actual else "mismatch"
        checks.append({"field": field, "claimed": claimed, "observed": actual, "status": status})
    if not checks:
        overall_status = "not_provided"
    elif all(check["status"] == "match" for check in checks):
        overall_status = "match"
    elif any(check["status"] == "mismatch" for check in checks):
        overall_status = "mismatch"
    else:
        overall_status = "partial"
    return {"overall_status": overall_status, "checks": checks}


def decode_transfer_log(log: object) -> tuple[str, str, int]:
    if not isinstance(log, dict):
        raise ValueError("malformed Transfer log")
    topics = log.get("topics")
    data = log.get("data")
    if (
        not isinstance(topics, list)
        or len(topics) != 3
        or not all(isinstance(topic, str) and WORD_RE.fullmatch(topic) for topic in topics[:3])
        or not all(is_canonical_address_word(topic) for topic in topics[1:3])
        or topics[0].lower() != TRANSFER_TOPIC
        or not isinstance(data, str)
        or not WORD_RE.fullmatch(data)
    ):
        raise ValueError("malformed Transfer log")
    sender = normalize_address("0x" + topics[1][-40:])
    recipient = normalize_address("0x" + topics[2][-40:])
    return sender, recipient, int(data, 16)


def scan_transfer_distribution(
    rpc_url: str,
    token_address: str,
    config: object,
    opener=urlopen,
    default_to_block: int | None = None,
) -> dict[str, object]:
    if config is None:
        return {"status": "not_checked", "reason": "no holder_scan configuration supplied"}
    if not isinstance(config, dict):
        raise ValueError("holder_scan must be an object")
    if "from_block" not in config:
        raise ValueError("holder_scan.from_block is required")
    from_block = parse_block(config["from_block"], "holder_scan.from_block")
    if config.get("to_block") is None and default_to_block is not None:
        to_block = default_to_block
    elif config.get("to_block") is None:
        latest = rpc_call(rpc_url, "eth_blockNumber", [], opener)
        if not isinstance(latest, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", latest):
            raise ValueError("RPC returned malformed latest block")
        to_block = int(latest, 16)
    else:
        to_block = parse_block(config["to_block"], "holder_scan.to_block")
    if to_block < from_block:
        raise ValueError("holder_scan.to_block must not precede from_block")
    chunk_size = parse_block(config.get("chunk_size", 2_000), "holder_scan.chunk_size")
    if chunk_size == 0:
        raise ValueError("holder_scan.chunk_size must be positive")
    complete = config.get("complete", False)
    if not isinstance(complete, bool):
        raise ValueError("holder_scan.complete must be boolean")

    flows: defaultdict[str, int] = defaultdict(int)
    transfer_events = 0
    unique_addresses: set[str] = set()
    for start in range(from_block, to_block + 1, chunk_size):
        end = min(start + chunk_size - 1, to_block)
        logs = rpc_call(
            rpc_url,
            "eth_getLogs",
            [
                {
                    "address": normalize_address(token_address),
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "topics": [TRANSFER_TOPIC],
                }
            ],
            opener,
        )
        if not isinstance(logs, list):
            raise ValueError("RPC returned malformed Transfer log list")
        for log in logs:
            if (
                not isinstance(log, dict)
                or not isinstance(log.get("address"), str)
                or normalize_address(log["address"]) != normalize_address(token_address)
            ):
                raise ValueError("Transfer log address did not match token")
            log_block = parse_block(log.get("blockNumber"), "Transfer log block number")
            if not start <= log_block <= end:
                raise ValueError("Transfer log block number fell outside requested range")
            sender, recipient, amount = decode_transfer_log(log)
            flows[sender] -= amount
            flows[recipient] += amount
            transfer_events += 1
            if sender != ZERO_ADDRESS:
                unique_addresses.add(sender)
            if recipient != ZERO_ADDRESS:
                unique_addresses.add(recipient)

    positive_flows = {
        address: amount for address, amount in flows.items() if address != ZERO_ADDRESS and amount > 0
    }
    total_positive = sum(positive_flows.values())
    top_addresses = []
    for address, amount in sorted(positive_flows.items(), key=lambda item: (-item[1], item[0]))[:10]:
        share = round((amount / total_positive) * 100, 6) if total_positive else 0.0
        top_addresses.append(
            {
                "address": address,
                "net_positive_flow_raw": amount,
                "share_of_positive_flow_pct": share,
            }
        )
    return {
        "status": "observed" if complete else "bounded",
        "from_block": from_block,
        "to_block": to_block,
        "complete_history_claimed": complete,
        "transfer_events": transfer_events,
        "unique_nonzero_addresses": len(unique_addresses),
        "positive_flow_total_raw": total_positive,
        "top_addresses": top_addresses,
        "basis": "net_positive_transfer_flow",
        "note": (
            "This is a bounded Transfer-event flow screen, not a verified holder snapshot."
            if not complete
            else "Distribution is reconstructed from the supplied event range; independently verify the starting block."
        ),
    }


def decode_v4_initialize_log(log: object, pool_id: str) -> dict[str, object] | None:
    if not isinstance(log, dict):
        raise ValueError("malformed Uniswap v4 Initialize log")
    topics = log.get("topics")
    data = log.get("data")
    if not isinstance(topics, list) or len(topics) != 4:
        raise ValueError("malformed Uniswap v4 Initialize log")
    if not isinstance(topics[0], str) or not all(
        isinstance(topic, str) and WORD_RE.fullmatch(topic) for topic in topics[1:4]
    ):
        raise ValueError("malformed Uniswap v4 Initialize log")
    if not all(is_canonical_address_word(topic) for topic in topics[2:4]):
        raise ValueError("malformed Uniswap v4 Initialize currency topics")
    if not isinstance(data, str) or not HEX_RE.fullmatch(data):
        raise ValueError("malformed Uniswap v4 Initialize log")
    if topics[0].lower() != V4_INITIALIZE_TOPIC or topics[1].lower() != pool_id.lower():
        return None
    raw = bytes.fromhex(data[2:])
    if len(raw) != 32 * 5:
        raise ValueError("malformed Uniswap v4 Initialize data")

    def signed_word(index: int, bits: int) -> int:
        value = int.from_bytes(raw[index * 32 : (index + 1) * 32], "big") & ((1 << bits) - 1)
        return value - (1 << bits) if value & (1 << (bits - 1)) else value

    currency0 = decode_address_result(topics[2])
    currency1 = decode_address_result(topics[3])
    if currency0 is None or currency1 is None:
        raise ValueError("malformed Uniswap v4 Initialize currency topics")
    return {
        "currency0": currency0,
        "currency1": currency1,
        "fee": int.from_bytes(raw[0:32], "big"),
        "tick_spacing": signed_word(1, 24),
        "hooks": "0x" + raw[64 + 12 : 64 + 32].hex(),
        "sqrt_price_x96": int.from_bytes(raw[96:128], "big"),
        "tick": signed_word(4, 24),
        "block_number": log.get("blockNumber"),
        "transaction_hash": log.get("transactionHash"),
    }


def scan_v4_pool(
    rpc_url: str,
    token_address: str,
    config: object,
    opener=urlopen,
    default_to_block: int | None = None,
) -> dict[str, object]:
    if not isinstance(config, dict):
        raise ValueError("uniswap-v4 liquidity_pool must be an object")
    manager_address = normalize_address(config.get("manager_address"))  # type: ignore[arg-type]
    pool_id_raw = config.get("pool_id")
    if not isinstance(pool_id_raw, str) or not WORD_RE.fullmatch(pool_id_raw):
        raise ValueError("uniswap-v4 liquidity_pool.pool_id must be a 32-byte 0x-prefixed hexadecimal value")
    pool_id = pool_id_raw.lower()
    scan_config = config.get("initialize_scan")
    if not isinstance(scan_config, dict):
        raise ValueError("uniswap-v4 liquidity_pool.initialize_scan must be an object")
    if "from_block" not in scan_config:
        raise ValueError("uniswap-v4 initialize_scan.from_block is required")
    from_block = parse_block(scan_config["from_block"], "initialize_scan.from_block")
    if scan_config.get("to_block") is None and default_to_block is not None:
        to_block = default_to_block
    elif scan_config.get("to_block") is None:
        latest = rpc_call(rpc_url, "eth_blockNumber", [], opener)
        if not isinstance(latest, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", latest):
            raise ValueError("RPC returned malformed latest block")
        to_block = int(latest, 16)
    else:
        to_block = parse_block(scan_config["to_block"], "initialize_scan.to_block")
    if to_block < from_block:
        raise ValueError("initialize_scan.to_block must not precede from_block")
    chunk_size = parse_block(scan_config.get("chunk_size", 2_000), "initialize_scan.chunk_size")
    if chunk_size == 0:
        raise ValueError("initialize_scan.chunk_size must be positive")

    matches: list[dict[str, object]] = []
    for start in range(from_block, to_block + 1, chunk_size):
        end = min(start + chunk_size - 1, to_block)
        logs = rpc_call(
            rpc_url,
            "eth_getLogs",
            [
                {
                    "address": manager_address,
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "topics": [V4_INITIALIZE_TOPIC, pool_id],
                }
            ],
            opener,
        )
        if not isinstance(logs, list):
            raise ValueError("RPC returned malformed Uniswap v4 Initialize log list")
        for log in logs:
            if (
                not isinstance(log, dict)
                or not isinstance(log.get("address"), str)
                or normalize_address(log["address"]) != manager_address
            ):
                raise ValueError("Initialize log address did not match manager")
            log_block = parse_block(log.get("blockNumber"), "Initialize log block number")
            if not start <= log_block <= end:
                raise ValueError("Initialize log block number fell outside requested range")
            decoded = decode_v4_initialize_log(log, pool_id)
            if decoded is not None:
                matches.append(decoded)

    expected_quote = config.get("expected_quote_address")
    expected_quote_normalized = None
    if expected_quote is not None:
        expected_quote_normalized = normalize_address(expected_quote)  # type: ignore[arg-type]
    if not matches:
        return {
            "status": "unavailable",
            "manager_address": manager_address,
            "pool_id": pool_id,
            "pool_type_hint": "uniswap-v4",
            "expected_quote_address": expected_quote_normalized,
            "initialize_scan": {"from_block": from_block, "to_block": to_block},
            "live_state": "not_checked",
            "reason": "no matching Initialize event found in the supplied block range",
        }

    event = matches[0]
    currency0 = event["currency0"]
    currency1 = event["currency1"]
    pool_tokens = [currency0, currency1]
    target_matches = token_address in pool_tokens
    quote_matches = expected_quote_normalized is None or expected_quote_normalized in pool_tokens
    liquidity_status = "match" if target_matches and quote_matches else "mismatch"
    initialize_block = event["block_number"]
    if initialize_block is not None:
        initialize_block = parse_block(initialize_block, "Uniswap v4 Initialize block")
    return {
        "status": liquidity_status,
        "manager_address": manager_address,
        "pool_id": pool_id,
        "pool_type_hint": "uniswap-v4",
        "currency0": currency0,
        "currency1": currency1,
        "expected_quote_address": expected_quote_normalized,
        "initialize_block": initialize_block,
        "initialize_transaction_hash": event["transaction_hash"],
        "fee": event["fee"],
        "tick_spacing": event["tick_spacing"],
        "hooks": event["hooks"],
        "sqrt_price_x96": event["sqrt_price_x96"],
        "tick": event["tick"],
        "live_state": "not_checked",
        "note": (
            "Currency identity comes from the PoolManager Initialize event. "
            "This read does not measure reserves or current active liquidity."
        ),
    }


def observation_context(
    rpc_url: str,
    config: dict[str, object],
    opener=urlopen,
) -> tuple[dict[str, object | None], str, int | None]:
    raw_block = config.get("observation_block")
    if raw_block is None:
        return (
            {
                "mode": "latest",
                "block_number": None,
                "block_tag": "latest",
                "block_hash": None,
                "timestamp": None,
            },
            "latest",
            None,
        )

    block_number = parse_block(raw_block, "observation_block")
    block_tag = hex(block_number)
    block = rpc_call(rpc_url, "eth_getBlockByNumber", [block_tag, False], opener)
    if not isinstance(block, dict):
        raise ValueError("RPC returned malformed observation block")
    reported_number = parse_block(block.get("number"), "observation block number")
    if reported_number != block_number:
        raise ValueError("RPC observation block number did not match requested block")
    block_hash = block.get("hash")
    if not isinstance(block_hash, str) or not WORD_RE.fullmatch(block_hash):
        raise ValueError("RPC returned malformed observation block hash")
    timestamp = parse_block(block.get("timestamp"), "observation block timestamp")
    return (
        {
            "mode": "pinned",
            "block_number": block_number,
            "block_tag": block_tag,
            "block_hash": block_hash.lower(),
            "timestamp": timestamp,
        },
        block_tag,
        block_number,
    )


def build_report(config: dict[str, object], opener=urlopen, captured_at: str | None = None) -> dict[str, object]:
    if not isinstance(config, dict):
        raise ValueError("input must be a JSON object")
    rpc_url = validate_rpc_url(config.get("rpc_url"))
    token_address = normalize_address(config.get("token_address"))  # type: ignore[arg-type]
    observation, observation_tag, observation_number = observation_context(rpc_url, config, opener)

    chain_id_raw = rpc_call(rpc_url, "eth_chainId", [], opener)
    if not isinstance(chain_id_raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", chain_id_raw):
        raise ValueError("RPC returned malformed chain ID")
    chain_id = int(chain_id_raw, 16)
    code = rpc_call(rpc_url, "eth_getCode", [token_address, observation_tag], opener)
    runtime = code_summary(code)
    minimal_proxy = detect_minimal_proxy(code)
    metadata = fetch_metadata(rpc_url, token_address, opener, observation_tag)

    implementation_raw = rpc_call(
        rpc_url,
        "eth_getStorageAt",
        [token_address, IMPLEMENTATION_SLOT, observation_tag],
        opener,
    )
    admin_raw = rpc_call(rpc_url, "eth_getStorageAt", [token_address, ADMIN_SLOT, observation_tag], opener)
    implementation = storage_address(implementation_raw)
    admin = storage_address(admin_raw)

    owner = decode_address_result(
        optional_eth_call(rpc_url, token_address, SELECTORS["owner"], opener, observation_tag)
    )
    minter = decode_address_result(
        optional_eth_call(rpc_url, token_address, SELECTORS["minter"], opener, observation_tag)
    )
    paused = decode_bool_result(
        optional_eth_call(rpc_url, token_address, SELECTORS["paused"], opener, observation_tag)
    )
    permission_flags = []
    if owner not in (None, ZERO_ADDRESS):
        permission_flags.append("owner() returned a non-zero address; owner-controlled functions need review")
    if minter not in (None, ZERO_ADDRESS):
        permission_flags.append("minter() returned a non-zero address; mint authority needs review")
    if paused is True:
        permission_flags.append("paused() is true")

    observed = {
        "chain_id": chain_id,
        "address": token_address,
        "symbol": metadata["symbol"],
        "decimals": metadata["decimals"],
    }
    liquidity_config = config.get("liquidity_pool")
    if liquidity_config is None:
        liquidity: dict[str, object] = {"status": "not_checked", "reason": "no liquidity_pool configuration supplied"}
    else:
        if not isinstance(liquidity_config, dict):
            raise ValueError("liquidity_pool must be an object")
        if liquidity_config.get("type") == "uniswap-v4":
            liquidity = scan_v4_pool(
                rpc_url,
                token_address,
                liquidity_config,
                opener,
                default_to_block=observation_number,
            )
        else:
            pool_address = normalize_address(liquidity_config.get("address"))  # type: ignore[arg-type]
            token0 = decode_address_result(
                optional_eth_call(rpc_url, pool_address, SELECTORS["token0"], opener, observation_tag)
            )
            token1 = decode_address_result(
                optional_eth_call(rpc_url, pool_address, SELECTORS["token1"], opener, observation_tag)
            )
            liquidity_raw = decode_uint_result(
                optional_eth_call(rpc_url, pool_address, SELECTORS["liquidity"], opener, observation_tag)
            )
            expected_quote = liquidity_config.get("expected_quote_address")
            expected_quote_normalized = None
            if expected_quote is not None:
                expected_quote_normalized = normalize_address(expected_quote)  # type: ignore[arg-type]
            pool_tokens = [address for address in (token0, token1) if address is not None]
            target_matches = token_address in pool_tokens
            quote_matches = expected_quote_normalized is None or expected_quote_normalized in pool_tokens
            if token0 is None or token1 is None:
                liquidity_status = "unavailable"
            elif target_matches and quote_matches:
                liquidity_status = "match"
            else:
                liquidity_status = "mismatch"
            liquidity = {
                "status": liquidity_status,
                "pool_address": pool_address,
                "pool_type_hint": liquidity_config.get("type", "uniswap-v3-like"),
                "token0": token0,
                "token1": token1,
                "expected_quote_address": expected_quote_normalized,
                "liquidity_raw": liquidity_raw,
            }

    distribution = scan_transfer_distribution(
        rpc_url,
        token_address,
        config.get("holder_scan"),
        opener,
        default_to_block=observation_number,
    )
    claims = compare_claims(config.get("claims"), observed)
    review_flags = list(permission_flags)
    if implementation is not None:
        review_flags.append("EIP-1967 implementation slot is populated; upgradeability needs review")
    if minimal_proxy is not None:
        review_flags.append(
            "minimal delegatecall proxy observed ("
            + minimal_proxy["pattern"]
            + "); implementation at "
            + minimal_proxy["implementation"]
            + "; token logic is mutable"
        )
    if claims["overall_status"] == "mismatch":
        review_flags.append("one or more supplied disclosure claims do not match observed chain data")
    if liquidity.get("status") == "mismatch":
        if liquidity.get("pool_type_hint") == "uniswap-v4":
            review_flags.append("supplied Uniswap v4 pool ID/currency claims do not match the Initialize event")
        else:
            review_flags.append("supplied liquidity-pool address/asset claims do not match observed pool tokens")

    return {
        "schema_version": 1,
        "captured_at": captured_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scope": "public JSON-RPC reads only; not an audit, certification, investment advice, or token recommendation",
        "sources": config.get("source_urls", []),
        "observation": observation,
        "chain": {"chain_id": chain_id, "rpc_url": rpc_url},
        "token": {"address": token_address, "runtime_code": runtime, "metadata": metadata},
        "permissions": {"owner": owner, "minter": minter, "paused": paused, "review_flags": permission_flags},
        "upgradeability": {
            "implementation_slot": IMPLEMENTATION_SLOT,
            "implementation": implementation,
            "admin_slot": ADMIN_SLOT,
            "admin": admin,
            "minimal_proxy": minimal_proxy,
            "status": (
                "eip1967_observed"
                if implementation is not None
                else "minimal_proxy_observed"
                if minimal_proxy is not None
                else "no_eip1967_implementation_observed"
            ),
        },
        "liquidity": liquidity,
        "distribution": distribution,
        "claims": claims,
        "review_status": "manual_review_required" if review_flags else "no_observed_mismatch",
        "review_flags": review_flags,
        "limitations": [
            "A successful read does not prove source-code correctness, admin intent, solvency, or safety.",
            "Transfer-event concentration is only as complete as the configured block range and does not identify beneficial ownership.",
            "Permission checks cover common owner()/minter()/paused() surfaces and EIP-1967 slots, not every custom control path.",
            "Minimal-proxy detection matches only well-known bytecode prefixes (EIP-1167 and Solady LibClone); other delegatecall/clone patterns are not detected.",
            "Uniswap v4 pool checks use the supplied PoolManager and PoolId; currency identity is read from Initialize events, while live reserves/active liquidity are not checked.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="JSON manifest with public RPC/check inputs")
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.input.read_text())
        report = build_report(config)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        metadata = report["token"]["metadata"]  # type: ignore[index]
        print(f"chain {report['chain']['chain_id']}; token {report['token']['address']}")  # type: ignore[index]
        print(f"{metadata['name'] or 'unknown'} ({metadata['symbol'] or 'unknown'}); review: {report['review_status']}")
        print(f"upgradeability: {report['upgradeability']['status']}; liquidity: {report['liquidity']['status']}")  # type: ignore[index]
        print(f"transfer events: {report['distribution'].get('transfer_events', 0)}")  # type: ignore[index]
        for flag in report["review_flags"]:  # type: ignore[union-attr]
            print(f"flag: {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
