from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from test_support import TEST_DATA  # noqa: F401
from backend.ai import AIProviderError, generate_week


class AIProviderTests(unittest.TestCase):
    def test_openai_compatible_request_parses_structured_week(self):
        variant = {
            "format": "Platform post",
            "body": "A practical security explanation with enough detail to be a useful, reviewable draft for a real team.",
            "caption": "A thoughtful call to action.",
        }
        ideas = [{
            "topic": f"Security idea {index}",
            "category": "APPLICATION SECURITY",
            "audience": "Product teams",
            "variants": {platform: dict(variant) for platform in ("instagram", "threads", "tiktok")},
        } for index in range(7)]
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps({"ideas": ideas})}}],
        }).encode()
        configured = SimpleNamespace(
            ai_api_key="test-server-only-key",
            ai_base_url="https://api.example.test/v1",
            ai_model="test-model",
        )
        with patch("backend.ai.settings", configured), patch("backend.ai.urllib.request.urlopen", return_value=response) as open_url:
            result = generate_week({"voice": "clear and educational"}, "2026-09-28", ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"])
        self.assertEqual(len(result), 7)
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example.test/v1/chat/completions")
        self.assertEqual(json.loads(request.data)["model"], "test-model")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-server-only-key")

    def test_missing_ai_key_fails_without_network_request(self):
        with patch("backend.ai.settings", SimpleNamespace(ai_api_key="")), patch("backend.ai.urllib.request.urlopen") as open_url:
            with self.assertRaises(AIProviderError):
                generate_week({}, "2026-09-28", [])
        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
