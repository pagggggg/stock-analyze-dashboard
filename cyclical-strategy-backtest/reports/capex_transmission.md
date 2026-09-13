# 雲端 CAPEX 對半導體供應鏈營收的傳導研究

- 產生時間（UTC）：2026-08-07T15:59:36+00:00
- 最新已完成日曆季：2026Q2；分析終點：2026Q1
- 研究期間：2014Q1 至 2026Q1；抓取基期自 2013Q1
- 範圍：獨立研究管線，不是交易策略，不估算報酬、進出場或可交易性。
- 事前登記：使用專案目錄 `capex_transmission_config.yaml` 的 2026-08-07 鎖定規格及同日獨立審查修正；不使用公司 guidance 資料。
- 本次設定檔 SHA-256：`230f24ede0d375b2acf8f573be68253b3f9e93d34318989b0b07723d24afb140`。
- SEC Fair Access User-Agent：來源 `local_research_fallback`；可用 `.env` 的 `SEC_USER_AGENT` 設定直接聯絡資訊。
- 執行標籤：preregistered，stationary bootstrap 99,999 次。

## 方法

固定四家雲端公司 CAPEX 先以核心 `build_four_company_capex_aggregate` 加總；只有 t 與 t-4 四家公司都完整時才計算 YoY。
各營收層固定成員，以 2014Q1 的固定匯率逐季換算後作字面加總；任一成員缺值，該層當季及需要該水準的 YoY 都留白。
落後方向固定為 **CAPEX(t) 對 revenue(t+lag)**，lag=0..8；正 lag 表示營收晚於 CAPEX。
原始確認族只含 full 視窗的 8 層 x 9 lag = 72 格，使用 circular stationary bootstrap 與 72 格 Holm 校正。
early/late 是另一個 draws=0 的純描述執行，bootstrap/Holm 欄位刻意留白，沒有加入或改動原始 72 格族。
去趨勢確認族另為 72 格：分別將 CAPEX YoY 與營收 YoY 對截距、時間、2020Q1 後水準及斜率殘差化，再獨立 bootstrap/Holm。
門檻：描述 n>=12、推論 n>=32、alpha=0.05、期望區塊長度 8 季。
缺季會把每一格配對切成連續日曆季度區塊；stationary bootstrap 只在各區塊內循環抽樣，絕不跨越缺口。各格區塊數、長度及邊界均寫入 lag CSV。

## 資料涵蓋

|類別|成員|名稱|來源|幣別|檢查區間|首筆|末筆|可用/應有|缺季|
|---|---|---|---|---|---|---|---|---:|---|
|CAPEX|MSFT|Microsoft|SEC companyfacts CIK 0000789019|USD|2014Q1..2026Q2|2014Q1|2026Q2|50/50|無|
|CAPEX|GOOGL|Alphabet|SEC companyfacts CIK 0001288776,0001652044|USD|2014Q1..2026Q2|2014Q1|2026Q2|50/50|無|
|CAPEX|AMZN|Amazon|SEC companyfacts CIK 0001018724|USD|2014Q1..2026Q2|2017Q3|2026Q2|36/50|2014Q1,2014Q2,2014Q3,2014Q4,2015Q1,2015Q2,2015Q3,2015Q4,2016Q1,2016Q2,2016Q3,2016Q4,2017Q1,2017Q2|
|CAPEX|META|Meta Platforms|SEC companyfacts CIK 0001326801|USD|2014Q1..2026Q2|2014Q1|2026Q1|41/50|2016Q1,2016Q2,2016Q3,2016Q4,2017Q1,2017Q2,2017Q3,2017Q4,2026Q2|
|Revenue|NVDA|NVIDIA|SEC companyfacts CIK 0001045810|USD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|AMD|Advanced Micro Devices|SEC companyfacts CIK 0000002488|USD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|AVGO|Broadcom|SEC companyfacts CIK 0001441634,0001649338,0001730168|USD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|2330|台積電|FinMind taiwan_stock_financial_statement|TWD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|3711|日月光投控|FinMind taiwan_stock_financial_statement|TWD|2014Q1..2026Q1|2018Q3|2026Q1|31/49|2014Q1,2014Q2,2014Q3,2014Q4,2015Q1,2015Q2,2015Q3,2015Q4,2016Q1,2016Q2,2016Q3,2016Q4,2017Q1,2017Q2,2017Q3,2017Q4,2018Q1,2018Q2|
|Revenue|MU|Micron Technology|SEC companyfacts CIK 0000723125|USD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|2408|南亞科|FinMind taiwan_stock_financial_statement|TWD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|ASML|ASML Holding|SEC 6-K U.S. GAAP exhibits CIK 0000937966|EUR|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|AMAT|Applied Materials|SEC companyfacts CIK 0000006951|USD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|LRCX|Lam Research|SEC companyfacts CIK 0000707549|USD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|KLAC|KLA|SEC companyfacts CIK 0000319201|USD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|6488|環球晶|FinMind taiwan_stock_financial_statement|TWD|2014Q1..2026Q1|2015Q1|2026Q1|45/49|2014Q1,2014Q2,2014Q3,2014Q4|
|Revenue|2308|台達電|FinMind taiwan_stock_financial_statement|TWD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|3017|奇鋐|FinMind taiwan_stock_financial_statement|TWD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|6139|亞翔|FinMind taiwan_stock_financial_statement|TWD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|
|Revenue|6196|帆宣|FinMind taiwan_stock_financial_statement|TWD|2014Q1..2026Q1|2014Q1|2026Q1|49/49|無|

台灣名單已更正：原要求中的錯誤代號 3416 未使用，廠務層採用 **6196 帆宣**。
`capex_history.csv` 與 `capex_transmission_revenue.csv` 都明列 2013Q1-Q4，`analysis_role=yoy_base_only`；2013 僅作為可能的 t-4 基期，只有基期與當期都完整時才重現 2014 YoY。涵蓋率、報告與主要研究期間仍自 2014Q1 起。
已知 FinMind 非單季營收排除：3711 2018Q2（FinMind selected six-month cumulative value）；6488 2014Q2（H1 cumulative）；6488 2014Q4（H2 aggregate）；原值只保留於 `excluded_source_value`，分析值留白且不替換、不插值。

## CAPEX 定義與稽核

- MSFT 與 GOOGL 使用 PPE 現金 CAPEX；META total = PPE + financing-lease principal，且兩者都存在才成立。
- AMZN 只使用 `PaymentsToAcquireProductiveAssets`：Amazon gross productive-assets cash purchases，範圍包含 PP&E、internal-use software 及其他 intangibles；不是純 PPE，也不與其他公司宣稱範圍完全相同。
- AMZN 2017Q3 前沒有該 tag 的 standalone 值，全部維持 insufficient；排除 `PaymentsToAcquirePropertyPlantAndEquipment`、`PaymentsForProceedsFromProductiveAssets`、`CapitalExpenditures`。理由：Semantically different legacy, net, and generic CAPEX tags are excluded; they are not coalesced with, used to reconstruct, or substituted for PaymentsToAcquireProductiveAssets.
- META 缺少 lease principal 時絕不當作 0。META 缺口：2016Q1,2016Q2,2016Q3,2016Q4,2017Q1,2017Q2,2017Q3,2017Q4,2026Q2
- META 完整列的 CSV notes 明載 included financing-lease principal；各成分的 tag、accession、來源網址及 filing date 都保留。
- 所有 SEC 非日曆財年公司（包括 MSFT 與供應鏈美股）：依報告期間中點所落的日曆季轉換，不把 fiscal quarter 名稱或期末月份直接當 calendar quarter。
- standalone quarter 可為直接季值，或同一 CIK、同一設定 metric 內的相鄰 YTD、FY 算術差額；不插值、不以前後季補值。
- Vintage 規則先選最早有效 filing date（衍生列為各成分 filing date 的最大值），再依設定 source/tag priority，最後才以直接季值勝過衍生值作同日 tie-break。較晚 comparative disclosure 只能填補完全沒有更早 standalone 候選的歷史季度；所有延遲 filing date 仍完整揭露。這是 historical actual research，不是 point-in-time trading，也不把延遲揭露當 contemporaneous signal。
- 固定匯率：TWD/USD=30.2796721311；USD/EUR=1.370504918，均為 2014Q1 FRED 61 筆非缺失日值平均。

## Full 最佳落後期

`n/ESS` 同時列出季度配對數與 lag-8 自相關診斷的有效樣本數；ESS 不是新的顯著性檢定。

|層級|描述峰值 lag|支持 lag|峰值 r|峰值一般 p|峰值 block-bootstrap p|峰值 Holm p|峰值 n/ESS|支持 r|支持一般 p|支持 bootstrap p|支持 Holm p|支持 n/ESS|狀態|
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
|chip_design 晶片設計|0|NA|0.500|0.006|0.056|1.000|29/9.8|NA|NA|NA|NA|NA/NA|descriptive_only|
|foundry 晶圓代工/台積電對照|0|NA|0.646|1.55e-04|0.004|1.000|29/14.1|NA|NA|NA|NA|NA/NA|descriptive_only|
|packaging 封測|1|NA|0.473|0.013|0.094|1.000|27/7.8|NA|NA|NA|NA|NA/NA|descriptive_only|
|memory 記憶體|0|NA|0.816|6.75e-08|1.90e-04|1.000|29/10.9|NA|NA|NA|NA|NA/NA|descriptive_only|
|equipment 設備|1|NA|0.487|0.009|0.052|1.000|28/11.6|NA|NA|NA|NA|NA/NA|descriptive_only|
|materials 材料|4|NA|0.342|0.094|0.258|1.000|25/8.6|NA|NA|NA|NA|NA/NA|descriptive_only|
|power_cooling 電源散熱|3|NA|0.735|1.90e-05|0.002|1.000|26/8.1|NA|NA|NA|NA|NA/NA|descriptive_only|
|facilities 廠務|7|NA|0.695|3.27e-04|0.003|1.000|22/12.2|NA|NA|NA|NA|NA/NA|descriptive_only|

## 分段一致性

必須 early 與 late 的正相關描述峰值都**精確等於** full 的支持 lag；不接受相鄰 lag 替代。

|層級|full 支持 lag|early 峰值 lag|early n/ESS|early r|late 峰值 lag|late n/ESS|late r|判定|
|---|---:|---:|---:|---:|---:|---:|---:|---|
|chip_design 晶片設計|NA|NA|NA/NA|NA|0|25/8.9|0.369|not_assessable|
|foundry 晶圓代工/台積電對照|NA|NA|NA/NA|NA|1|24/9.0|0.598|not_assessable|
|packaging 封測|NA|NA|NA/NA|NA|1|24/6.6|0.497|not_assessable|
|memory 記憶體|NA|NA|NA/NA|NA|0|25/9.8|0.802|not_assessable|
|equipment 設備|NA|NA|NA/NA|NA|2|23/10.9|0.466|not_assessable|
|materials 材料|NA|NA|NA/NA|NA|8|17/5.9|0.324|not_assessable|
|power_cooling 電源散熱|NA|NA|NA/NA|NA|3|22/6.2|0.728|not_assessable|
|facilities 廠務|NA|NA|NA/NA|NA|7|18/9.9|0.745|not_assessable|

## 去趨勢比較

|層級|raw 描述峰值 lag/r|detrended 描述峰值 lag/r|detrended 峰值 n/ESS|raw 支持 exact lag|該 lag detrended r|該 lag detrended Holm p|該 lag n/ESS|exact-lag 穩健性|
|---|---|---|---:|---:|---:|---:|---:|---|
|chip_design 晶片設計|0/0.500|0/0.175|29/9.7|NA|NA|NA|NA/NA|not_assessable|
|foundry 晶圓代工/台積電對照|0/0.646|0/0.561|29/12.9|NA|NA|NA|NA/NA|not_assessable|
|packaging 封測|1/0.473|1/0.743|27/7.9|NA|NA|NA|NA/NA|not_assessable|
|memory 記憶體|0/0.816|0/0.702|29/10.7|NA|NA|NA|NA/NA|not_assessable|
|equipment 設備|1/0.487|2/0.671|27/15.2|NA|NA|NA|NA/NA|not_assessable|
|materials 材料|4/0.342|4/0.437|25/7.7|NA|NA|NA|NA/NA|not_assessable|
|power_cooling 電源散熱|3/0.735|3/0.690|26/7.8|NA|NA|NA|NA/NA|not_assessable|
|facilities 廠務|7/0.695|7/0.745|22/12.4|NA|NA|NA|NA/NA|not_assessable|

## 有效循環數

- 完整、交替的 trough-to-trough 有效獨立循環數：**0**。
- 循環邊界：未辨識出完整循環
- 研究設計事前辨識的約三段景氣事件：2018-2019 downcycle；2020-2021 pandemic cycle；2024-2026 AI expansion。這是定性事件數，不等於符合 trough-to-trough 規則的有效獨立循環。
- 警告：季度 n 是重疊 YoY 的觀測筆數，不是獨立循環數；不能把 n 季解讀為 n 個獨立景氣實驗。
- 因少於四個完整循環，本研究證據明確分類為 **exploratory（探索性）**，即使其他統計條件通過亦同。

## 台積電對照

foundry（晶圓代工/台積電對照）的描述峰值 lag=0，r=0.646、一般 p=1.55e-04、block-bootstrap p=0.004、Holm p=1.000；正向 Holm 顯著 lag=無；對照標準 FAIL。
對照不可評估，因此不能排除結果只是廣泛半導體景氣或 market beta。

## 四項標準與結論

|標準|可評估|結果|細節|
|---|---|---|---|
|至少三個非對照層在 full 有正向 Holm 支持|FAIL|FAIL|{"count":0,"required":3}|
|所有受支持非對照層的 early/late 峰值精確一致|FAIL|FAIL|{}|
|所有受支持非對照層在 raw exact lag 去趨勢後仍顯著|FAIL|FAIL|{}|
|台積電對照沒有正向 Holm 顯著 lag|FAIL|FAIL|{"control_layer":"foundry","positive_significant_lags":[]}|

核心 decision helper 的最終結論（原樣）：**傳導關係不穩定,不進入策略設計階段**
此為事前登記的失敗分類用語；本次實證意義是樣本不足、未能證明穩定傳導，不是統計上證明關係必然不存在或必然不穩定。

**第二階段限制：除非上述四項標準全部 PASS，否則禁止進入 phase 2。**

## 限制

- 研究期間事前辨識約三段景氣事件／regime，但機械規則辨識的完整獨立循環為上表數值；重疊 YoY、共同週期與低有效循環數會產生 pseudo-correlation。
- 共同趨勢即使經殘差化仍不等於因果；相關與領先落後不能證明 CAPEX 導致特定供應商營收。
- 公開 CAPEX guidance 會立即反映在價格，本研究刻意不用 guidance；即使財報相關存在，也完全不代表可交易性。
- 多角化公司的總營收不是純 AI 曝險，層級加總混合了非雲端、非 AI 與不同產品週期。
- 缺值不插補，會改變不同 lag 的配對樣本；TWD 與 EUR 全期使用固定 2014Q1 FX，忽略之後匯率變動。
- AMZN productive-assets 購買包含 PP&E、內部使用軟體及其他無形資產，與其他公司的 PPE 定義不同；2017Q3 前歷史因排除舊 net/legacy tags 而不可用。
- 延遲 comparative facts 只可填補原本完全缺少候選的歷史季度，並非當時可交易訊號；本研究不主張 point-in-time trading。
- 72 格 Holm 校正處理多重檢定；gap-aware stationary block bootstrap 只在連續季度區塊內抽樣。兩者只能降低、不能消除自相關與模型選擇風險。
