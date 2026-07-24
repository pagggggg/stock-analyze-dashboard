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
git_sync "chore(data): 每週財報/共識/篩選更新"
deploy_ghpages
log "================ 每週更新完成 ================"
