import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicDistributionBoundaryTests(unittest.TestCase):
    def test_sensitive_runtime_paths_are_ignored(self):
        ignored = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertTrue(
            {
                ".env",
                ".private/",
                "monitor.log",
                "monitor.log.*",
                "monitor-state.json",
                "monitor-state.*.json",
                "*.sqlite3*",
            }.issubset(ignored)
        )

    def test_public_python_does_not_reference_private_overlay(self):
        forbidden_markers = (
            ".private",
            "run_private",
            "notifications_extra",
            "http_client_extra",
            "wxpusher_orders",
            "PROXY_",
            "WXPUSHER_UIDS",
            "WXPUSHER_TOPIC_IDS",
            "WXPUSHER_ORDER_DB",
        )
        public_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(ROOT.glob("*.py"))
        )

        for marker in forbidden_markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, public_source)

    def test_public_env_example_excludes_private_features(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")

        for marker in (
            "PROXY_",
            "WXPUSHER_UIDS",
            "WXPUSHER_TOPIC_IDS",
            "WXPUSHER_CALLBACK_SECRET",
            "WXPUSHER_ORDER_DB",
            "FEISHU_",
            "DINGTALK_",
            "WECOM_",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, example)

    def test_public_candidate_files_contain_no_real_credentials(self):
        candidate_paths = [
            ROOT / ".env.example",
            ROOT / "README.md",
            ROOT / "monitor.py",
            ROOT / "notifications.py",
        ]
        candidate_text = "\n".join(
            path.read_text(encoding="utf-8") for path in candidate_paths
        )
        credential_patterns = {
            "WxPusher AppToken": r"\bAT_[A-Za-z0-9]{20,}\b",
            "WxPusher UID": r"\bUID_[A-Za-z0-9]{20,}\b",
            "ServerChan SendKey": r"\bSCT[A-Za-z0-9]{16,}\b",
            "Bark device key": r"https://api\.day\.app/[A-Za-z0-9]{20,}\b",
            "DataImpulse gateway": r"\bgw\.dataimpulse\.com\b",
        }

        for label, pattern in credential_patterns.items():
            with self.subTest(label=label):
                self.assertIsNone(re.search(pattern, candidate_text))
