# Threads Account Tracker

追蹤、分析 Threads (Meta) 帳號的公開貼文，並把結果存進 SQLite，方便長期觀察。

針對的問題：Threads 沒有公開的讀取 API，網頁是 JavaScript 渲染但伺服器會在 HTML 裡內嵌完整的 GraphQL JSON（`__bbox.result.data`）。本工具直接抓那段 JSON，不需要登入也不需要瀏覽器自動化。

## 功能

- `track`：抓 `https://www.threads.com/@<user>` 的公開頁面，解析帳號資訊與最新貼文，寫入 SQLite。
- `show`：印出 DB 裡儲存的帳號與最近貼文。
- `analyze`：產出活動報告（發文頻率、互動量、熱門 hashtag/詞、發文時段……）。
- `parse-file`：從本機 HTML 檔解析（瀏覽器另存頁面後可用，用於無法直連 Threads 的環境）。
- `export`：把貼文匯出成 JSON 或 CSV。

每次 `track` 都會記錄一份 `post_snapshots`（同一則貼文不同時間的讚數／回覆數），可用來看互動隨時間的變化。

## 安裝

```bash
pip install -r requirements.txt
```

只需要 `requests`（必要）和 `beautifulsoup4` + `lxml`（目前未強制使用，但放著方便擴充）。Python ≥ 3.10。

## 使用範例

```bash
# 抓取並儲存 @evachien.chien 的公開資料
python main.py track evachien.chien

# 抓取時順便走 GraphQL，可拿到更多歷史（doc_id 可能要視 Meta 更新而調整）
python main.py track evachien.chien --graphql

# 看資料庫裡的內容
python main.py show evachien.chien --limit 30

# 產出分析報告
python main.py analyze evachien.chien

# 匯出
python main.py export evachien.chien posts.json
python main.py export evachien.chien posts.csv

# 排程：搭配 cron / launchd 定時跑 track，DB 會累積互動歷史
# 例：每小時抓一次
# 0 * * * * cd /path/to/crawler && /usr/bin/python3 main.py track evachien.chien
```

DB 預設位置 `data/threads.db`，可用 `--db` 或環境變數 `THREADS_DB` 覆寫。

## 在受限網路 / 無法直連 Threads 時

當執行環境（公司網路、CI、雲端 sandbox）擋住 `threads.com`，腳本會得到 `403 Host not in allowlist` 之類的錯誤。此時走「瀏覽器另存 → 本機解析」的路徑：

1. 在瀏覽器打開 `https://www.threads.com/@evachien.chien`。
2. 右鍵 → 另存新檔 → **Webpage, HTML Only**（不需要圖片資源）。檔名例：`page.html`。
3. 把檔案丟給 `parse-file`：

    ```bash
    python main.py parse-file evachien.chien path/to/page.html --store --show 20
    ```

`parse-file` 跟 `track` 走的是同一條解析路徑（`parse_profile_html`），所以拿到的 user_id / 貼文 / 互動數會完全一致。

> **本專案目前所在的 Claude Code on the web 沙箱**：對外網路只允許 `github.com` / `pypi.org` 等少數白名單主機，連不到 `threads.com`／`threads.net`，所以在這個容器內無法直接示範 `track`。在你自己的機器上跑沒有這個限制。

## 為什麼是直接抓 HTML，不用 Selenium / Playwright？

Threads 的 server-rendered HTML 裡，每則貼文都已經以 JSON 形式塞在 `<script>` 區塊。我們掃描每個 script，用 `json.JSONDecoder.raw_decode` 找出所有頂層 JSON 物件，再走訪節點找 `text_post_app_info`（Threads 貼文的特徵欄位）與有 `follower_count` 的使用者物件。這比起：

- 啟動完整瀏覽器：快幾個量級，省記憶體，不需 Chrome。
- 直接打 GraphQL：不用追蹤 Meta 隨時會換的 `doc_id`。
- 用 Threads 私有 API：不用登入帳號、不會卡 challenge。

當 Meta 改 JSON 結構時，因為我們是「在整棵樹找特徵欄位」而不是「跟著固定 path 走」，通常會比 schema-bound 的 parser 多撐一段時間。

GraphQL 路徑仍保留在 `client.py`，當 HTML 內嵌資料不夠（例如只想抓更舊的歷史）時可用 `--graphql` 啟用。注意 `doc_id` 是會變的；如果失效，可以開 Chrome DevTools → Network 找 `api/graphql` 的請求，把新的 `doc_id` 抄到 `client.DEFAULT_DOC_IDS`。

## 資料庫結構

- `accounts`：每個帳號一列，最近一次抓到的 profile 欄位。
- `posts`：每則貼文一列，欄位含貼文 pk、code、貼文網址（`https://www.threads.com/@<user>/post/<code>`）、文字、互動數、圖片 / 影片 URL。
- `post_snapshots`：每次抓取的快照，可看互動數隨時間變化。
- `account_snapshots`：粉絲數的時間序列。

可以直接 SQL 查詢：

```bash
sqlite3 data/threads.db "SELECT pk, like_count, text FROM posts ORDER BY like_count DESC LIMIT 10"
```

## 偵錯

```bash
# 把所有 HTTP 回應原文存下來方便檢查
python main.py track evachien.chien --dump-dir debug/

# 看詳細 log
python main.py -v track evachien.chien
```

## 測試

```bash
python tests/test_parser.py
python tests/test_e2e.py
```

兩個 test 用合成的 Threads 風格 HTML / GraphQL payload 跑完「parse → store → analyze」整條鏈，確保解析邏輯與 schema 對得起來。
