"""Synchronize persisted US closes without refetching statement blocks."""

from pathlib import Path

import yaml

from src.screener import load_config
from src.us_price_refresh import refresh_us_prices

ROOT = Path(__file__).resolve().parent


def main() -> None:
    screener_cfg = load_config(ROOT / "config/screener.yaml")
    chain_cfg = yaml.safe_load(
        (ROOT / "config/ai_chain.yaml").read_text(encoding="utf-8")) or {}
    result = refresh_us_prices(ROOT, screener_cfg, chain_cfg)
    print(
        "US price sync complete: "
        f"universe {result['universe_records']}, "
        f"AI cache {result['ai_cache_records']}, quotes {result['quotes']}; "
        f"close {result['close_date']}")


if __name__ == "__main__":
    main()
