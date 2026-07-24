# 本機排程(Mac mini / macOS launchd)

這套排程讓 **Mac mini 負責重活**(全市場抓取、完整篩選、部署),GitHub Actions 只在你 push 程式碼時輕量重建(見 `.github/workflows/daily.yml` 註解)。

## 分工

| 排程 | 時間 | 做什麼 | 腳本 |
| --- | --- | --- | --- |
| **每日** | 每天 14:30(盤後) | 只更新母體**股價 + yfinance**,重算隨股價變動指標(前瞻PE/PEG/FCF Yield/EV·EBITDA/估值旗標),不重抓財報 | `scripts/run_daily.sh` |
| **每週** | 週六 03:00 | 重抓母體全體**財報 + 共識**,重跑完整兩層篩選 | `scripts/run_weekly.sh` |
| **每月** | 1 號 04:00 | **重建可分析母體**(全上市逐檔:市值/覆蓋/法說會)→ 全量更新 | `scripts/run_monthly.sh` |

每支跑完都會:`git commit`(帶 `[skip ci]` 避免觸發 CI 重抓)→ push `main` → 部署 `gh-pages`。都有 log、失敗重試、error log(見 `logs/`)。

## 前置(只做一次)

1. **FinMind token**(每月全市場一定要):到 finmindtrade.com 免費註冊,把 token 寫進專案根目錄 `.env`:
   ```
   FINMIND_TOKEN=你的token
   ```
2. **git 推送權限**:Mac mini 需能 `git push`(你已用 `gh auth login`,git 憑證會存在 keychain,launchd 下也能用)。
3. 確認 `python3` 在 `/opt/homebrew/bin`(`scripts/_common.sh` 已把它加進 PATH)。

## 安裝(載入排程)

```bash
cd /Users/kaochihping/Stock_analyze
chmod +x scripts/*.sh
cp launchd/com.stockanalyze.*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stockanalyze.daily.plist
launchctl load ~/Library/LaunchAgents/com.stockanalyze.weekly.plist
launchctl load ~/Library/LaunchAgents/com.stockanalyze.monthly.plist
```

## 常用指令

```bash
launchctl list | grep stockanalyze          # 看有沒有載入
launchctl start com.stockanalyze.daily       # 立即手動跑一次(不等排程)
tail -f logs/daily.log                        # 看即時 log
cat logs/daily.error.log                      # 只看錯誤

# 改了 plist 要先 unload 再 load
launchctl unload ~/Library/LaunchAgents/com.stockanalyze.daily.plist
launchctl load   ~/Library/LaunchAgents/com.stockanalyze.daily.plist
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
# 沒問題後,再讓 launchctl start com.stockanalyze.monthly 或等排程自動跑
```

> Mac mini 記得到「系統設定 → 電池/節能」把「網路存取時喚醒」「防止自動睡眠」打開,排程才不會因睡眠錯過。
