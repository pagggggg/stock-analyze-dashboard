# 系統總覽 SYSTEM_OVERVIEW

> 網站:https://pagggggg.github.io/stock-analyze-dashboard/
>
> 本系統只用公開資料做估值與品質研究,不含持倉、成本或交易紀錄,不構成投資建議。
> 網站不是即時報價:表中價格是最近交易日收盤價,盤中不會跳動。

## 一、目前系統組成

本專案已從「台積電 EPS 試算器」擴充為五個區塊:

| 區塊 | 回答的問題 | 主要入口 |
| --- | --- | --- |
| 台積電法說 EPS 模型 | 官方營收、毛利率、匯率指引換算成悲觀/中性/樂觀 EPS 後,估值範圍是多少? | `main.py` |
| 可分析母體 | 哪些上市公司先符合市值、法說會、流動性等基本研究範圍? | `build_universe.py` |
| 全市場資料落地 | 如何把母體的財報、股價、估值與月營收保存成可重跑的本地資料? | `fetch_universe.py` |
| 兩層篩選器 | 哪些公司先排除明顯地雷,再通過營收、毛利率、ROE 品質條件? | `screen.py` |
| 多股網站與訊號 | 今天相較上次有哪些共識或 FCF 品質變化?估值位階目前如何? | `build_site.py` |
| AI 產業鏈全景圖 | 雲端 Capex 變化是否往晶片、代工、設備與下游供應鏈傳導? | `build_site.py` → `ai-chain.html` |

主要目錄:

```text
config/       人工假設、觀察清單、篩選門檻、母體清單
src/          資料層、計算、篩選、訊號與 HTML 產生器
data/         母體逐檔 JSON、共識歷史、訊號狀態、手動備援
cache/        API 原始快取(可重建,不進版控)
reports/      Markdown 報告
public/       GitHub Pages 靜態網站產物
scripts/      本機手動測試腳本(不寫遠端)
launchd/      已停用的舊本機排程
rule-backtest/                 一般 PE/基本面規則研究
cyclical-strategy-backtest/    循環股策略研究(獨立專案)
revision-momentum-backtest/    修正動能代理訊號研究(尚未完成正式報告)
```

## 二、目前資料流

```text
config/universe.yaml(母體唯一清單)
        │
        ▼
fetch_universe.py
  FinMind:財報/資產負債/現金流/月營收；TWSE/TPEx:盤後收盤與成交額
  yfinance:共識EPS/FCF/EV元件/美股
        │
        ▼
data/universe/<代號>.json
        │
        ├─ screen.py → reports/screener_result.md
        │
        └─ build_site.py → public/index.html / screener.html / stock_<id>.html
                               │
                               ▼
                         GitHub Pages
```

`data/universe/` 必須和 `config/universe.yaml` 完全一致:

- 缺少或多餘股票時,`screen.py` 中止,不再靜默用不一致資料篩選。
- JSON 無法解析時,整批中止並列出壞檔,不再靜默略過股票。
- 完整 `fetch_universe.py --from-universe` 執行會補齊母體美股並移除已退出母體的舊 JSON。

## 三、母體與篩選邏輯

目前母體由 `config/universe.yaml` 定義。母體建構的預設守門條件是:

1. 台股市值 > 300 億元。
2. 近一年有 MOPS 法人說明會。
3. 近 60 日平均成交額 > 1 億元。
4. 分析師覆蓋只記錄、不守門(`coverage_gates: false`),避免 yfinance 漏資料誤刪大型金融/傳產股。

第一層資格篩選(六條全過):

1. 上市滿 5 年(目前以最早財報代理,不是正式掛牌日)。
2. 近 5 年至少 4 年 EPS 為正。
3. 近 3 年累積 OCF 為正,且至少 2 年全年 OCF 為正。
4. 有息負債比低於產業門檻；金融股依設定排除。
5. 近 60 日平均成交額達標。
6. 最新財報距今不超過 200 天。

第二層品質篩選(標記,不淘汰):

7. 近 3 年營收 CAGR > 10%。
8. 近 3 年毛利率趨勢持平或上升。
9. 近 3 年平均 ROE > 15%。
10. 盈餘修正動能只標記,目前不納入「兩層全過」。

估值旗標只貼標籤、不淘汰:

- 歷史位階改採**同口徑**:目前 trailing PE 對個股近 5 年 trailing PE 分布。
- 綠旗:前瞻 PEG < 1 且目前 trailing PE < 歷史 trailing PE 中位。
- 紅旗:目前 trailing PE > 歷史 trailing P90,或前瞻 PEG > 2,或前瞻 PE > 60。
- forward PE/PEG 與 trailing 歷史位階分開顯示,不再把 forward PE 與 trailing 歷史分布混成同一口徑。
- forward PE 卡片僅顯示參考值,不再產生對 trailing 歷史河道的便宜/貴判讀或跨級事件。

## 四、估值與圖表口徑

### 四個估值指標

- 前瞻 PE = 收盤價 ÷ 今年共識 EPS。
- PEG = 前瞻 PE ÷ 共識 EPS 成長率。
- FCF Yield = 近四季自由現金流 ÷ 市值。
- EV/EBITDA = (市值 + 負債 - 現金) ÷ 近四季 EBITDA。

抓不到 FY 共識時,個股詳情可能退回 TTM 實際 EPS；此時數值本質是 trailing,不能解讀為真正 forward PE。

### 本益比河流圖

河道使用:

```text
當時可得的近四季實際 EPS × 近 5 年 trailing PE P10/P50/P90
```

- FinMind 無實際公告日欄位；本國發行人季報採法定申報期限作保守生效日 fallback（一般業 Q2 8/14、金融保險業 Q2 8/31），KY/外國發行人不套用此假設。
- 歷史期間與篩選器共用 `valuation_flag.pe_history_years`，目前為 5 年。
- 歷史每個月使用截至當月為止的 rolling 5 年 P10/P50/P90,不把今天才知道的分位套回過去。
- FinMind 只提供目前可取得的財報值,歷史序列屬「按可用日落後、使用最新重編值」,不是保存每次原始申報版本的嚴格 point-in-time 資料庫。
- 台股歷史 PE 採 FinMind basic EPS；美股採 Yahoo Reported EPS(調整後口徑)。兩者只和各股自身歷史比較,不跨口徑混算。
- 河道按月重算歷史分位；黑色股價線按每個交易日更新，紅點標示最新收盤。
- 河道保留真實歷史分位,**不再為了包住股價而向外擴張**。
- 股價可以高於 P90 或低於 P10；超出河道本身就是極端估值資訊,不是繪圖錯誤。

### 歷史 PEG 與月營收動能

- 歷史 PEG 使用實際 EPS CAGR,是回顧型資料,不等同前瞻 PEG。
- 月營收動能使用近三月平均 YoY 及其加速/減速,不依賴分析師覆蓋。
- 兩者都不能替代共識預估；循環股在景氣高點尤其可能被歷史成長誤導。

### AI 產業鏈全景圖

- 分類由 `config/ai_chain.yaml` 控制,目前 12 層、30 個節點。
- 母體內標的直接沿用同一次篩選結果；非母體美股/上櫃股只做 best-effort 快取,抓不到即標不納入。
- 四大雲端 Capex 來自 yfinance 季度現金流；口頭指引無法自動可靠解析,須人工填 config。
- 每筆人工指引需包含金額型態(約數/下限/區間/未揭露)、期間口徑、相對前次方向、來源與日期；跨公司比較只採日曆年資料。
- 美股免費季度資料通常只有約 5 季；只有前後兩期四家公司資料都齊全才算合計 YoY。
- 傳導分析需至少 8 組季度配對，並對 0–6 季 lag 的 permutation p 值做 Bonferroni 校正；不足時不宣稱落後期。
- 循環標記沿用近10年季度資料三取二定義；資料不足標未知。
- 供應鏈層級不代表股價連動或因果關係。
- 頁面最下方「產出側」追蹤企業實際 AI 付費/合約指標。最新值會和前期比較；已確認未揭露才計入連續未揭露,尚未輸入不冒充未揭露。
- 產出側的下限值與衍生值不參與精確加速度判定；Salesforce 保留原生財季與截止日；backlog 範圍改變會標口徑斷點。
- 產業鏈節點另有 17 檔台股與 16 檔美股收盤行情快照；台股在盤後更新，美股於台灣時間週二至週六早晨更新。行情與單日漲跌只供資訊，不納入基本面訊號。
- `config/thesis_2330.yaml` 定義台積電個人持有 thesis。條件 1 是回測驗證出場訊號；條件 2 僅有共識水準年減代理、逐季下修未直接回測；條件 3、4 為未回測人工補充門檻。資料不足顯示灰燈，不冒充未觸發。
- 美股設備標的 ASML、AMAT、LRCX、KLAC 的 `data/universe/*.json → detail.river` 保存可離線重建的月頻河道與日收盤黑線。口徑為 Yahoo split-adjusted Close 與發布後首個可交易收盤日可得的 Reported EPS；ASML 的 EUR EPS 依各交易日當時 EUR/USD 換成 USD 後再做歷史相對位階。
- 公司 Logo 為本地資產；官方圖示不可得時使用名稱縮寫標章。商標權利仍屬各公司。

## 五、資料不足與來源原則

### 來源綁定

台積電單股模型的核心假設採「數值 + source」綁定並列在報告中。
全市場 `data/universe/*.json` 尚未做到每欄來源與每區塊日期,只有整檔日期與 errors；因此不能宣稱全站每個數字都有欄位級來源。

### 資料不足

- 篩選條件缺資料標 `na/資料不足`,一律不算通過。
- JSON 損毀屬資料完整性錯誤,直接中止；不把股票靜默丟掉。
- 多股分析可局部顯示 N/A；核心財報完全失敗時不產生該股詳情頁。
- `fetch_universe.py` 若本次抓取缺區塊,會保留前次完整資料並標 `partial_update`。
- 單股 `main.py` 的 FinMind/TWSE 可退回手動 CSV；多股管線沒有等價的手動 fallback。

### 主要資料限制

- yfinance 對台股共識覆蓋不完整,尤其金融與傳產。
- 美股免費財報常只有 4–5 個完整年度。
- MOPS HTML 解析或年度覆蓋異常時會中止母體覆寫，保留上一版 manifest。
- 台股掛牌年數目前以最早財報代理。
- FinMind 免費帳號有每小時額度；系統用快取、TTL jitter、舊資料保護與 CI quality gate 降低風險。
- 網站是盤後資料,不是即時報價；個股頁可手動輸入價格重算估值。

## 六、自動化與單一 writer

### GitHub Actions(唯一 writer/deployer)

`.github/workflows/daily.yml` 是唯一可寫入以下位置的自動化:

- `data/`
- `reports/`
- 遠端 `main`
- `gh-pages`
- Release cache seed

排程:

- 週一至週五台灣 15:10（交易所收盤檔發布後）同步台股價格、重算指標並記錄訊號。
- 台股價格採全市場批次；FinMind 財報類資料每日輪替小批次補抓，yfinance 共識則每日輪詢全母體。
- 週二至週六台灣約 06:17 更新 16 檔美股行情、同步 7 檔美股母體 records，並更新 ASML、AMAT、LRCX、KLAC 河流圖；使用 `--no-record`，不重複寫入台股訊號。
- GitHub 排程可能延遲數分鐘至數十分鐘；頁面分開標示建置時間、台股行情時間與美股行情時間。
- Release cache seed 包含建站所需的台股財報、價格、共識與 AI 專用快取；美股早晨建站可離線沿用台股資料。
- 財報依 30–44 天 TTL 分散自然到期。
- push 只重建網站；手動 dispatch 可選 none/prices/all。

品質保護:

- 年度財報完整度須至少 90% 才 commit 資料。
- 台股全母體收盤價／日期必須出現在篩選頁；AI 台股節點另須逐檔與同批母體 record 完全一致。
- 首頁實際可連詳情頁須至少為母體資料檔的 70% 才部署。
- 品質不足時保留線上前一版,並不更新 cache seed。

### 本機 scripts / launchd

- daily、weekly、monthly launchd 全部停用。
- 本機 scripts 只供開發與人工測試,可產生 `public/` 與報告,但不 commit、不 push、不部署。
- 2026-08-01 曾因本機 monthly 與 GitHub Actions 同時寫資料造成大規模 rebase 衝突；在沒有跨環境鎖前不得重新啟用。

## 七、回測研究對主系統的限制

### rule-backtest

- 純 PE 會漏掉盈餘結構性崩壞,但「EPS/毛利率連兩季惡化」也常過早離場。
- 低 PE/低歷史百分位沒有展現穩定進場擇時價值；相對任意日進場多數落後。
- 「期望值 + 安全邊際」只有 14 次進場,不足以判斷有效性。
- 2008–2016 A 與買進持有接近；2017 後買進持有大勝,顯示時代依賴。
- 因此主系統估值旗標不可升級為自動買賣訊號。

### cyclical-strategy-backtest

- 海運左側策略假訊號率高；右側/月營收策略未在樣本內外同時勝過 0050。
- 月營收版樣本外年化 20.4% 接近 0050 的 20.5%,但最大回撤仍約 -74%。
- 結論只限於目前資料、規則與實作；不能外推成所有循環策略都無效。
- 主系統應繼續把循環股視為例外,不把低歷史 PEG 或月營收動能直接當買進分數。

## 八、日常使用

### 每天

看首頁狀態燈與訊號流水。股價漲跌本身不列為基本面訊號。

### 每週

看篩選器精華組、估值旗標與個股河流圖。綠旗是「優先研究」,不是買進訊號。

### 每季

更新 `config/assumptions.yaml` 的台積電法說指引,重跑 EPS 模型並檢查模型回測誤差。

**核心定位:**系統幫助縮小研究範圍、揭露口徑與過濾雜訊,不替使用者做交易決定。
