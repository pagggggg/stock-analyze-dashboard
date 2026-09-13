# cyclical-strategy-backtest

驗證「景氣循環股能否用**系統化訊號**操作」。分階段做,**階段一(海運貨櫃三雄)先做;
失敗就停止整個專案**。國巨(2327)為構想來源、存在倖存者偏差 → 不在階段一樣本,
不計入有效性判定。

## 目錄
```
config.yaml          所有門檻/分類/各組參數/日期切割/指標來源(改動須登記 changelog)
cache.py             檔案快取(data/raw/*.json)
sources_finmind.py   FinMind:日收盤(分割/減資回溯調整)、季財報、每日 PBR、月營收
sources_freight.py   investing.com 週線運價指數:貨櫃(#940796)、BDI(#940793)
util.py              設定載入、財報可用日(法定申報期限 → 防前視偏誤)、百分位
cyclical_def.py      循環股量化定義(三取二),輸出各項數值供人工複核
features.py          點時特徵(以運價週線為決策格點,嚴格用「當下可得」資料)
features_monthly.py  月營收「加總」YoY 訊號(可用日=次月10日)
backtest.py          事件驅動回測:分批進出場、逐筆交易明細、每日權益曲線
backtest_monthly.py  月營收版回測(執行語意與 backtest.py 一致,只換訊號來源)
metrics.py           指標(假訊號率/年化/MDD/勝率/盈虧比/空手%)+ 對照組
run.py               階段一主程式 → reports/phase1_report.md、phase1_trades.csv
run_monthly.py       階段一補充測試 → reports/phase1_monthly_supplement.md
fetch_data.py        先抓齊並印出各資料集筆數/日期範圍(確認資料可得性)
sources_sec.py       SEC EDGAR XBRL/6-K:Capex 與美股季度營收
capex_transmission.py Capex 傳導研究的固定成員加總與統計檢定
run_capex_transmission.py 獨立 Capex 傳導研究(不含交易策略)
```

## 執行
```bash
python3 fetch_data.py   # 確認資料可得性與長度(全走快取,可重跑)
python3 cyclical_def.py # 循環股量化定義表
python3 run.py          # 階段一完整報告 + 逐筆交易 CSV
python3 run_monthly.py  # 階段一補充測試(月營收版)
python3 run_capex_transmission.py # 正式 Capex 傳導研究(99,999 次 bootstrap)
```
需 `.env` 內 `FINMIND_TOKEN`(專案根目錄已有)。相依:FinMind、requests、pyyaml。

## 獨立研究:Capex 傳導分析
此研究與已結案的循環股交易回測獨立,第一階段只檢驗資料與相關性,不建立交易策略。
事前規格見 `capex_transmission_config.yaml`;安裝相依可執行
`pip install -r requirements.txt`。

產出:
- `reports/capex_history.csv`:四大雲端業者逐季 Capex、來源、申報日與缺值。
- `reports/capex_transmission_revenue.csv`:固定成員、固定匯率的營收加總稽核。
- `reports/capex_transmission_lags.csv`:原始、分段、去趨勢的完整 lag 明細。
- `reports/capex_transmission.md`:正式研究報告與四項成功標準判定。

正式結果截至 2026Q1:Amazon 僅自 2017Q3 起有可維持一致定義的季度
`PaymentsToAcquireProductiveAssets`,四大 Capex 合計 YoY 的有效配對最多只有 29 季,
低於事前推論門檻 32 季。所有層均只能作描述,沒有任何層可宣稱顯著傳導。
結論為「傳導關係不穩定,不進入策略設計階段」。

## 資料可得性(已實測)
| 資料 | 來源 | 範圍 | 備註 |
|---|---|---|---|
| 三雄+對照 日收盤 | FinMind | 2009→2026 | 未還原;已回溯調整分割/減資;現金股利未還原(報酬保守) |
| 季財報(毛利率/EPS/營收) | FinMind | 2009Q1→2026Q1 | 毛利率=GrossProfit/Revenue |
| 每日 PBR | FinMind | 2010→2026 | 左側 P/B 百分位用 |
| **BDI** | investing.com #940793 | 2009→2026 | 完整;交叉檢核 |
| **貨櫃運價指數** | investing.com #940796 | 2009→**2025-03** | 主訊號;**樣本外最後16個月缺**,已標示不硬湊 |

SCFI/BDI 免費**歷史**在 StockQ 只有近期快照、MacroMicro 需登入 → 均不足;
最終採 investing.com 歷史 API(唯一免 Cloudflare 的端點)。

## 成功標準(四項全過才算可行;任一不過即不可行,**不得調參補救**)
1. 假訊號率 < 40%  2. 樣本外不崩  3. 年化勝 0050 且 MDD 可接受  4. 每組訊號 ≥ 5

## 防過擬合
每組參數 ≤ 4、各組獨立不共用;不做參數搜尋、不事後調整;
門檻寫進 config、改動登記 changelog;樣本內定參、樣本外只驗證。

## 階段一結論(摘要,詳見 reports/phase1_report.md)
- **左側(供給面)**:樣本內/外假訊號率 66.7% / 75% > 50% → **不可行**
  (深跌+低估的進場點,基本面常仍在惡化 → 買在下跌刀口)。
- **右側(需求面)**:假訊號率 0%,但**樣本內(-4.0%)與樣本外(13.4%)年化皆輸 0050**
  (同期 4.8% / 20.5%),且 MDD -60%~-76%。**未通過**。
- 台積電(非循環對照)在右側樣本外也有 10.5% 年化、0% 假訊號 → 訊號抓到的偏向
  大盤 beta/動能,而非循環特性。
- BDI 交叉檢核(完整覆蓋)結論一致 → 對運價來源選擇穩健。

**→ 四項標準無任一哲學全過。依專案前提,階段一判定「不可行」,不進入階段二三。**
(注:即使排除 2025-04→2026-07 貨櫃缺口,樣本內為完整覆蓋、右側仍輸 0050,
故結論不依賴缺口期。)

## 階段一補充測試:月營收版(最後一次測試)
假設「季報落後2-3個月是主因」→ 改用三檔**月營收加總YoY**(次月10日公布)+ BDI 方向確認。
詳見 `reports/phase1_monthly_supplement.md`。

| 期間 | 季報版年化 | 月營收版年化 | 0050 | 月營收版MDD | 假訊號率 |
|---|--:|--:|--:|--:|--:|
| 樣本內 2010–2017 | -4.0% | 3.8% | **4.8%** | -43.9% | 33.3% |
| 樣本外 2018–2026 | 13.4% | 20.4% | **20.5%** | -73.7% | 66.7% |

- **Q1 時效**:可配對訊號中月營收平均早 **146 個交易日**(中位134);但制度性時差僅約
  30 個交易日 → 差額來自「規則定義不同」,不能全歸功於時效。
- **Q2 年化**:改善 +7~8pp,但**樣本內外都沒勝過 0050**(樣本外 20.4% vs 20.5% 打平)。
- **Q3 回撤**:**如你所料沒有實質改善**(樣本外 -76.3%→-73.7%,僅 2.5pp),
  回撤來自循環股波動本身,不是進場時點能解決。
- 樣本外假訊號率 0%→**66.7%(>50%)**,依事前登記規則直接判**不可行**:更快的訊號=更多雜訊。
- 台積電(非循環)套同一規則樣本外年化 **23.4%,比三雄還好** → 抓到的是營收動能+大盤beta。

**→ 結論與階段一相同:年化未勝 0050,策略不可行。專案結案,不進入階段二、三。**
