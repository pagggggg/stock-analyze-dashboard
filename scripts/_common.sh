#!/bin/bash
# ============================================================================
# 共用函式庫(scripts/*.sh 都 source 這支)
#   - 設定 PATH(launchd 環境很精簡,必須明確指定,否則找不到 python3/git)
#   - 載入 .env(FINMIND_TOKEN 等)
#   - log / 失敗重試 / git 同步(commit+push,帶 [skip ci] 避免觸發 CI 重抓)
#   - 部署 gh-pages
# ============================================================================

# Homebrew python3/git 在這;launchd 預設 PATH 很少,務必補上
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="zh_TW.UTF-8"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { echo "cannot cd $ROOT"; exit 1; }

LOGDIR="$ROOT/logs"; mkdir -p "$LOGDIR"
JOB="${JOB:-job}"
LOG="$LOGDIR/${JOB}.log"
ERR="$LOGDIR/${JOB}.error.log"
RETRY_MAX="${RETRY_MAX:-3}"

# 載入 .env(FINMIND_TOKEN 等),自動 export 給所有子 python
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

log()  { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
# 必須回傳非零。過去 fail() 只印訊息、仍回 0，造成 git push 失敗後腳本
# 繼續 force-push gh-pages，launchd 最後也錯誤顯示成功。
fail() { echo "[$(date '+%F %T')] ERROR: $*" | tee -a "$LOG" "$ERR" >&2; return 1; }

# pull_latest :跑之前先拉最新程式碼(你從別台 push 的變更也會生效)
pull_latest() {
  git pull --rebase -q origin main 2>/dev/null && log "已同步最新程式碼" \
    || log "git pull(code)失敗,改用本地版本續跑"
}

# retry <cmd...> :失敗重試 RETRY_MAX 次,間隔遞增
retry() {
  local n=0
  while true; do
    "$@" && return 0
    n=$((n+1))
    if [ "$n" -ge "$RETRY_MAX" ]; then fail "放棄(試 $RETRY_MAX 次仍失敗):$*"; return 1; fi
    log "重試 $n/$RETRY_MAX(等 $((30*n))s):$*"; sleep $((30*n))
  done
}

# GitHub Actions 是唯一資料 writer / 部署者。本機函式保留名稱，是為了讓舊腳本
# 若誤呼叫時明確失敗，而不是悄悄改遠端。要部署請手動觸發 GitHub Actions。
git_sync() {
  fail "本機禁止 git_sync：GitHub Actions 是唯一 writer。請檢查本地結果後手動觸發 workflow。"
}

# deploy_ghpages :把 public/ 強推到 gh-pages(GitHub Pages 部署)
deploy_ghpages() {
  fail "本機禁止 deploy_ghpages：GitHub Actions 是唯一部署者。"
}
