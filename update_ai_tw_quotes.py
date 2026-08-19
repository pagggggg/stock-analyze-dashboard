"""舊入口：改由單一同步器更新母體與 AI 台股行情。"""

from update_tw_prices import main as update_all_tw_prices


def main() -> None:
    update_all_tw_prices()


if __name__ == "__main__":
    main()
