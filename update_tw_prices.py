"""同步更新台股母體收盤／估值與 AI 產業鏈台股行情。"""

from pathlib import Path

import yaml

from src.screener import load_config
from src.tw_price_refresh import refresh_tw_prices


ROOT = Path(__file__).resolve().parent


def main() -> None:
    screener_cfg = load_config(ROOT / "config/screener.yaml")
    chain_cfg = yaml.safe_load((ROOT / "config/ai_chain.yaml").read_text(encoding="utf-8")) or {}
    result = refresh_tw_prices(ROOT, screener_cfg, chain_cfg)
    if not result["updated"]:
        print("交易所沒有比目前快照更新的收盤資料，本次不改寫行情時間。")
    print(f"台股價格同步完成:母體 {result['records']} 檔、AI 行情 {result['quotes']} 檔")
    print(f"損益表補抓:{result['income_updates']}/{result['income_refresh_attempts']} 檔成功")
    print(f"收盤日期:{result['close_dates']}")


if __name__ == "__main__":
    main()
