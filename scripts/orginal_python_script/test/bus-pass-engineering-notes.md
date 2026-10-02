# bus-pass 查詢功能：工程決策紀錄（推甄「資訊能力」素材）

> 範圍：`scripts/orginal_python_script/`、`components/busPlanner.ts`、`components/bus-api.ts`、`databases/`。不含 BusPulse（`_recordTraffic`、12/17 的 "optimize bus riding time" 系列 commit）與 favoriteRoutes。
> 整理日期：2026-10-02。所有金鑰／secret 一律寫成 [已遮蔽]。

## 0. 資料來源與使用方式

**探索過的地方**

- Repo：`GEProject/last_two/expo-Bus-Route-App`（branch `last_two`，最新 commit `ca379bd`）。`GEProject/test/expo-Bus-Route-App` 是較舊的 clone，停在 `0da3a33`。
- 工作資料夾：`GEProject/test/` 裡的 `busPlanner.py`、`experiment.py`、`gen_new_database.py`、`getRoute.py`、`verify_metro_bus_route.py`、`buildRoute.md`、`record.txt`、`checkpoint_5284.jsonl` 等。
- 原始檔時間：`GEProject/p2p/` 是 `orginal_python_script` 的原始副本，檔案時間保留 2025-11 到 2025-12 的真實修改日。repo 裡的副本則是 2026-10-02 才 commit 的。
- 對話紀錄：本機只找到這次 session，`~/.claude/projects` 下沒有更早的 Claude Code 對話。**先前對話中的紀錄 → 找不到紀錄**。

**標記說明**

- 「（我重算）」：這次整理時用 repo 裡現存的資料檔，以唯讀方式重新計算的數字，**不是你當時留下的輸出**。寫進文件前請自己再跑一次確認。
- 「確定」：程式碼、commit 或檔案內容直接可證。
- 「需要我確認」：只能從程式推測動機或過程，請你補充或修正。

**時間軸（依 commit 與檔案修改時間）**

| 日期 | 事件 | 證據 |
|---|---|---|
| 2025-10-03 | 爬 5284 網站、建站名→SLID 表、`bus-api.ts` 與文件 | `7425331` |
| 2025-10-05 | App 內 demo 驗證 API 可用 | `cd7b765` |
| 2025-10-09 | Web 版 CORS：加 allorigins proxy | `af327ba` |
| 11-18～11-20 | 存 `ntnuBranch.html`/`252.html`、SLID 驗證、TDX 站牌資料庫 | `p2p/` 檔案時間 |
| 2025-11-20 | 點對點規劃 `busPlanner.ts` + `stop_id_map.json`，proxy 改 corsproxy.io | `8c811af` |
| 12-04～12-06 | 修 planner（SLID 去重、方向判斷）、proxy 改 codetabs、修 header、修隊友呼叫介面 | `1336e4b` `c373d2c` `e10baea` `1fc5fb9` `93a50c1` `4f37e46` |
| 2025-12-05 | Python async 版 `optimizeVersion.py`（含快取、鎖） | 檔頭日期、`p2p/` 檔案時間 |
| 2025-12-09 | `experiment.py`、`record.txt`、`stop_id_map_v3.json`、`buildRoute.md` | `test/` 檔案時間 |
| 2025-12-10 | 改為離線路線資料庫：`busPlanner optimize` | `2005916` |
| 12-13～12-14 | 用 5284 重建路線資料庫：`fix routes database` | `getRoute.py`、`0da3a33` |

## ⚠️ 寫推甄文件前先看：誠信與歸屬

1. **AI 協作痕跡**
   - `optimizeVersion.py` 檔頭寫 `Author: Gemini (Refactored from original)`。
   - `buildRoute.md` 作者欄是 "Solution Architect / Problem Solver"。
   - `test/verification.py` 是「template for the user」，import 了不存在的 `refactor` 模組，**看起來沒真正跑過**。
   - 建議在文件中誠實寫「我主導問題定義、假設與驗證，使用 AI 協助重構」。哪些是你自己寫的 → **需要我確認**。
2. **`busPlanner.ts` 不是只有你改過**。隊友 Fizzy 有大幅修改：
   - `e39db01`：914 行變動
   - `d73fd47`／`c569401`：路線時間估算
   - `a3a8919`：方向
   - `08b1ec2`：最近站牌
   - `52b25e1`：合併時 791 行變動

   引用某段程式時，請用 `git blame` 確認是你寫的。
3. 這裡的 `bus-api.ts` 沒有被現在的 app import。`8c811af` 之後 app 改用 `BusPlannerService`，它變成早期版本。

---

## 主題 1：沒有 API 文件時，如何分析政府網站

### 1-1 發現 HTML 靜態表格與動態 JSON 的對應（tte 前綴 / dynamic id）

- **遇到的問題**：
  - 台北 5284 的站位頁 `stoplocation.jsp?slid=…` 只列出「路線／站牌／去返程」，預估到站欄是空的。時間是頁面 JS 另外向 `StopLocationDyna?stoplocationid=…` 抓 JSON 後填入的。
  - 原始 HTML 的中文全是 `&#x516c;` 這類實體編碼，直接看不懂。
- **解決用的技術或想法**：
  1. 寫 `html_decode.py`，用 `html.unescape` 把 `in_page.html`／`page.html` 解成可讀版。
  2. 比對後發現兩邊靠 dynamic id 串起來：
     - 時間欄 `<td id="tte{id}">` 去掉 `tte` 前綴後的數字，等於 JSON `Stop[].n1` 以逗號切開後的第 2 欄（`vals[1]`）。
     - 第 8 欄（`vals[7]`）是秒數或狀態碼。
  3. 從頁面 JS 的 `TTEMap` 抄出狀態碼對照：`'0'` 進站中、`''`/`-1` 未發車、`-2` 交管不停、`-3` 末班已過、`-4` 今日未營運。
  4. 從範例頁可看出 `tte` 後面的數字就是該列連結的 `sid`，例如 `tte43116` 對應 `stop.jsp?sid=43116`。
  5. 做法是靜態 HTML 提供「哪條路線」，動態 JSON 提供「幾秒後到」，兩者 join。

  這套 join 一路沿用到 `main.py`、`bus_api.py`、`optimizeVersion.py` 和 `busPlanner.ts` 的 `fetchRealtimeBySlid`。
- **如何驗證**：
  - `main.py` 以「捷運公館站」實際查詢並印出路線、去返程、預估時間。
  - `cd7b765`「verify api functioning with demo」在 App 內 demo。
  - 存下來的 `in_page_decoded.html` 有 45 處 `tte`，標題是 `[師大分部]公車動態資訊`，JSON 來源是 `StopLocationDyna?stoplocationid=1000036`。
  - **當時比對了幾個站、成功率多少 → 找不到紀錄**。
- **展現的能力**：逆向分析無文件的資料來源、從前端程式推出資料協定、資料 join 設計。
- **證據位置**：
  - `scripts/orginal_python_script/consForBeingProductive/`：`html_decode.py`、`in_page.html`、`in_page_decoded.html`、`page.html`、`main.py`
  - `bus_api.py`
  - commit `7425331`（2025-10-03）、`cd7b765`
  - `busPlanner.ts` 的 `fetchRealtimeBySlid`（`[id^="tte"]`、`vals[1]`、`vals[7]`）
- **確定程度**：對應規則與檔案是**確定**。你當初怎麼找到的（DevTools Network 面板？讀 `ajax2.js`？）→ **需要我確認**。

### 1-2 路線頁結構（去程／返程）

- **遇到的問題**：路線頁 `route.jsp?rid=…` 沒有文件，要知道站序與方向。
- **解決用的技術或想法**：
  - 存下 `252.html`、`74.html` 分析。
  - 歸納出去程列的 class 是 `ttego\d+`，返程是 `tteback\d+`；站牌 `sid` 在 `<a href>` 的 query string 裡。
  - 用 regex 搭配 `parse_qs` 解析。
- **如何驗證**：
  - 同一個解析器用在 `routeEstimate.py`、`optimizeVersion.py`（`parse_route_page`）、`getRoute.py`、`verify_metro_bus_route.py`。
  - 後者拿它當 ground truth 比對路線資料庫（見 5-3）。
- **展現的能力**：HTML 結構歸納、可重用的 parser。
- **證據位置**：`scripts/orginal_python_script/252.html`、`74.html`，`optimizeVersion.py` 的 `HtmlParser`，`test/getRoute.py` 的 `_parse_table`。
- **確定程度**：確定。

### 1-3 站名資料的編碼損毀修復

- **遇到的問題**：
  - 早期站名→SLID 資料來自 shapefile（`busstop.dbf`）轉成的 `busstop.csv`。
  - 中文名稱被截斷成亂碼，例如 `捷運台北101/世�`。
  - 3,407 列中有 535 列含 U+FFFD（我重算）。
- **解決用的技術或想法**：
  - 偵測 U+FFFD 替代字元。
  - 對每個損毀的 slid 重新請求 `stoplocation.jsp?slid=…`，解碼後從 `<title>[站名]…` 取回正確站名。
  - 修不好的寫進 `err.txt`。
- **如何驗證**：
  - `busstop_clean.csv` 有 3,299 列，含 U+FFFD 的是 0 列。
  - `err.txt` 108 列，剛好等於 3,407 − 3,299，表示修不好的直接排除。
  - 推算修復成功 427 列。以上皆我重算。
- **展現的能力**：資料清理、找替代資料源補正、留下失敗清單。
- **證據位置**：`consForBeingProductive/test.py`（被註解掉的修復程式）、`busstop.csv`、`busstop_clean.csv`、`err.txt`、`busstop/busstop.dbf`、commit `7425331`。
- **確定程度**：數字確定（我重算）。亂碼成因是 Big5 截斷 → **需要我確認**。

---

## 主題 2：SID 與 SLID 的差異

### 2-1 假設與第一次驗證：一個站名 ≠ 一個站牌

- **遇到的問題**：
  - 同一站名底下有很多 SID，例如「師大分部」在 TDX 有 60 筆 Stop。
  - 若每個 SID 各打一次請求，會大量重複，也分不清方向。
  - 5284 的動態 API（`StopLocationDyna?stoplocationid=`）是以 slid 為鍵，不是 sid。
- **解決用的技術或想法**：
  - 假設：SID 是「某路線在某站的停靠點」；SLID（TDX 的 `StationID`）是「實體站位」。多條路線的 SID 會共用一個 SLID，同一站名的不同 SLID 則代表不同方向或位置的站牌。
  - 寫 `verifySlidMatch.py`，用 TDX OData `$filter=StopName/Zh_tw eq '師大分部'` 查回所有 Stop，檢查 `StationID` 欄位，並確認 1000036 跟 `ntnuBranch.html` 的 slid 一致。
- **如何驗證**：`verifySlidMatch_output.json` 共 60 筆，分到 4 個 StationID：

  | StationID | 筆數 | Bearing |
  |---|---|---|
  | 2430 | 28 | SE |
  | 1000036 | 26 | NW |
  | 1165603295 | 4 | S |
  | 1165603296 | 2 | N |

  - 每個 StationID 有自己的 Bearing 與座標，支持「SLID = 有方向的實體站位」。
  - TDX 的 StationID 1000036 等於 5284 頁面的 slid，證明兩個資料源可以串接。
- **展現的能力**：提出可驗證的假設、跨資料源 ID 對齊。
- **證據位置**：`scripts/orginal_python_script/verifySlidMatch.py`、`verifySlidMatch_output.json`、`ntnuBranch.html`；原始檔時間 2025-11-19（`p2p/`）。
- **確定程度**：資料確定（我重算統計）。

### 2-2 第二次驗證：SID 與 SLID 座標一致性（exact / close / mismatch）

- **遇到的問題**：若要以 SLID 取代 SID 作為去重與定位單位，必須確認「同一 SLID 底下的 SID 座標幾乎相同」，否則會畫錯地圖、算錯距離。
- **解決用的技術或想法**：
  - `verifyGeoClose.py` 比對每個 SID 與所屬 SLID 的座標，容許誤差 0.0003°。
  - 分成完全一致、極微差異、顯著差異三類。
  - 程式寫的判斷門檻是 exact+close > 90% 即「假設成立」。
- **如何驗證**：
  - **你當時的 verifyGeoClose 輸出 → 找不到紀錄**。另外，現存的 `station_id_map.json` 沒有 `sids` 欄位，照原樣執行會得到「沒有找到任何 SID 資料」，可能當時用的是另一版資料檔 → **需要我確認**。
  - 有保留下來的是 `test/record.txt`（2025-12-09）：539 個「衝突案例」，即同一 SLID 下有 ≥2 組不同座標。其中 2 組 491 個、3 組 39 個、4 組 5 個、5 組 4 個。
  - 我用 `stop_id_map.json` 的 `by_sid`（每筆含 slid、lat、lon）重算，以每個 SLID 第一個 SID 的座標為基準：

    | 項目 | 數字 |
    |---|---|
    | SID 總數 | 61,273 |
    | SLID 數 | 11,378 |
    | 完全一致 | 59,620（97.30%） |
    | < 0.0003° | 1,645（2.68%） |
    | 顯著差異 | 8（0.01%） |
    | 含多組座標的 SLID | 539（與 `record.txt` 吻合） |

  - `test/missing_slid_record.txt`／`missing_slid_for_sid_record.txt` 紀錄所有 SID 都有 SLID。
- **展現的能力**：資料驗證、定量化假設、找出例外清單。
- **證據位置**：`scripts/orginal_python_script/verifyGeoClose.py`、`station_id_map.json`、`test/record.txt`、`test/missing_*.txt`。
- **確定程度**：`record.txt` 的 539 是確定。百分比是我重算的近似重現，基準點跟原腳本不同。
- **寫文件時要修正**：程式註解說「0.0001 度約等於 1.1 公尺」「< 1 公尺」是錯的。0.0001° 緯度約 11 公尺，0.0003° 約 33 公尺（經度在北緯 25° 約 30 公尺）。

### 2-3 為何以 SLID 作為去重單位

- **遇到的問題**：查詢起點時要決定打哪些請求。以 SID 為單位會重複；以站名為單位會混淆方向。
- **解決用的技術或想法**：
  - 動態 API 本身以 slid 為鍵；一個 SLID 頁面會列出所有經過該站位的路線。
  - 2-2 顯示同 SLID 的 SID 位置幾乎相同。
  - 因此每個 SLID 只查一次：
    - `optimizeVersion.py` 的 `get_representative_sids` 依 SLID 去重，沒有 SLID 的保留。
    - `busPlanner.ts` 的 `plan()` 用 `slidMap`（slid → 代表 sid）去重後才發請求。
  - 方向判定也用 SLID：`_determine_direction` 先比 SID，再以 SLID 容錯，終點可用 SID／SLID／站名命中。`1336e4b` 加入 `currentSlid !== stopSlid` 就判定方向相反。
- **如何驗證**：
  - 師大分部從 60 個 SID 降到 4 個 SLID 請求，即 4/60（我重算）。
  - `test.py` 的 `audit_907_route`：以「站名順序寬鬆比對」當 ground truth，跟嚴格的 SLID 規劃交叉比對，逐筆列出命中／遺漏與原因（缺 SLID 或 SLID 不符）。**這個稽核的實際輸出 → 找不到紀錄**。
- **展現的能力**：以資料特性決定系統設計單位、設計稽核工具驗證演算法。
- **證據位置**：`optimizeVersion.py`（`get_representative_sids`、`_determine_direction`）、`scripts/orginal_python_script/test.py`、`busPlanner.ts` 的 `plan()` 步驟 2、commit `1336e4b`。
- **確定程度**：程式確定。整個網路的請求減少比例 → 找不到紀錄。

---

## 主題 3：快取設計

### 3-1 只快取靜態路徑比對，不快取到站時間

- **遇到的問題**：
  - 線上驗證版本每次規劃都要抓起點站位頁、終點頁、每條候選路線的 `route.jsp`，經過免費 proxy 很慢。
  - 但到站時間每次都會變。
- **解決用的技術或想法**：
  - Python 的 `RouteCacheRepository`：
    - key 是 `"{起點}|{終點}"`，寫入前移除 `arrival_time_text`、`raw_time`，只存路線、方向、站數、座標、路徑。
    - 命中時呼叫 `update_cached_buses`：只依 SID 分組重抓即時時間，再貼回快取的路徑。
  - 另有一層 per-request 記憶體快取 `_memory_route_cache`（見 4-2）。
  - TS 版：
    - `1fc5fb9` 之前有兩層設計（Level 1 memory route cache + Level 2 disk cache `route_validation_cache_v2`）。
    - 現在用 AsyncStorage key `BUS_ROUTE_CACHE_V2_{start}|{end}`，命中時走 `updateCachedBuses`。
- **如何驗證**：
  - `route_validation_cache.json` 實際內容只有 1 個 key `捷運公館站|師大`，每筆欄位為 `route_name, rid, sid, direction_text, stop_count, start_geo, end_geo, path_stops`，沒有時間欄位。
  - testENV 版是 `捷運淡水站|台電宿舍`。
  - **快取命中前後的耗時或請求數比較 → 找不到紀錄**。
- **展現的能力**：區分資料的變動頻率、快取正確性與效能的取捨。
- **證據位置**：
  - `optimizeVersion.py`：`RouteCacheRepository.set`、`update_cached_buses`、`plan_route`
  - `scripts/orginal_python_script/route_validation_cache.json`、`testENV/route_validation_cache.json`
  - `busPlanner.ts` 的 `plan()`、`updateCachedBuses`
  - `git show 1fc5fb9`（可看到舊的兩層快取被移除）
- **確定程度**：設計確定。「為什麼」的說法是從程式推測 → **需要我確認**。
- **程式觀察（未修改）**：`busPlanner.ts` 的註解寫「Cache without dynamic time」，但實際存進 AsyncStorage 的 `finalBuses` 仍含 `arrivalTimeText`／`rawTime`。因為命中時會被 `updateCachedBuses` 覆寫，所以不會顯示舊時間，但跟 Python 版不一致。另外 `1fc5fb9` 曾把 AsyncStorage 換成 `MockAsyncStorage`（純記憶體）→ 原因 **需要我確認**。

### 3-2 CORS proxy 回傳舊資料

- **找不到紀錄**。
  - commit、程式、筆記中都沒有 cache-busting 參數、stale 偵測或相關測試輸出。
  - 唯一相關的是 proxy 更換歷史（allorigins → corsproxy.io → codetabs，見 6-1），但沒有寫原因。
  - 若你當時有手動比對 proxy 回應的 `UpdateTime` 和官網時間，請提供紀錄或口述 → **需要我確認**。

---

## 主題 4：並行化決策

### 4-1 從同步到 ThreadPool，再到 asyncio.gather

- **遇到的問題**：一次規劃要發很多 HTTP 請求（每個起點站位的 HTML+JSON、終點頁、各路線頁），逐一發送太慢。
- **解決用的技術或想法**：
  - `routeEstimate.py`（約 11-19）：序列式 `requests`。
  - `modifyVersion.py`：改用 `concurrent.futures.ThreadPoolExecutor`。
  - `optimizeVersion.py`（12-05）改成 `aiohttp` + `asyncio.gather`：
    - 同一站的 HTML（路線列表）與 JSON（秒數）並行抓。
    - 「起點所有車次」與「終點經過的路線」兩組請求並行。
    - 候選路線頁並行。
- **如何驗證**：**有沒有實測三個版本的耗時 → 找不到紀錄**。
- **展現的能力**：I/O-bound 問題的並行模型選擇。
- **證據位置**：`modifyVersion.py:123`、`optimizeVersion.py`（`fetch_realtime_at_sid`、`plan_route`）、`p2p/` 檔案時間。
- **確定程度**：程式確定，演進順序從檔案時間推測 → **需要我確認**。

### 4-2 Double-check lock（避免同一路線重複抓取）

- **遇到的問題**：多台候選公車可能屬於同一條路線（RID）。並行時會同時對同一個 `route.jsp?rid=` 發多次請求。
- **解決用的技術或想法**：
  - `get_route_structure`：每個 RID 一把 `asyncio.Lock`。
  - 進鎖前先查 `_memory_route_cache`，進鎖後再查一次（double-check），只有第一個拿到鎖的協程真的發請求。
  - 呼叫端另外先用 `set` 對 RID 去重。
  - TS 版對應做法是 promise 去重：`pendingRouteRequests`，後改為 `routeRequestDedupMap`，2 秒後刪除。
- **如何驗證**：**找不到紀錄**（沒有鎖前／鎖後請求數比較）。
- **展現的能力**：並行下的競態條件、記憶體快取一致性。
- **證據位置**：`optimizeVersion.py` 的 `get_route_structure`（約 426–443 行）；`git show 1fc5fb9`、`git show 2005916`（TS dedup map 的移除）。
- **確定程度**：程式確定。

### 4-3 交集過濾法實驗（Naive vs Intersection）

- **遇到的問題**：暴力法要對「經過起點的所有路線」逐一抓路線頁驗證，請求數很多。
- **解決用的技術或想法**：
  - 先抓終點站頁取得「經過終點的 RID 集合」，跟起點車次的 RID 取交集，只驗證交集內的路線。
  - `experiment.py` 用計數 client（MetricClient）同時記錄 HTTP 請求數與耗時。每次實驗前清空 `_memory_route_cache` 求公平，並檢查兩法找到的 RID 是否一致。
  - 程式也預先寫好一個可能的不一致原因：終點頁資料不完整。
- **如何驗證**：**實驗執行結果（請求數、秒數）→ 找不到紀錄**。腳本還在，可重跑，但需要連網且依賴 proxy，所以這次沒跑。
- **展現的能力**：設計公平的 A/B 實驗、同時看正確性與成本。
- **證據位置**：`GEProject/test/experiment.py`（2025-12-09）。
- **確定程度**：實驗設計確定，結果找不到。

### 4-4 分批 Promise.all（限流 5）

- **遇到的問題**：App 端經過公共 CORS proxy，一次發太多請求可能被限流或失敗。
- **解決用的技術或想法**：
  - Python `busPlanner.py` 用 `MAX_CONCURRENT_REQUESTS = 5` 分批 `asyncio.gather`。
  - TS 的 `batchProcess` 以 5 個一批 `Promise.all`，註解「模擬 Python 的 asyncio + batch logic」。
- **如何驗證**：**找不到紀錄**。選 5 的依據 → **需要我確認**。
- **展現的能力**：在併發量與外部服務限制之間取捨。
- **證據位置**：`busPlanner.ts` 的 `CONFIG.MAX_CONCURRENT_REQUESTS`、`batchProcess`；`test/busPlanner.py:40, 433-438`；commit `2005916`。
- **確定程度**：程式確定，動機需確認。
- **可補充的反思（我的建議，不是紀錄）**：分批模式下，每批要等最慢的那個請求才能進下一批。若面試被問，可以提到 semaphore 或 worker pool 的改進方向。

---

## 主題 5：從線上驗證改為離線預建資料庫

### 5-1 為何改離線

- **遇到的問題**：線上版每次規劃都要即時抓路線頁判斷「先經過起點、後經過終點」。請求多、受 proxy 穩定度影響，快取也只對重複查詢有效。
- **解決用的技術或想法**：
  - `2005916`「busPlanner optimize」（2025-12-10）把路線站序預先建成 `metro_bus_routes.json`。
  - `findStaticRoutes` 在本機用 SID 集合比對 `startIdx < endIdx`。
  - 網路請求只剩「去重後的 SLID 即時時間」。
  - 同一個 commit 也移除了 `getRouteStructure`／`routeRequestDedupMap`／`route.jsp` 抓取。
- **如何驗證**：**改版前後耗時與請求數比較 → 找不到紀錄**。
- **展現的能力**：把「每次重算」改成「離線預算」的架構取捨。
- **證據位置**：`git show 2005916`、`busPlanner.ts` 的 `findStaticRoutes`、`test/busPlanner.py`、`test/readme.md`（流程說明）。
- **確定程度**：架構變更確定，動機從程式推測 → **需要我確認**。

### 5-2 `stop_id_map.json` → `stop_id_map_v3.json`

- **遇到的問題**：站點資料要打包進 App，但原始檔有 10.9 MB。
- **解決用的技術或想法**：
  1. `fetchStopData.py`：用 TDX OAuth2 client credentials（金鑰 [已遮蔽]）下載 Taipei、NewTaipei 的 `Bus/Stop`，`$select=StopName,StopUID,StopPosition,StationID`。
     - 用 regex 去掉 UID 前綴（TPE/NWT）得到 SID。
     - 建立 `by_sid`（sid → lat、lon、name、slid、full_uid）與 `by_name`（name → [sid]）。
  2. `check_slid.py` 先統計相異座標數，確認重複程度。
  3. `gen_new_database.py` 做「座標池化」：
     - 相同 (lat, lon) 只存一次於 `g`。
     - `s[sid] = [name, slid, geoIndex]`，`n[name] = [sid…]`。
     - 用緊湊的 JSON separators。
- **如何驗證**（我重算）：

  | 項目 | stop_id_map.json | stop_id_map_v3.json |
  |---|---|---|
  | 檔案大小 | 10,920,071 bytes | 3,443,185 bytes（−68.5%） |
  | SID 數 | 61,273 | 61,273 |
  | 站名數 | 6,094 | 6,094 |
  | 座標 | 每個 SID 各存一份 | 11,948 組唯一座標 |
  | 空 SLID | — | 0 |
  | 無座標 | — | 0 |

  - 檔案時間：`stop_id_map.json` 於 2025-11-20 進 repo（`8c811af`）；v3 生成於 2025-12-09，進 repo 於 `2005916`／`52b25e1`。
- **展現的能力**：資料格式設計、去重壓縮、行動端資源考量。
- **證據位置**：`scripts/orginal_python_script/fetchStopData.py`、`test/check_slid.py`、`test/gen_new_database.py`、`test/stop_id_map_v3_readme.md`、`databases/`。
- **確定程度**：確定。
- **附註**：`app/search.tsx`、`app/route.tsx`（非你負責）仍 import 10.9 MB 的舊 `stop_id_map.json`，所以 bundle 裡兩份都在。

### 5-3 `metro_bus_routes.json` 的兩代產生方式

- **遇到的問題**：需要每條路線、每個方向的正確站序（SID 序列）。
- **解決用的技術或想法**：
  - **第一代（TDX）**：`buildRoute.md`（2025-12-09）記錄 V1–V6 演進：

    | 版本 | 遇到的問題 | 做法 |
    |---|---|---|
    | V2 | 站序未排序 | 依 `StopSequence` 排序 |
    | V3 | 886 缺站 | 取站數最多的變體 |
    | V4 | 685 平行路徑 | Kahn 拓撲排序 |
    | V5 | 595 繞駛站被擠到尾端 | 插值權重 |
    | V6 | 872、128 逆向邊造成迴圈 | 骨架權威剪邊 + 死結強制救援 |

  - **驗證工具**：`verify_metro_bus_route.py` 抓 5284 的 `route.jsp` 當 ground truth，用 `difflib.SequenceMatcher` 逐站列出 equal／replace／delete／insert。目標路線：837區、128、872、685、303、595、927、林口-捷運府中站。
  - **第二代（直接爬 5284）**：`test/getRoute.py`（2025-12-13）`Robust5284Fetcher`：
    - 以現有 1,034 個 RID 為清單，逐一爬 `route.jsp`。
    - JSONL checkpoint 每筆 `fsync`，可中斷續傳。
    - 5xx 或連線錯誤最多重試 3 次，退避間隔遞增。
    - 隨機延遲 0.5–1.5 秒、輪替 User-Agent。
    - 「查無資料」不寫入。
- **如何驗證**（我重算）：
  - `0da3a33`「fix routes database」（2025-12-14）把檔案從 1,207,191 bytes 換成 1,211,709 bytes。
  - 兩版都是 1,869 個「RID×方向」、1,034 個 RID，但有 **214 個方向的站序不同**。
  - 新檔與 `test/checkpoint_5284.jsonl`（1,034 行）展開後 1,869/1,869 完全一致，證明新檔就是爬蟲產物。
  - **verify_metro_bus_route.py 當時的 diff 輸出 → 找不到紀錄**。
- **展現的能力**：圖論演算法應用、以獨立來源驗證資料、發現演算法修補有極限後改換資料源、穩健爬蟲（斷點續傳、退避、禮貌爬取）。
- **證據位置**：`test/buildRoute.md`、`test/verify_metro_bus_route.py`、`test/getRoute.py`、`test/checkpoint_5284.jsonl`、`databases/metro_bus_routes.json`、commit `0da3a33`、`52b25e1`（第一代進 repo）。
- **確定程度**：檔案與數字確定（我重算）。以下需要我確認：
  - 「為什麼放棄 TDX 改用 5284」：推測是即時資料來自 5284，SID 序列要與它一致。
  - `buildRoute.md` 的 V1–V6 是否都是你實作。
  - 第一代 TDX 版 `getRoute.py` 的獨立檔案找不到，只有 `buildRoute.md` 內嵌的程式碼。

---

## 主題 6：後端改為用戶端

- **Vercel 流量額度考量 → 找不到紀錄**。
  - git 歷史沒有任何 `api/` 或 serverless 後端程式。
  - `vercel.json` 只有靜態 build 設定。
  - 出現 "Vercel Function" 的是隊友的推播 GitHub workflow，不屬查詢功能。
- **原生平台直連失敗的實際錯誤訊息 → 找不到紀錄**。

**找得到的相關事實**

### 6-1 Web 版 CORS 與 proxy 演進

- **遇到的問題**：瀏覽器直接 fetch `pda5284.gov.taipei` 會被 CORS 擋。
- **解決用的技術或想法**：依序換了三個公共 proxy：

  | 日期 | commit | proxy |
  |---|---|---|
  | 2025-10-09 | `af327ba`「fix CORS bug」 | `api.allorigins.win/raw?url=` |
  | 2025-11-20 | `8c811af` | `corsproxy.io/?` |
  | 2025-12-06 | `1fc5fb9`「fix cors problem」 | `api.codetabs.com/v1/proxy?quest=` |

  - 另外 `4f37e46`「fix header problem」移除了 fetch 的自訂 `User-Agent` header。推測原因是瀏覽器禁止設定此 header，或它觸發了 preflight → **需要我確認**。
- **如何驗證**：**找不到紀錄**。
- **證據位置**：上列 commit；`busPlanner.ts` 的 `CONFIG.BASE_URL`；`bus-api.ts:103,143`（仍是 corsproxy.io，與 planner 不一致）。
- **確定程度**：更換順序確定。每次更換的原因 → **需要我確認**。

### 6-2 Python 原型移植到 TypeScript 用戶端

- **事實**：
  - Python 版（`optimizeVersion.py`、`busPlanner.py`）的邏輯被移植到 `busPlanner.ts`，直接在 App／瀏覽器執行。註解多處寫「與 Python 對齊」「模擬 Python 的 asyncio」。
  - `scripts/orginal_python_script/testENV/` 是用 Node + axios 跑 `busPlanner.ts` 的測試環境。Node 沒有 CORS 限制，推測用來分離「邏輯錯誤」與「CORS 錯誤」→ **需要我確認**。
  - `1336e4b` 的註解寫「若為純 Native 環境可移除 corsproxy.io 前綴」，但現行程式在所有平台都走 proxy。原生直連當時是否試過、錯誤是什麼 → **需要我確認**。

---

## 主題 7：修過的 bug 與踩過的坑

| # | 問題 | 修法 | 證據 | 確定程度 |
|---|---|---|---|---|
| 7-1 | 同一路線多次經過起點站名（例如繞行，在第 5 站與第 20 站都經過「淡水」），找到第一組配對就 `break`，漏掉其他合法上車點 | 移除 `break`，對每個 startIndex 找其後最近的 endIndex，全部列為候選 | `busPlanner.ts` `findStaticRoutes`「修正：移除 break」，首次出現於 `2005916`（你）；Python 端類似修正見 `modifyVersion.py`「[修正版] 支援多重站點出現的距離計算邏輯」 | 確定。實際觸發的路線 → 需要我確認 |
| 7-2 | 「more to one selection problem」：同站名多個 SID 被重複選入，方向判斷錯誤 | 依 SLID 選代表 SID；`currentSlid !== stopSlid` 視為反方向；`seenRoutes` 依 RID 去重 | `1336e4b`（2025-12-04） | 修法確定，原始症狀描述 → 需要我確認 |
| 7-3 | 排序錯誤：「進站中」「將到站」與分鐘數排序錯亂 | `TimeParser.parse_text_to_seconds`：進站中 = −1、將到站 = 0、N 分 = N×60、未發車 = 99999 | `scripts/orginal_python_script/test.py`、`test/test.py` 的 assert；`1336e4b` 註解「修正排序問題」 | 確定 |
| 7-4 | 與隊友介面不一致：函式名 `fetchRealtimeAtSid` 與呼叫端不符 | 改名為 `fetchBusesAtSid` | `e10baea`（2025-12-05） | 確定 |
| 7-5 | 回傳欄位由 snake_case（`route_name`、`path_stops`）改成 camelCase（`routeName`、`pathStops`）後，隊友的 `map.native.tsx`、`route.tsx` 壞掉 | 由你修正呼叫端 | `93a50c1`「fix service call」（2025-12-06） | 確定 |
| 7-6 | 自訂 `User-Agent` header 造成 Web 端請求問題 | 移除 header | `4f37e46` | 修法確定，錯誤訊息找不到 |
| 7-7 | 早期 `stop_to_slid.json` 是「站名 → 單一 slid」的 dict：3,299 列只剩 1,829 個 key，**1,290 個站名有多個 slid 被覆蓋**，等於只看得到一個方向（我重算） | 之後改用 `by_sid` 與 SLID 模型（2-3） | `consForBeingProductive/test.py`、`databases/stop_to_slid.json`、`bus-api.ts:68` | 數字確定。你當時是否因此發現問題 → 需要我確認 |
| 7-8 | Shapefile 站名亂碼 535 列 | 見 1-3 | 同 1-3 | 確定 |
| 7-9 | TDX 合併的路線站序錯誤（214 個方向） | 見 5-3 | `0da3a33` | 確定 |

另外，`c80a27c`「fix legacy overwrite bug」（12-17）雖然改了 `busPlanner.ts`，但落在 BusPulse 時期。是否屬於查詢功能 → **需要我確認**，目前先排除。

---

## 主題 8：與隊友的協作介面

- **遇到的問題**：前端（主要是 Fizzy、vitenn）需要穩定的查詢介面。
- **解決用的技術或想法**：
  - 對外介面是 `new BusPlannerService().plan(startName, endName): Promise<BusInfo[]>`。
    - `BusInfo` 含 `routeName, rid, sid, arrivalTimeText, rawTime, arrivals, directionText, stopCount, estimatedDuration, startGeo, endGeo, pathStops`。
  - 另有 `getStopArrivals(stopName)`、`getArrivalsBySlid(slid, stopName)`、`fetchBusesAtSid(sid)` 供站牌頁使用。
  - 呼叫端：
    - `app/index.tsx`：首頁初次載入與自動更新都呼叫 `plan()`
    - `app/map.native.tsx`：`plan('師大分部','師大')`
    - `app/stop.tsx`
    - `app/ride.tsx`（BusPulse，不在範圍）
  - 早期 `bus-api.ts` 的 `TaipeiBusAPI.getStopEstimates(stopName)` 附了 718 行的 `documentation/bus-api.md`（`7425331`）。`8c811af` 之後 app 改用 BusPlannerService，`bus-api.ts` 目前沒有被 import。
- **整合問題**：
  - 7-4 函式名不符（`e10baea`）、7-5 欄位命名改動（`93a50c1`）。
  - 隊友直接修改 `busPlanner.ts` 加功能：`d73fd47`／`c569401` 路線時間估算、`a3a8919` 方向、`08b1ec2` 最近站牌、`e39db01` 大改、`52b25e1` 合併。
  - Copilot 自動解衝突的 PR #16 被 dillen revert（#18）。
- **溝通紀錄（LINE、會議、PR 討論）→ 找不到紀錄**。PR 只有標題，沒有討論內容。
- **展現的能力**：定義服務介面、文件化、修復整合問題。
- **證據位置**：上列檔案與 commit。
- **確定程度**：程式與 commit 確定。分工與溝通方式 → **需要我確認**。
- **可反思（我的建議）**：7-5 是改回傳型別時沒同步呼叫端，可以談「應該用共享的 TypeScript interface 或型別檢查來避免」。

---

## 主題 9：嘗試過但放棄的做法

| 放棄的做法 | 換成 | 證據 | 原因 |
|---|---|---|---|
| 站名 → 單一 slid 對照表（`stop_to_slid.json`，來自 shapefile） | TDX 的 `by_sid`／SLID 模型 | 7-7、`8c811af` | 從程式推測：無法表示多方向站牌 |
| 線上驗證路線（每次抓 `route.jsp`）+ 記憶體／磁碟兩層快取 + promise 去重 | 離線 `metro_bus_routes.json` | `1fc5fb9`、`2005916` | 需要我確認 |
| 用 TDX 資料以拓撲排序合併路線變體（V1–V6） | 直接爬 5284 路線頁 | `buildRoute.md`、`0da3a33` | 需要我確認 |
| 序列 requests → ThreadPoolExecutor | aiohttp + asyncio | `routeEstimate.py`、`modifyVersion.py`、`optimizeVersion.py` | 需要我確認 |
| CORS proxy：allorigins、corsproxy.io | codetabs | 6-1 | 找不到紀錄 |
| fetch 帶自訂 User-Agent | 移除 | `4f37e46` | 需要我確認 |
| AsyncStorage | 暫時換成 `MockAsyncStorage`；現行版又改回 AsyncStorage | `1fc5fb9` | 需要我確認 |

---

## 找不到紀錄（彙整）

1. 先前與 AI 的對話紀錄：本機只有這次 session。
2. `verifyGeoClose.py` 的原始輸出，以及當時使用的含 `sids` 欄位版本的 `station_id_map.json`。
3. CORS proxy 回傳舊資料的測試。
4. 任何耗時或請求數的前後實測：Naive vs Intersection、快取命中前後、線上 vs 離線、加鎖前後。
5. `verify_metro_bus_route.py` 與 `test.py`（907 稽核）的執行輸出。
6. Vercel 流量額度的考量，以及後端程式的存在。
7. 原生平台直連的錯誤訊息。
8. 與隊友的溝通紀錄。

## 需要我確認（彙整）

1. 哪些程式由你親手撰寫、哪些是 AI（Gemini 等）協助重構或產生，特別是 `optimizeVersion.py`、`buildRoute.md`、`verification.py`。
2. 你怎麼發現 tte↔JSON 的對應（DevTools？讀 `ajax2.js`？）。
3. shapefile 亂碼的成因（Big5 截斷？）。
4. 選 SLID 的理由是否如 2-3 所述；verifyGeoClose 當時的實際數字。
5. 每次換 proxy 的原因、移除 User-Agent 的錯誤訊息、改用 `MockAsyncStorage` 的原因。
6. 並行上限 5 的依據。
7. 改離線資料庫、改爬 5284 的動機，以及是否有前後效能數據。
8. 7-1、7-2 實際遇到的路線或症狀。
9. Vercel 或原生直連的經過，若有請補充截圖或回憶。

## 安全提醒

- commit `fe0c43f`（2025-10-02 推上 `origin/last_two`）的 `fetchStopData.py`／`verifySlidMatch.py` 含**明文 TDX CLIENT_SECRET [已遮蔽]**。`ca379bd` 雖改成環境變數，但**舊值仍留在 git 歷史中**。
  - commit 訊息寫「renew」，請確認舊 secret 已在 TDX 後台作廢。
  - 若 repo 是公開的，可考慮改寫歷史。
- 另外 `GEProject/plan.txt`（repo 外）有一行明文 API 金鑰 [已遮蔽]，建議移除並撤銷。
