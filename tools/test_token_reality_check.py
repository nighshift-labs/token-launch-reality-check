#!/usr/bin/env python3
import json
import unittest

import token_reality_check


TOKEN = "0x" + "11" * 20
POOL = "0x" + "22" * 20
QUOTE = "0x" + "33" * 20
OWNER = "0x" + "44" * 20
V4_MANAGER = "0x" + "55" * 20
V4_POOL_ID = "0x" + "66" * 32
V4_HOOKS = "0x" + "77" * 20


def word(value: int) -> str:
    return f"{value:064x}"


def padded_address(address: str) -> str:
    return "0" * 24 + address[2:]


def dynamic_string(value: str) -> str:
    encoded = value.encode().hex()
    return "0x" + word(32) + word(len(value.encode())) + encoded.ljust(((len(encoded) + 63) // 64) * 64, "0")


def transfer_log(sender: str, recipient: str, amount: int, block: int) -> dict[str, object]:
    return {
        "address": TOKEN,
        "topics": [
            token_reality_check.TRANSFER_TOPIC,
            "0x" + padded_address(sender),
            "0x" + padded_address(recipient),
        ],
        "data": "0x" + word(amount),
        "blockNumber": hex(block),
        "transactionHash": "0x" + "55" * 32,
        "logIndex": "0x0",
    }


def v4_initialize_log(pool_id: str, currency0: str, currency1: str, block: int) -> dict[str, object]:
    return {
        "address": V4_MANAGER,
        "topics": [
            token_reality_check.V4_INITIALIZE_TOPIC,
            pool_id,
            "0x" + padded_address(currency0),
            "0x" + padded_address(currency1),
        ],
        "data": "0x" + "".join(
            (
                word(100),
                word(1),
                padded_address(V4_HOOKS),
                word(2**96),
                word(0),
            )
        ),
        "blockNumber": hex(block),
        "transactionHash": "0x" + "88" * 32,
        "logIndex": "0x0",
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return json.dumps(self.payload).encode()


class TokenRealityCheckTests(unittest.TestCase):
    def test_rejects_non_https_rpc_url_before_any_request(self):
        with self.assertRaisesRegex(ValueError, "rpc_url must be an HTTPS URL"):
            token_reality_check.build_report(
                {"rpc_url": "file:///etc/passwd", "token_address": TOKEN},
                opener=lambda *_: self.fail("opener must not be called"),
            )

    def test_builds_public_state_report_and_claim_checks(self):
        implementation = "0x" + "aa" * 20
        storage = {
            (TOKEN.lower(), token_reality_check.IMPLEMENTATION_SLOT): "0x" + "00" * 12 + implementation[2:],
            (TOKEN.lower(), token_reality_check.ADMIN_SLOT): "0x" + "00" * 32,
        }
        calls = []

        def opener(request, timeout):
            payload = json.loads(request.data)
            calls.append(payload)
            method = payload["method"]
            params = payload["params"]
            if method == "eth_chainId":
                result = "0x2105"
            elif method == "eth_getCode":
                result = "0x60016000"
            elif method == "eth_getStorageAt":
                result = storage[(params[0].lower(), params[1])]
            elif method == "eth_call":
                selector = params[0]["data"][:10]
                results = {
                    token_reality_check.SELECTORS["name"]: dynamic_string("Example"),
                    token_reality_check.SELECTORS["symbol"]: dynamic_string("EX"),
                    token_reality_check.SELECTORS["decimals"]: "0x" + word(18),
                    token_reality_check.SELECTORS["total_supply"]: "0x" + word(1000),
                    token_reality_check.SELECTORS["owner"]: "0x" + word(int(OWNER[2:], 16)),
                    token_reality_check.SELECTORS["minter"]: "0x" + word(int(OWNER[2:], 16)),
                    token_reality_check.SELECTORS["paused"]: "0x" + word(0),
                    token_reality_check.SELECTORS["token0"]: "0x" + word(int(TOKEN[2:], 16)),
                    token_reality_check.SELECTORS["token1"]: "0x" + word(int(QUOTE[2:], 16)),
                    token_reality_check.SELECTORS["liquidity"]: "0x" + word(500),
                }
                result = results[selector]
            elif method == "eth_getLogs":
                result = [
                    transfer_log("0x" + "00" * 20, OWNER, 400, 256),
                    transfer_log(OWNER, QUOTE, 100, 257),
                ]
            else:
                raise AssertionError(f"unexpected RPC method: {method}")
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": result})

        report = token_reality_check.build_report(
            {
                "rpc_url": "https://rpc.example",
                "token_address": TOKEN.upper().replace("0X", "0x"),
                "holder_scan": {"from_block": 256, "to_block": 257, "chunk_size": 100},
                "liquidity_pool": {"address": POOL, "expected_quote_address": QUOTE},
                "claims": {"chain_id": 8453, "address": TOKEN, "symbol": "EX", "decimals": 18},
            },
            opener=opener,
            captured_at="2026-08-08T00:00:00Z",
        )

        self.assertEqual(report["chain"]["chain_id"], 8453)
        self.assertEqual(report["token"]["metadata"]["symbol"], "EX")
        self.assertEqual(report["upgradeability"]["implementation"], implementation)
        self.assertEqual(report["permissions"]["minter"], OWNER.lower())
        self.assertEqual(report["claims"]["overall_status"], "match")
        self.assertEqual(report["liquidity"]["status"], "match")
        self.assertEqual(report["distribution"]["transfer_events"], 2)
        self.assertEqual(report["distribution"]["top_addresses"][0]["address"], OWNER.lower())
        self.assertEqual(report["distribution"]["top_addresses"][0]["share_of_positive_flow_pct"], 75.0)
        self.assertEqual([call["method"] for call in calls].count("eth_getLogs"), 1)

    def test_mismatch_claim_is_explicitly_flagged(self):
        result = token_reality_check.compare_claims(
            {"chain_id": 1, "address": "0x" + "99" * 20, "symbol": "WRONG", "decimals": 6},
            {"chain_id": 8453, "address": TOKEN, "symbol": "EX", "decimals": 18},
        )
        self.assertEqual(result["overall_status"], "mismatch")
        self.assertEqual({item["field"] for item in result["checks"]}, {"chain_id", "address", "symbol", "decimals"})
        self.assertTrue(all(item["status"] == "mismatch" for item in result["checks"]))

    def test_rejects_malformed_dynamic_string_instead_of_bytes32_fallback(self):
        malformed = "0x" + word(0) + word(4) + "61626364".ljust(64, "0")
        with self.assertRaisesRegex(ValueError, "malformed ABI string result"):
            token_reality_check.decode_text_result(malformed)

    def test_accepts_exact_bytes32_string_result(self):
        result = token_reality_check.decode_text_result("0x" + "4558".ljust(64, "0"))
        self.assertEqual(result, "EX")

    def test_rejects_malformed_transfer_log(self):
        with self.assertRaisesRegex(ValueError, "malformed Transfer log"):
            token_reality_check.decode_transfer_log(
                {"topics": [token_reality_check.TRANSFER_TOPIC], "data": "0x" + word(1)}
            )

    def test_rejects_transfer_log_with_extra_topics(self):
        log = transfer_log(OWNER, QUOTE, 1, 256)
        log["topics"].append("0x" + word(0))
        with self.assertRaisesRegex(ValueError, "malformed Transfer log"):
            token_reality_check.decode_transfer_log(log)

    def test_rejects_transfer_log_from_unrequested_contract(self):
        log = transfer_log(OWNER, QUOTE, 1, 256)
        log["address"] = "0x" + "99" * 20
        with self.assertRaisesRegex(ValueError, "Transfer log address did not match token"):
            token_reality_check.scan_transfer_distribution(
                "https://rpc.example",
                TOKEN,
                {"from_block": 256, "to_block": 256},
                opener=lambda request, timeout: FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": [log],
                    }
                ),
            )

    def test_rejects_transfer_log_outside_requested_block_range(self):
        log = transfer_log(OWNER, QUOTE, 1, 257)
        with self.assertRaisesRegex(ValueError, "Transfer log block number fell outside requested range"):
            token_reality_check.scan_transfer_distribution(
                "https://rpc.example",
                TOKEN,
                {"from_block": 256, "to_block": 256},
                opener=lambda request, timeout: FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": [log],
                    }
                ),
            )

    def test_rejects_non_boolean_complete_history_claim(self):
        with self.assertRaisesRegex(ValueError, "holder_scan.complete must be boolean"):
            token_reality_check.scan_transfer_distribution(
                "https://rpc.example",
                TOKEN,
                {"from_block": 256, "to_block": 256, "complete": "false"},
                opener=lambda request, timeout: FakeResponse(
                    {"jsonrpc": "2.0", "id": 1, "result": []}
                ),
            )

    def test_rejects_noncanonical_address_words_across_decoders(self):
        malformed = "0x" + "01" * 12 + OWNER[2:]

        with self.assertRaisesRegex(ValueError, "malformed address result"):
            token_reality_check.decode_address_result(malformed)
        with self.assertRaisesRegex(ValueError, "malformed storage word"):
            token_reality_check.storage_address(malformed)

        transfer = transfer_log(OWNER, QUOTE, 1, 256)
        transfer["topics"][1] = malformed
        with self.assertRaisesRegex(ValueError, "malformed Transfer log"):
            token_reality_check.decode_transfer_log(transfer)

        v4 = v4_initialize_log(V4_POOL_ID, TOKEN, QUOTE, 512)
        v4["topics"][2] = malformed
        with self.assertRaisesRegex(ValueError, "malformed Uniswap v4 Initialize currency topics"):
            token_reality_check.decode_v4_initialize_log(v4, V4_POOL_ID)

    def test_checks_uniswap_v4_pool_id_from_initialize_event(self):
        calls = []

        def opener(request, timeout):
            payload = json.loads(request.data)
            calls.append(payload)
            method = payload["method"]
            params = payload["params"]
            if method == "eth_chainId":
                result = "0x2105"
            elif method == "eth_getCode":
                result = "0x60016000"
            elif method == "eth_getStorageAt":
                result = "0x" + "00" * 32
            elif method == "eth_call":
                selector = params[0]["data"][:10]
                results = {
                    token_reality_check.SELECTORS["name"]: dynamic_string("Example"),
                    token_reality_check.SELECTORS["symbol"]: dynamic_string("EX"),
                    token_reality_check.SELECTORS["decimals"]: "0x" + word(18),
                    token_reality_check.SELECTORS["total_supply"]: "0x" + word(1000),
                }
                result = results.get(selector, "0x")
            elif method == "eth_getLogs":
                log_filter = params[0]
                self.assertEqual(log_filter["address"], V4_MANAGER)
                self.assertEqual(log_filter["topics"], [token_reality_check.V4_INITIALIZE_TOPIC, V4_POOL_ID])
                result = [v4_initialize_log(V4_POOL_ID, TOKEN, QUOTE, 512)]
            else:
                raise AssertionError(f"unexpected RPC method: {method}")
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": result})

        report = token_reality_check.build_report(
            {
                "rpc_url": "https://rpc.example",
                "token_address": TOKEN,
                "liquidity_pool": {
                    "type": "uniswap-v4",
                    "manager_address": V4_MANAGER,
                    "pool_id": V4_POOL_ID,
                    "initialize_scan": {"from_block": 512, "to_block": 512, "chunk_size": 100},
                    "expected_quote_address": QUOTE,
                },
            },
            opener=opener,
            captured_at="2026-08-08T00:00:00Z",
        )

        self.assertEqual(report["liquidity"]["status"], "match")
        self.assertEqual(report["liquidity"]["pool_id"], V4_POOL_ID)
        self.assertEqual(report["liquidity"]["currency0"], TOKEN)
        self.assertEqual(report["liquidity"]["currency1"], QUOTE)
        self.assertEqual(report["liquidity"]["initialize_block"], 512)
        self.assertEqual(report["liquidity"]["live_state"], "not_checked")
        self.assertEqual([call["method"] for call in calls].count("eth_getLogs"), 1)

    def test_rejects_malformed_uniswap_v4_initialize_log(self):
        log = v4_initialize_log(V4_POOL_ID, TOKEN, QUOTE, 512)
        log["topics"][0] = None
        with self.assertRaisesRegex(ValueError, "malformed Uniswap v4 Initialize log"):
            token_reality_check.decode_v4_initialize_log(log, V4_POOL_ID)

    def test_rejects_uniswap_v4_initialize_log_with_extra_topics(self):
        log = v4_initialize_log(V4_POOL_ID, TOKEN, QUOTE, 512)
        log["topics"].append("0x" + word(0))
        with self.assertRaisesRegex(ValueError, "malformed Uniswap v4 Initialize log"):
            token_reality_check.decode_v4_initialize_log(log, V4_POOL_ID)

    def test_rejects_uniswap_v4_log_from_unrequested_manager(self):
        log = v4_initialize_log(V4_POOL_ID, TOKEN, QUOTE, 512)
        log["address"] = "0x" + "99" * 20
        with self.assertRaisesRegex(ValueError, "Initialize log address did not match manager"):
            token_reality_check.scan_v4_pool(
                "https://rpc.example",
                TOKEN,
                {
                    "type": "uniswap-v4",
                    "manager_address": V4_MANAGER,
                    "pool_id": V4_POOL_ID,
                    "initialize_scan": {"from_block": 512, "to_block": 512},
                },
                opener=lambda request, timeout: FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": [log],
                    }
                ),
            )

    def test_rejects_uniswap_v4_log_outside_requested_block_range(self):
        log = v4_initialize_log(V4_POOL_ID, TOKEN, QUOTE, 513)
        with self.assertRaisesRegex(ValueError, "Initialize log block number fell outside requested range"):
            token_reality_check.scan_v4_pool(
                "https://rpc.example",
                TOKEN,
                {
                    "type": "uniswap-v4",
                    "manager_address": V4_MANAGER,
                    "pool_id": V4_POOL_ID,
                    "initialize_scan": {"from_block": 512, "to_block": 512},
                },
                opener=lambda request, timeout: FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": [log],
                    }
                ),
            )

    def test_pins_state_reads_and_open_ended_scans_to_observation_block(self):
        calls = []

        def opener(request, timeout):
            payload = json.loads(request.data)
            calls.append(payload)
            method = payload["method"]
            params = payload["params"]
            if method == "eth_chainId":
                result = "0x2105"
            elif method == "eth_getBlockByNumber":
                self.assertEqual(params, ["0x200", False])
                result = {
                    "number": "0x200",
                    "hash": "0x" + "99" * 32,
                    "timestamp": "0x123456",
                }
            elif method == "eth_getCode":
                self.assertEqual(params, [TOKEN, "0x200"])
                result = "0x60016000"
            elif method == "eth_getStorageAt":
                self.assertEqual(params[2], "0x200")
                result = "0x" + "00" * 32
            elif method == "eth_call":
                self.assertEqual(params[1], "0x200")
                selector = params[0]["data"][:10]
                results = {
                    token_reality_check.SELECTORS["name"]: dynamic_string("Example"),
                    token_reality_check.SELECTORS["symbol"]: dynamic_string("EX"),
                    token_reality_check.SELECTORS["decimals"]: "0x" + word(18),
                    token_reality_check.SELECTORS["total_supply"]: "0x" + word(1000),
                }
                result = results.get(selector, "0x")
            elif method == "eth_getLogs":
                self.assertEqual(params[0]["fromBlock"], "0x200")
                self.assertEqual(params[0]["toBlock"], "0x200")
                result = []
            else:
                raise AssertionError(f"unexpected RPC method: {method}")
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": result})

        report = token_reality_check.build_report(
            {
                "rpc_url": "https://rpc.example",
                "token_address": TOKEN,
                "observation_block": 512,
                "holder_scan": {"from_block": 512, "chunk_size": 100},
            },
            opener=opener,
            captured_at="2026-08-08T00:00:00Z",
        )

        self.assertEqual(
            report["observation"],
            {
                "mode": "pinned",
                "block_number": 512,
                "block_tag": "0x200",
                "block_hash": "0x" + "99" * 32,
                "timestamp": 0x123456,
            },
        )
        self.assertEqual(report["distribution"]["to_block"], 512)
        self.assertEqual([call["method"] for call in calls].count("eth_getBlockByNumber"), 1)


    def test_detects_eip1167_minimal_proxy(self):
        impl = "0x" + "ab" * 20
        code = "0x" + "363d3d373d3d3d363d73" + impl[2:] + "5af43d82803e903d91602b57fd5bf3"
        self.assertEqual(
            token_reality_check.detect_minimal_proxy(code),
            {"pattern": "eip-1167", "implementation": impl},
        )

    def test_detects_solady_minimal_proxy(self):
        impl = "0x" + "cd" * 20
        code = "0x" + "3d3d3d3d363d3d37363d73" + impl[2:] + "5af43d3d93803e602a57fd5bf3"
        self.assertEqual(
            token_reality_check.detect_minimal_proxy(code),
            {"pattern": "solady-minimal-proxy", "implementation": impl},
        )

    def test_minimal_proxy_detection_ignores_plain_contract(self):
        self.assertIsNone(token_reality_check.detect_minimal_proxy("0x60016000"))
        self.assertIsNone(token_reality_check.detect_minimal_proxy("0x"))

    def test_minimal_proxy_reported_in_upgradeability_and_review(self):
        impl = "0x" + "cd" * 20
        proxy_code = "0x" + "3d3d3d3d363d3d37363d73" + impl[2:] + "5af43d3d93803e602a57fd5bf3"

        def opener(request, timeout):
            payload = json.loads(request.data)
            method = payload["method"]
            params = payload["params"]
            if method == "eth_chainId":
                result = "0x2105"
            elif method == "eth_getCode":
                result = proxy_code
            elif method == "eth_getStorageAt":
                result = "0x" + "00" * 32
            elif method == "eth_call":
                selector = params[0]["data"][:10]
                results = {
                    token_reality_check.SELECTORS["name"]: dynamic_string("ProxyCoin"),
                    token_reality_check.SELECTORS["symbol"]: dynamic_string("PXY"),
                    token_reality_check.SELECTORS["decimals"]: "0x" + word(18),
                    token_reality_check.SELECTORS["total_supply"]: "0x" + word(1000),
                    token_reality_check.SELECTORS["owner"]: "0x" + word(int(OWNER[2:], 16)),
                }
                result = results.get(selector, "0x")
            elif method == "eth_getLogs":
                result = []
            else:
                raise AssertionError(f"unexpected RPC method: {method}")
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": result})

        report = token_reality_check.build_report(
            {
                "rpc_url": "https://rpc.example",
                "token_address": TOKEN,
                "holder_scan": {"from_block": 256, "to_block": 256},
            },
            opener=opener,
            captured_at="2026-08-08T00:00:00Z",
        )

        self.assertEqual(report["upgradeability"]["status"], "minimal_proxy_observed")
        self.assertEqual(report["upgradeability"]["minimal_proxy"]["implementation"], impl)
        self.assertTrue(any("delegatecall proxy" in flag for flag in report["review_flags"]))
        self.assertEqual(report["review_status"], "manual_review_required")


    def test_flags_high_bounded_flow_concentration(self):
        def opener(request, timeout):
            payload = json.loads(request.data)
            method = payload["method"]
            params = payload["params"]
            if method == "eth_chainId":
                result = "0x2105"
            elif method == "eth_getCode":
                result = "0x60016000"
            elif method == "eth_getStorageAt":
                result = "0x" + "00" * 32
            elif method == "eth_call":
                selector = params[0]["data"][:10]
                results = {
                    token_reality_check.SELECTORS["name"]: dynamic_string("Concentrated"),
                    token_reality_check.SELECTORS["symbol"]: dynamic_string("CC"),
                    token_reality_check.SELECTORS["decimals"]: "0x" + word(18),
                    token_reality_check.SELECTORS["total_supply"]: "0x" + word(1000),
                    token_reality_check.SELECTORS["owner"]: "0x" + word(0),
                    token_reality_check.SELECTORS["minter"]: "0x" + word(0),
                    token_reality_check.SELECTORS["paused"]: "0x" + word(0),
                }
                result = results.get(selector, "0x")
            elif method == "eth_getLogs":
                result = [transfer_log("0x" + "00" * 20, OWNER, 100, 256 + i) for i in range(5)]
            else:
                raise AssertionError(f"unexpected RPC method: {method}")
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": result})

        report = token_reality_check.build_report(
            {
                "rpc_url": "https://rpc.example",
                "token_address": TOKEN,
                "holder_scan": {"from_block": 256, "to_block": 260, "chunk_size": 100},
            },
            opener=opener,
            captured_at="2026-08-08T00:00:00Z",
        )

        self.assertEqual(report["review_status"], "manual_review_required")
        self.assertTrue(any("concentrated in a single address" in flag for flag in report["review_flags"]))
        self.assertTrue(all("%%" not in flag for flag in report["review_flags"]))

    def test_does_not_flag_low_bounded_flow_concentration(self):
        def opener(request, timeout):
            payload = json.loads(request.data)
            method = payload["method"]
            params = payload["params"]
            if method == "eth_chainId":
                result = "0x2105"
            elif method == "eth_getCode":
                result = "0x60016000"
            elif method == "eth_getStorageAt":
                result = "0x" + "00" * 32
            elif method == "eth_call":
                selector = params[0]["data"][:10]
                results = {
                    token_reality_check.SELECTORS["name"]: dynamic_string("Example"),
                    token_reality_check.SELECTORS["symbol"]: dynamic_string("EX"),
                    token_reality_check.SELECTORS["decimals"]: "0x" + word(18),
                    token_reality_check.SELECTORS["total_supply"]: "0x" + word(1000),
                    token_reality_check.SELECTORS["owner"]: "0x" + word(0),
                    token_reality_check.SELECTORS["minter"]: "0x" + word(0),
                    token_reality_check.SELECTORS["paused"]: "0x" + word(0),
                }
                result = results.get(selector, "0x")
            elif method == "eth_getLogs":
                result = [
                    transfer_log("0x" + "00" * 20, OWNER, 400, 256),
                    transfer_log(OWNER, QUOTE, 100, 257),
                ]
            else:
                raise AssertionError(f"unexpected RPC method: {method}")
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": result})

        report = token_reality_check.build_report(
            {
                "rpc_url": "https://rpc.example",
                "token_address": TOKEN,
                "holder_scan": {"from_block": 256, "to_block": 257, "chunk_size": 100},
            },
            opener=opener,
            captured_at="2026-08-08T00:00:00Z",
        )

        self.assertEqual(report["review_status"], "no_observed_mismatch")
        self.assertEqual(report["distribution"]["top_addresses"][0]["share_of_positive_flow_pct"], 75.0)


if __name__ == "__main__":
    unittest.main()
