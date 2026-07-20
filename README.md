# 台積電 EPS 試算與估值工具 (Stock_analyze)

用**公司法說會指引**重建台積電(2330.TW)的單季 EPS 預估,對照**市場分析師共識**,輸出**估值價格區間**。純 Python + 繁體中文,程式碼逐段中文註解,方便逐條檢查每個假設。

> ⚠️ 本工具僅為個人試算,所有數字請回到原始出處核實,**不構成投資建議**。

## 設計原則

1. **先手動、再自動**:預設就是「單季 + 手動輸入」模式,保證跑得動;加 `--data-mode auto` 才嘗試自動抓取,且失敗會自動退回手動 CSV。
2. **每個數字都標來源**:所有輸入都以「數值 + 來源」成對儲存,報告會把每個假設連同來源逐條列出。
3. **攤開計算邏輯**:報告把「從營收到 EPS」每一步都印出來,不是只給答案。

## 功能對應

| 需求 | 實作 |
| --- | --- |
| ① 資料層(近8季財務) | `--data-mode auto` 用 **FinMind** 抓;手動 `data/financials_manual.csv` 作 fallback |
| ② 法說會指引輸入 | `config/assumptions.yaml`(季營收美元區間、毛利率區間、匯率假設) |
| ③ EPS 三情境試算 | `src/eps_calc.py`(樂觀/中性/悲觀) |
| ④ 估值區間 | `--data-mode auto` 用 **TWSE 個股日本益比** 抓近10年聚合;手動 `data/pe_history.csv` 作 fallback |
| ⑤ 預期差 | 用 yfinance 分析師共識(當季/今年FY)自動算;或 `config` 手填 |
| ⑥ 單一 markdown 報告 | `reports/report.md`,所有假設列成清單、每筆資料標來源+抓取日期 |
| ⑦ 估值儀表板(教學層) | `src/metrics.py`:前瞻PE / PEG / FCF Yield / EV·EBITDA,即時連動現價;含名詞白話註解、連動圖解、判讀速查表 |
| ⑧ 共識EPS監控 | yfinance 共識EPS 記錄到 `data/consensus_history.csv`,報告顯示較上次「上修/下修/持平」 |
| ⑨ 視覺化儀表板(HTML) | `--html` 產出單一 `reports/dashboard.html`(plotly.js **內嵌、離線可開、手機可讀**):估值卡片 + 本益比河流圖 + EPS走勢 + 共識監控 + **FCF品質檢查** |

### 估值儀表板 4 指標(auto 模式,資料來自 yfinance,全部即時連動 TWSE 現價)

| 指標 | 算式 | 衡量 |
| --- | --- | --- |
| 前瞻PE | 現價 ÷ 年化EPS | 市場為每 1 元(未來)盈餘付幾元 |
| PEG | 前瞻PE ÷ 盈餘成長率 | 把「貴」和「成長」一起看(成長率 = 2027/2026 共識EPS) |
| FCF Yield | 近4季自由現金流 ÷ 市值 | 用現價買,每年拿回多少自由現金 |
| EV/EBITDA | (市值+負債−現金) ÷ 近4季EBITDA | 含負債現金的整體企業估值,可跨公司比 |

## 安裝與執行

```bash
# 1. (建議) 建立虛擬環境
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate

# 2. 安裝套件(手動模式其實只需要 PyYAML)
pip install -r requirements.txt

# 3. 執行(手動模式,讀 config/ 與 data/ 底下的檔案)
python main.py

# 產出報告在 reports/report.md
```

### 視覺化儀表板(單一 HTML,免架 server)

```bash
# 加 --html:除了 report.md,再多產一個 reports/dashboard.html
python main.py --data-mode auto --html
# 產出後直接用瀏覽器打開 reports/dashboard.html(離線可開、手機可讀)
```

`dashboard.html` 是**單一自帶檔**(plotly.js 內嵌,不連網也能看),內含 6 塊:

| 區塊 | 內容 |
| --- | --- |
| 三行摘要(置頂大字) | 我的單季EPS試算 · 合理股價中樞 · 現價溢價/折價 |
| ① 估值儀表板卡片 | 前瞻PE / PEG / FCF Yield / EV·EBITDA,**便宜綠 / 合理灰 / 貴紅**,各附白話一行 |
| ② 本益比河流圖 | 近10年股價疊 12.5x / 22.1x / 35.5x 三條河道 + 現價標記,一眼看位階 |
| ③ EPS 走勢 | 近8季實際EPS + 3Q26 三情境試算(斜線紋柱區分實際/試算) |
| ④ 共識EPS監控 | 2026/2027 共識EPS 歷史折線 + 上修(▲綠)/下修(▼紅)標記 |
| ⑤ **FCF 品質檢查**(新功能) | 資本支出年增率(領先2年)vs 營收年增率雙線;**存貨天數 / 應收天數 / OCF年增率**三燈號(綠/黃/紅,門檻寫在 `src/fcf_quality.py` 註解) |

- **響應式**:卡片 grid 自動換行、圖表寬度 100% 自適應,手機瀏覽器可讀。
- **資料需求**:河流圖與 FCF 品質需 `--data-mode auto`(FinMind 長區間財報 + 日股價);手動模式仍會產出其餘圖,缺的以「資料不足」佔位。
- 安裝提醒:此功能需 `plotly`(見 requirements.txt);若遇 PEP 668 錯誤,加 `--break-system-packages`。

## 多股個人選股分析儀表板(可遠端存取)

> 只用**公開市場數據**做估值研究,**無任何持倉或個人交易紀錄**。掃描總表僅供縮小研究範圍,非買進清單。

把一份觀察清單掃成一個靜態網站(多頁,離線也能開,可部署到 GitHub Pages 手機連線):

```bash
# 1) 編輯觀察清單
#    config/watchlist.yaml —— 填 stock_id / name;有法說指引檔的股票可加 guidance

# 2) 產生網站到 public/
python build_site.py
open public/index.html            # 本機預覽
```

**三層哲學**:

| 層 | 內容 |
| --- | --- |
| 第一層 · 狀態燈 | 🟢 無訊號級變化／🟡 有共識異動或 FCF 燈變色／🔴 有股票跨越估值門檻(前瞻PE 判讀等級改變) |
| 第二層 · 訊號流水 | 只列**共識EPS 上下修 / FCF 品質燈變色 / 估值門檻跨越**;**股價漲跌屬雜訊,刻意不列** |
| 第三層 · 個股詳情 | 點名稱展開:四指標卡 + 本益比河流圖 + FCF 品質三燈 + FCF 雙線 + EPS 走勢 + 共識折線 |

- **掃描總表**:所有股票四指標一覽、**點欄位可排序**、依判讀著色(便宜綠/合理灰/偏貴橘/貴紅)、**盈餘修正動能欄**(僅標記近期共識被上/下修,**依原則等回測驗證後才加權重,目前不評分**)。
- **跨日比對**:每次執行把每檔快照寫進 `data/scan_state.json`、事件寫進 `data/signal_log.csv`、共識歷史寫進 `data/consensus/<代號>.csv`;**隔次執行才能比出「上修/下修/燈變色」**。這些狀態檔要進版控。
- **資料來源**:FinMind(財報/資產負債/現金流/日股價)+ yfinance(共識EPS/FCF/EV 元件),不依賴 TWSE 逐月抓,故可套用任意台股代號。

### 每日自動更新 + 部署 GitHub Pages

`.github/workflows/daily.yml` 已設定好:每天定時(+ 每次 push / 手動)重跑 `build_site.py`,把更新後的狀態檔 commit 回 repo,並部署 `public/` 到 GitHub Pages。首次啟用步驟見本文件結尾或對話說明。

- **選填**:設 GitHub Secret `FINMIND_TOKEN`(至 finmindtrade.com 免費註冊)可提高抓取額度;本機可放 `.env`(已 gitignore)。

### 進階:自動抓取(免費 API,取代手動 CSV)

```bash
# 近8季財務用 FinMind、近10年本益比用 TWSE;任一步失敗都會自動退回手動 CSV
python main.py --data-mode auto
```

自動模式做的事:

| 資料 | 來源(免費) | 產出 / 快取 |
| --- | --- | --- |
| 近8季財務(營收、毛利、營業費用、稅、淨利、股數) | **FinMind** `TaiwanStockFinancialStatements` | 存 `data/financials_auto.csv` |
| 近10年每日本益比 → 年度高/低/平均 | **TWSE** 個股日本益比 `BWIBBU` | 存 `data/pe_history_auto.csv` |

- **每筆資料標來源 + 抓取日期**:報告第七節逐季列出「FinMind 財報(抓取 YYYY-MM-DD)」,本益比列出「TWSE(抓取 YYYY-MM-DD)」。
- **自動驗證**:把 API 近8季數字和你原本 `financials_manual.csv` 逐欄對照,**差異 > 2% 的欄位會列成警告表**,提醒你人工核對(報告第七節)。
- **快取**:所有 API 回應存在 `cache/`。過去月份的本益比永久快取、當月短快取、財報 12 小時快取,所以**第一次 auto 較慢(TWSE 要逐月抓約 1~2 分鐘),之後幾乎不再連網**。刪掉 `cache/` 會在下次重抓。
- **保留手動作 fallback**:auto 不會覆蓋你手寫的 `financials_manual.csv` / `pe_history.csv`,兩者仍是斷網或 API 失敗時的備援。

> 安裝提醒:自動模式需要 `FinMind`。若你用 Homebrew Python 或 Python 3.14 遇到安裝問題,見 `requirements.txt` 末段的安裝提示(多半是 `--break-system-packages` 或先單獨裝 `lxml>=6`)。

### 其他參數

```bash
python main.py \
  --config config/assumptions.yaml \
  --financials data/financials_manual.csv \
  --pe data/pe_history.csv \
  --out reports/report.md \
  --data-mode manual        # 或 auto
```

## 三步驟使用流程

1. **填指引**:打開 `config/assumptions.yaml`,把 `guidance`(季營收美元區間、毛利率區間、匯率)換成你查到的法說會數字,並在每個 `source` 寫清楚來源。
2. **填共識(可選)**:在 `consensus` 區塊填分析師共識 EPS(單季/全年),要算預期差才需要。
3. **執行**:`python main.py`,打開 `reports/report.md` 逐條檢查假設與結果。

## EPS 試算邏輯(核心公式)

```text
營收(美元) × 匯率        = 營收(台幣)
營收(台幣) × 毛利率      = 毛利
營收(台幣) × 營業費用率  = 營業費用
毛利 − 營業費用          = 營業利益
營收(台幣) × 業外比      = 業外收支
營業利益 + 業外收支      = 稅前淨利
稅前淨利 × (1 − 稅率)    = 稅後淨利
稅後淨利 ÷ 股數          = 單季 EPS
```

三情境:**樂觀** = 高營收×高毛利率;**中性** = 區間中點;**悲觀** = 低營收×低毛利率。
(匯率、營業費用率、稅率、業外比、股數三情境相同,可自行擴充成情境化。)

## 專案結構

```
Stock_analyze/
├── main.py                       # 主程式(CLI,串接整個流程)
├── config/
│   └── assumptions.yaml          # ★手動輸入:法說會指引 + 模型假設 + 共識
├── data/
│   ├── financials_manual.csv     # 近8季財務數據(手動 fallback + 驗證基準)
│   ├── financials_auto.csv       # FinMind 抓取結果(執行 auto 後產生)
│   ├── pe_history.csv            # 近10年本益比(手動 fallback)
│   └── pe_history_auto.csv       # TWSE 抓取結果(執行 auto 後產生)
├── src/
│   ├── models.py                 # 資料模型(SourcedValue/SourcedRange 帶來源)
│   ├── guidance.py               # 讀取 assumptions.yaml
│   ├── data_layer.py             # 資料層(手動 CSV + FinMind 財報/資產負債/現金流/日股價 + TWSE 本益比 + 驗證)
│   ├── cache.py                  # API 檔案快取(避免重複抓、省 FinMind 額度)
│   ├── eps_calc.py               # EPS 三情境試算引擎 + 模型回測
│   ├── valuation.py              # 年化 + 本益比價格矩陣
│   ├── expectation.py            # 預期差(對照共識)
│   ├── metrics.py                # 估值儀表板 4 指標
│   ├── river.py                  # ★HTML:本益比河流圖資料(月頻股價 + TTM EPS 河道)
│   ├── fcf_quality.py            # ★HTML:FCF 品質檢查(DIO/DSO/OCF 燈號 + 資本支出vs營收雙線)
│   ├── dashboard_html.py         # ★HTML:單一 dashboard.html 產生器(plotly 內嵌)
│   └── report.py                 # 產出單一 markdown 報告
├── cache/                        # API 快取(可刪,gitignore)
├── reports/
│   ├── report.md                 # 產出的報告
│   └── dashboard.html            # ★--html 產出的視覺化儀表板(單一檔,離線可開)
└── requirements.txt
```

## 單位約定(務必一致)

| 項目 | 單位 | 範例 |
| --- | --- | --- |
| 營收(美元) | 十億美元 | `25.0` = 250 億美元 |
| 營收(台幣) | 十億台幣 | `800.0` = 8000 億台幣 |
| 匯率 | 1 美元 = ? 台幣 | `32.0` |
| 百分比 | 直接填數字 | `57.8` 代表 57.8% |
| 股數 | 十億股 | `25.93` = 259.3 億股 |
| EPS | 台幣元 / 股 | `13.21` |

> 小技巧:EPS = 淨利(十億台幣) ÷ 股數(十億股),分子分母「十億」自動約掉 → 直接得到「台幣元/股」。

## 報告內容(reports/report.md)

一、結論速覽 · 二、假設清單(逐條含來源) · 三、EPS 三情境試算(攤開計算鏈) · 四、估值價格矩陣 · 五、預期差 · 六、模型回測(公式重算歷史對照財報) · 七、資料層明細與驗證(近8季每筆標來源 + API vs 手動CSV 差異>2%示警) · 八、來源彙整與免責。

## 已知限制 / 備註

- **資料來源(自動)**:財務用 **FinMind**(免費、免 token,綜合損益表欄位齊全,回測誤差 <0.3%);本益比用 **TWSE 個股日本益比**(逐月抓每日值,聚合年度高/低/平均)。兩者皆免費 API。
- **FinMind 額度**:免費版有請求上限,故加了 `cache/` 檔案快取(財報 12 小時、本益比過去月份永久),正常使用一天打不到幾次 API。
- **年化口徑**:單季試算要換算成「全年」才能乘本益比。預設用 **TTM(試算季度之前最近3季實際 + 本季試算)**,它是滾動12月、和分析師「完整會計年度(FY)」口徑不完全一致,做全年預期差時請留意期間落差。可在 config 改成 `x4`。
- **驗證警告是正常的**:範例 `financials_manual.csv` 是手填近似值,和 FinMind 實際財報必有差(尤其稅率、業外比),所以 auto 模式會列出警告——這正是設計目的:提醒你以實際財報(API)為準去更新手動檔。
- **fallback 一律保底**:FinMind 或 TWSE 任一失敗(斷網/限流),該項自動退回對應手動 CSV,報告照樣產出。
