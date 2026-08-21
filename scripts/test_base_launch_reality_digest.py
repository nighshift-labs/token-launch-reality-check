import unittest
from unittest.mock import patch

import base_launch_reality_digest as digest


class BaseLaunchRealityDigestTests(unittest.TestCase):
    @patch.object(digest.trc, "build_report", return_value={"ok": True})
    def test_run_check_pins_observation_block(self, build_report):
        token = {"address": "0x" + "11" * 20, "symbol": "TEST", "decimals": 18}

        result = digest.run_check(token, 123456)

        self.assertEqual(result, {"ok": True})
        manifest = build_report.call_args.args[0]
        self.assertEqual(manifest["observation_block"], 123456)


if __name__ == "__main__":
    unittest.main()