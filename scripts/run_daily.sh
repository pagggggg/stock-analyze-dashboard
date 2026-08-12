#!/bin/bash
# 每日盤後:只更新母體『股價 + yfinance(市值/共識)』,重算隨股價變動的指標
# (前瞻PE、PEG、FCF Yield、EV/EBITDA、估值旗標),不重抓財報。
JOB="daily"; source "$(cd "$(dirname "$0")" && pwd)/_common.sh"

pull_latest
log "================ 每日更新開始 ================"
# 1) 只重抓股價 + yfinance(--refresh prices);財報沿用快取
retry python3 fetch_universe.py --from-universe --refresh prices --skip-us || { fail "fetch prices 失敗"; exit 1; }
retry python3 update_ai_tw_quotes.py || { fail "AI 台股行情更新失敗"; exit 1; }
# 2) 重跑篩選(估值旗標會隨新股價/PEG 更新)
retry python3 screen.py || log "screen 有誤,續跑"
# 3) 重建儀表板(掃描總表四指標、詳情頁、狀態燈/訊號流水;訊號仍只看訊號級變化)
retry python3 build_site.py --from-universe || { fail "build_site 失敗"; exit 1; }
# 本機只產生結果供人工檢查，不 commit、不 push、不部署。
log "本機結果已更新(public/ + reports/)，未寫入遠端；GitHub Actions 是唯一 writer/deployer。"
log "================ 每日本機測試完成 ================"
