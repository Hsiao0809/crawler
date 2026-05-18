# Threads Stock Tracker

抓取 Threads 公開貼文 → 萃取台股代碼與動作標籤 → 串 TWSE / TPEx OpenAPI 取得基本面 → 寫成 SQLite + JSON + 靜態儀表板。針對台股相關內容的帳號設計（範例：`@evachien.chien`）。

跟 [Hsiao0809/threads-stock-watch](https://github.com/Hsiao0809/threads-stock-watch) 同樣的問題、同樣的資料路線（瀏覽器自動化 + TWSE/TPEx OpenAPI），改用 Python，並保留純 HTTP 解析作為輕量備援。

## 它做什麼

1. **取貼文**：用 Playwright 啟動 headless Chromium 開 `https://www.threads.com/@<user>`，邊滾邊攔 `/api/graphql` 回應。同時保留純 HTTP 模式作為快速備援。
2. **萃股票**：在每則貼文裡找台股代碼／中文公司名（最長別名優先匹配），並從句子內的關鍵字判斷動作標籤：`買 / 賣 / 漲停 / 跌停 / 觀察 / 後悔`。
3. **接基本面**：對所有提到的個股，從 TWSE / TPEx 公開 OpenAPI 拉收盤價、本益比、股價淨值比、殖利率、EPS、營收 YoY、毛利率、ROE、負債比。
4. **產出**：
   - `data/threads.db` — 累積的 SQLite（含每次抓取的快照）。
   - `data/latest.json` — 給下游用的分析 JSON（schema 跟參考專案相容）。
   - `public/index.html` — 單檔靜態儀表板，可掛 GitHub Pages。
   - GitHub Actions 排程每 4 小時自動更新並 commit 回 repo。

> 這只是資訊萃取工具，**不是投資建議**。

## 本機跑

```bash
pip install -r requirements.txt
pip install playwright && playwright install chromium      # 啟用 --browser 模式

python main.py track evachien.chien --browser --scroll-rounds 6 --post-pages 4
python main.py stocks evachien.chien --refresh-universe --refresh-fundamentals --out data/latest.json
python scripts/render_dashboard.py data/latest.json public/index.html
```

第一次跑會先建好 SQLite + universe 快取 + fundamentals 快取；之後只用 `--refresh-fundamentals` 就會更新基本面。

## GitHub Actions（排程）

repo 內含 `.github/workflows/update.yml`：

- 預設每 4 小時跑一次（也可從 Actions 頁手動觸發），抓 `evachien.chien`。
- 用 Playwright + Chromium 抓貼文，跑 stocks 分析，更新 `data/`、`public/index.html`，然後 commit 回 branch。
- 自動把 `public/` 部署到 GitHub Pages（在 repo Settings → Pages 把 Source 設為 `GitHub Actions`）。
- 想換帳號：手動觸發時填 `handle` 輸入欄，或改 workflow 的預設值。

## CLI

```
threads-tracker track <user>      # 抓貼文（HTTP 或 --browser）
threads-tracker show <user>       # 看 DB 內容
threads-tracker analyze <user>    # 一般活動分析（發文頻率、hashtag、字頻）
threads-tracker stocks <user>     # ★ 股票專屬分析（mention + action + 基本面）
threads-tracker parse-file <user> <html>  # 解析本機另存的 Threads HTML
threads-tracker export <user> <out.csv|out.json>
```

`stocks` 子命令常用參數：

```
--refresh-universe       # 從 TWSE/TPEx OpenAPI 拉完整上市櫃名錄
--refresh-fundamentals   # 從 TWSE/TPEx OpenAPI 拉價格＋估值＋EPS＋營收＋損益＋資產
--out data/latest.json   # 同時輸出 JSON
--top 20                 # 印出前 N 名
--json                   # 直接印 JSON
```

## 為什麼這個版本能跑（之前那版的問題）

第一版用 `requests` 直接打 `threads.com`，被 Threads 反爬機制擋下（403）。Threads 對非瀏覽器 client 的 TLS / header 指紋很挑，純 HTTP 不一定打得贏。

這版照參考專案的做法改用真實瀏覽器（Playwright Chromium），請求的 TLS 握手、JS 執行、`/api/graphql` 呼叫都跟真人一樣，並順手攔 GraphQL 回應。對 anti-bot 來說就是個正常瀏覽 session。

純 HTTP 模式還在（`track` 不加 `--browser`），它輕量、適合 Threads 沒擋的時候，或當作 `parse-file` 的後端。但**正式跑請用 `--browser`**。

## 資料來源（基本面）

- TWSE OpenAPI: `https://openapi.twse.com.tw/`
  - `STOCK_DAY_ALL` 收盤、`BWIBBU_ALL` 本益比/股價淨值比/殖利率、`t187ap14_L` EPS、`t187ap05_L` 月營收、`t187ap06_L_*` 損益、`t187ap07_L_*` 資產負債
- TPEx OpenAPI: `https://www.tpex.org.tw/openapi/v1/`
  - `tpex_mainboard_daily_close_quotes` 收盤、`tpex_mainboard_peratio_analysis` 估值、`mopsfin_t187ap14_O` EPS、`mopsfin_t187ap05_O` 月營收、`mopsfin_t187ap06_O_*` 損益、`mopsfin_t187ap07_O_*` 資產負債

## 偵錯

```bash
python main.py -v track evachien.chien --browser --dump-dir debug/
# debug/browser_<user>.html      # 最終渲染 HTML
# debug/browser_<user>_graphql.json  # 攔到的 GraphQL 回應原文
```

## 測試

```bash
python tests/test_parser.py
python tests/test_e2e.py
python tests/test_stocks.py    # ★ 股票萃取 + 動作標籤 + 完整 report
```
