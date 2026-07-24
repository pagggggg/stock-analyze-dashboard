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
fail() { echo "[$(date '+%F %T')] ERROR: $*" | tee -a "$LOG" "$ERR" >&2; }

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

# git_sync <commit訊息> :把 data/ 狀態與報告 commit 回 main(帶 [skip ci])
git_sync() {
  git add data/ config/universe.yaml reports/screener_result.md reports/universe_report.md 2>/dev/null || true
  if git diff --cached --quiet; then
    log "無資料變動,略過 commit"
  else
    git commit -q -m "$1 $(date '+%F %H:%M') [skip ci]" || { fail "git commit 失敗"; return 1; }
    retry git pull --rebase -q origin main && retry git push -q origin main && log "已 push data 到 main" || fail "git push 失敗"
  fi
}

# deploy_ghpages :把 public/ 強推到 gh-pages(GitHub Pages 部署)
deploy_ghpages() {
  local tmp; tmp="$(mktemp -d)" || { fail "mktemp 失敗"; return 1; }
  cp -R "$ROOT/public/." "$tmp/" && touch "$tmp/.nojekyll"
  ( cd "$tmp" \
    && git init -q && git checkout -q -b gh-pages && git add -A \
    && git -c user.email="mac-mini@local" -c user.name="mac-mini" commit -q -m "deploy $(date '+%F %H:%M')" \
    && retry git push -f -q "https://github.com/pagggggg/stock-analyze-dashboard.git" gh-pages ) \
    && log "gh-pages 已部署" || fail "gh-pages 部署失敗"
  rm -rf "$tmp"
}
