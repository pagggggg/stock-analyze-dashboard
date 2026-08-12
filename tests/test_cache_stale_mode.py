import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import cache


class CacheStaleModeTests(unittest.TestCase):
    def test_expired_cache_is_available_only_in_stale_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entry.json"
            path.write_text(json.dumps({"fetched_at": 1, "data": {"ok": True}}), encoding="utf-8")
            with patch("src.cache._path", return_value=path):
                self.assertIsNone(cache.cache_get("entry", ttl_seconds=1))
                with patch.dict(os.environ, {"ALLOW_STALE_CACHE": "1"}):
                    self.assertEqual(cache.cache_get("entry", ttl_seconds=1)["data"], {"ok": True})


if __name__ == "__main__":
    unittest.main()
