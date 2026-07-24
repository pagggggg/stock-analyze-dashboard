#!/bin/bash
# 每月一次:重建『可分析母體』(市值、覆蓋家數、法說會狀態每月會變動),
# 再重抓母體全量、完整篩選、重建站。這是最重的一支(全上市逐檔 yfinance + MOPS)。
# ★ 需要 FINMIND_TOKEN(放 .env)才跑得順,否則匿名額度會很慢/被限流。
JOB="monthly"; source "$(cd "$(dirname "$0")" && pwd)/_common.sh"

pull_latest
log "================ 每月母體重建開始 ================"
if [ -z "$FINMIND_TOKEN" ]; then log "⚠ 未設 FINMIND_TOKEN,全市場抓取會很慢/易限流(見 .env)"; fi

# 1) 重建母體(台股全上市 + 美股測試)→ config/universe.yaml
retry python3 build_universe.py --market tw --full || { fail "build_universe tw --full 失敗"; exit 1; }
retry python3 build_universe.py --market us || log "build_universe us 有誤,續跑"
# 2) 抓母體全量財務(--refresh all)
retry python3 fetch_universe.py --from-universe --refresh all || { fail "fetch_universe 失敗"; exit 1; }
# 3) 完整篩選 + 重建站
retry python3 screen.py || { fail "screen 失敗"; exit 1; }
retry python3 build_site.py --from-universe || { fail "build_site 失敗"; exit 1; }
git_sync "chore(data): 每月母體重建 + 全量更新"
deploy_ghpages
log "================ 每月母體重建完成 ================"
