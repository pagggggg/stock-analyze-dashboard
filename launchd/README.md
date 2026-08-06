# 本機排程(已停用；僅保留手動測試說明)

> **現況:daily / weekly / monthly 全部停用。** GitHub Actions 是唯一資料 writer 與部署者。
> 本機只能做開發或手動測試，`scripts/*.sh` 不會 commit、push 或部署。
>
> 2026-08-01 曾因 GitHub Actions 與本機 monthly 同時寫 `data/`、`main`、`gh-pages`，
> 造成 211 個合併衝突。除非先重新設計跨環境鎖，**不得重新載入這些 plist**。

## 分工

| 排程 | 時間 | 做什麼 | 腳本 |
| --- | --- | --- | --- |
| **每日** | 每天 14:30(盤後) | 只更新母體**股價 + yfinance**,重算隨股價變動指標(前瞻PE/PEG/FCF Yield/EV·EBITDA/估值旗標),不重抓財報 | `scripts/run_daily.sh` |
| **每週** | 週六 03:00 | 重抓母體全體**財報 + 共識**,重跑完整兩層篩選 | `scripts/run_weekly.sh` |
| **每月** | 1 號 04:00 | **重建可分析母體**(全上市逐檔:市值/覆蓋/法說會)→ 全量更新 | `scripts/run_monthly.sh` |

三支腳本目前都只更新本機 `data/`、`reports/`、`public/`,並寫 log；不 commit、不 push、不部署。

## 前置(只做一次)

1. **FinMind token**(每月全市場一定要):到 finmindtrade.com 免費註冊,把 token 寫進專案根目錄 `.env`:
   ```
   FINMIND_TOKEN=你的token
   ```
2. 確認 `python3` 在 `/opt/homebrew/bin`(`scripts/_common.sh` 已把它加進 PATH)。

## 不要安裝或載入排程

若舊機器曾載入，停用方式：

```bash
uid=$(id -u)
for j in daily weekly monthly; do
  launchctl bootout gui/$uid "$HOME/Library/LaunchAgents/com.stockanalyze.$j.plist" 2>/dev/null || true
  launchctl disable gui/$uid/com.stockanalyze.$j
done
```

## 常用指令

```bash
launchctl list | grep stockanalyze          # 看有沒有載入
launchctl print-disabled gui/$(id -u) | grep stockanalyze
bash scripts/run_daily.sh                    # 僅本機產出，絕不寫遠端
```

## 第一次全市場母體(建議手動先跑一次,確認順)

```bash
cd /Users/kaochihping/Stock_analyze
# 先確定 .env 有 FINMIND_TOKEN
python3 build_universe.py --market tw --full     # 全上市逐檔;首次較久(數十分~數小時)
python3 build_universe.py --market us            # 美股測試清單
python3 fetch_universe.py --from-universe --refresh all
python3 screen.py
python3 build_site.py --from-universe
# 本機結果只供檢查；正式資料與網站由 GitHub Actions 寫入
```

> 不要把本機排程當正式資料來源。需要母體重建時，先在本機測試，確認後以明確流程交由唯一 writer 寫入。
