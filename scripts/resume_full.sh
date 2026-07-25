#!/bin/bash
# ============================================================================
# 續跑全量(resume_full.sh)—— 一次性補完中斷的每週全量更新
# ----------------------------------------------------------------------------
# 為什麼要分批:FinMind 套件每次呼叫都 login_by_token 且不關連線,
# 單一長時間 process 會累積上百條 ESTABLISHED 連線 → 最後卡死。
# 對策:每批 20 檔開一個獨立 process,跑完就退出釋放連線;批間 sleep 節流。
#
# 不帶 --refresh:已抓過(data/universe/*.json 夠新)的自動略過,只補沒抓到的。
# ============================================================================
JOB="resume"; source "$(cd "$(dirname "$0")" && pwd)/_common.sh"

BATCH_SLEEP="${BATCH_SLEEP:-45}"     # 批與批之間休息(秒),避開 FinMind 每小時上限

log "================ 續跑全量開始 ================"

n=0
while IFS= read -r ids; do
  [ -z "$ids" ] && continue
  n=$((n+1))
  cnt=$(echo "$ids" | tr ',' '\n' | wc -l | tr -d ' ')
  log "批次 $n:$cnt 檔 → $ids"
  # 每批獨立 process(跑完釋放連線);不帶 --refresh → 已有的自動略過
  retry python3 fetch_universe.py --from-universe --stock-ids "$ids" \
    || log "! 批次 $n 有失敗,續跑下一批(缺的股票會標資料不足,不冒充)"
  have=$(ls data/universe/*.json 2>/dev/null | wc -l | tr -d ' ')
  log "批次 $n 完成;目前本地資料 $have 檔"
  sleep "$BATCH_SLEEP"
done < /tmp/batches.txt

have=$(ls data/universe/*.json 2>/dev/null | wc -l | tr -d ' ')
log "抓取階段結束,本地資料 $have 檔 → 進入篩選/建站"

retry python3 screen.py || { fail "screen 失敗"; exit 1; }
retry python3 build_site.py --from-universe || { fail "build_site 失敗"; exit 1; }
git_sync "chore(data): 全量母體(239)財報/共識/篩選更新"
deploy_ghpages
log "================ 續跑全量完成 ================"
