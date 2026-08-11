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
[ -r /tmp/batches.txt ] || { fail "找不到 /tmp/batches.txt"; exit 1; }

validate_batch() {
  python3 - "$1" <<'PY'
import sys
import yaml

ids = [x.strip() for x in sys.argv[1].split(",") if x.strip()]
doc = yaml.safe_load(open("config/universe.yaml", encoding="utf-8")) or {}
expected = {str(x["stock_id"]) for market in ("twse", "us") for x in (doc.get(market) or [])}
unknown = sorted(set(ids) - expected)
if not ids or len(ids) != len(set(ids)) or unknown:
    raise SystemExit(f"批次代號無效或重複:{unknown or ids}")
PY
}

n=0; batch_failed=0
while IFS= read -r ids; do
  [ -z "$ids" ] && continue
  validate_batch "$ids" || { fail "批次清單驗證失敗:$ids"; exit 1; }
  n=$((n+1))
  cnt=$(echo "$ids" | tr ',' '\n' | wc -l | tr -d ' ')
  log "批次 $n:$cnt 檔 → $ids"
  # 每批獨立 process(跑完釋放連線);不帶 --refresh → 已有的自動略過
  retry python3 fetch_universe.py --from-universe --stock-ids "$ids" \
    || { log "! 批次 $n 有失敗,續跑下一批"; batch_failed=1; }
  have=$(ls data/universe/*.json 2>/dev/null | wc -l | tr -d ' ')
  log "批次 $n 完成;目前本地資料 $have 檔"
  sleep "$BATCH_SLEEP"
done < /tmp/batches.txt
[ "$n" -gt 0 ] || { fail "批次清單為空"; exit 1; }
[ "$batch_failed" -eq 0 ] || { fail "有批次抓取失敗，請修復後再續跑"; exit 1; }

have=$(ls data/universe/*.json 2>/dev/null | wc -l | tr -d ' ')
log "抓取階段結束,本地資料 $have 檔 → 進入篩選/建站"
retry python3 update_ai_quotes.py || { fail "AI 美股行情更新失敗"; exit 1; }

retry python3 screen.py || { fail "screen 失敗"; exit 1; }
retry python3 build_site.py --from-universe || { fail "build_site 失敗"; exit 1; }
log "本機資料與網站已補完，未寫入遠端；GitHub Actions 是唯一 writer/deployer。"
log "================ 續跑全量完成 ================"
