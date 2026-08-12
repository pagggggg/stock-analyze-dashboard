"""更新 AI 產業鏈 17 檔台股最近收盤行情。"""

from pathlib import Path

import yaml

from src.tw_quotes import expected_tw_quote_tickers, update_tw_quote_snapshot

ROOT = Path(__file__).resolve().parent


def main() -> None:
    config_path = ROOT / "config/ai_chain.yaml"
    output_path = ROOT / "data/ai_chain_tw_quotes.json"
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    snapshot, warnings = update_tw_quote_snapshot(cfg, output_path)
    print(f"AI 台股行情:{len(snapshot['quotes'])}/{len(expected_tw_quote_tickers(cfg))} 檔")
    for warning in warnings:
        print(f"! {warning}")
    print(f"已寫入:{output_path}")


if __name__ == "__main__":
    main()
