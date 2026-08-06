#!/bin/bash
# 每週一次:重抓母體全體『財報 + 共識』,重跑完整兩層篩選。
JOB="weekly"; source "$(cd "$(dirname "$0")" && pwd)/_common.sh"

pull_latest
log "================ 每週更新開始 ================"
# 1) 連財報一起重抓(--refresh all):財報/資產負債/現金流 + 股價 + yfinance
retry python3 fetch_universe.py --from-universe --refresh all || { fail "fetch all 失敗"; exit 1; }
# 2) 完整兩層篩選
retry python3 screen.py || { fail "screen 失敗"; exit 1; }
# 3) 重建儀表板
retry python3 build_site.py --from-universe || { fail "build_site 失敗"; exit 1; }
log "本機結果已更新(public/ + reports/)，未寫入遠端；GitHub Actions 是唯一 writer/deployer。"
log "================ 每週本機測試完成 ================"
