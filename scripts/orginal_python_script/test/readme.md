# 🚌 Taipei Bus Planner (busPlanner) 技術文件

**版本**：v1.0
**適用程式碼**：`busPlanner.py`
**相依資料**：`metro_bus_routes.json`, `stop_id_map_v3.json`

## 1\. 資料架構說明 (Data Schema)

本系統依賴兩個靜態 JSON 檔案來達成「離線路徑匹配」與「高效資料查詢」。

### A. 路線拓撲資料庫 (`metro_bus_routes.json`)

由 TDX API 經演算法清洗後的標準化路線檔，解決了路線變體與順序錯亂問題。

  * **用途**：提供靜態的路線站序，用於判斷「A站到B站有哪些公車經過」。
  * **結構 (List of Objects)**：
    ```json
    {
      "route_name": "303區",       // (Str) 路線名稱
      "rid": "10272",              // (Str) 路線唯一碼 (RouteUID)
      "direction": 0,              // (Int) 0=去程, 1=返程
      "stops_sid": [               // (List[Str]) 依序排列的站點 SID
        "20647", "57654", ...
      ]
    }
    ```

### B. 站點索引資料庫 (`stop_id_map_v3.json`)

採用 **座標池化 (Geo Pooling)** 技術壓縮的站點資料庫。

  * **用途**：快速查詢站名 (Name) 對應的 ID (SID)，以及 SID 對應的座標與 SLID (Stop Location ID)。
  * **結構**：
      * **`g` (Geo Pool)**: `[[Lat, Lon], ...]` 浮點數座標陣列 (共用池)。
      * **`n` (Name Index)**: `{ "站名": ["SID1", "SID2"] }` 用於模糊搜尋與站名解析。
      * **`s` (Stops)**: `{ "SID": ["站名", "SLID", GeoIndex] }`
          * `SLID`: 用於爬取動態時間的 ID。
          * `GeoIndex`: 指向 `g` 陣列的索引值 (若為 `-1` 則無座標)。

-----

## 2\. 核心模組 API 說明 (Core Modules)

`busPlanner.py` 採用 **Repository-Service** 模式設計，以下為主要類別的使用方式。

### 2.1 資料存取層 (Repositories)

#### `StopRepository`

負責讀取 `stop_id_map_v3.json`。

  * **初始化**: `repo = StopRepository(Path('stop_id_map_v3.json'))`
  * **主要方法**:
      * `get_sids_by_name(name: str) -> List[str]`:
          * 輸入站名 (如 "捷運公館站")，回傳所有可能的 SID 列表。
      * `get_info(sid: str) -> Optional[Dict]`:
          * 回傳 `{'name': str, 'slid': str, 'lat': float, 'lon': float}`。
          * **注意**: 若 SLID 為空字串，代表該站點可能無法查詢動態。

#### `StaticRouteRepository`

負責讀取 `metro_bus_routes.json` 並執行集合運算。

  * **初始化**: `static_repo = StaticRouteRepository(Path('metro_bus_routes.json'))`
  * **主要方法**:
      * `find_routes_between(start_sids: Set[str], end_sids: Set[str]) -> List[Dict]`:
          * **輸入**: 起點與終點的所有可能 SID 集合。
          * **輸出**: 符合「先經過 Start 且後續經過 End」的路線物件列表。
          * **擴充欄位**: 回傳物件會多出 `match_range: (start_index, end_index)`，標示搭乘區間在 `stops_sid` 中的索引位置。

### 2.2 業務邏輯層 (Service)

#### `BusPlannerService`

系統入口，整合靜態匹配與動態爬蟲。

  * **初始化**: `service = BusPlannerService()` (預設使用 Config 路徑)
  * **主要方法**:
      * `async plan_route(start_name: str, end_name: str) -> List[BusInfo]`:
          * **輸入**: 起點站名、終點站名 (支援模糊字串，需對應 `stop_id_map` 的 Key)。
          * **輸出**: `BusInfo` 物件列表 (已按到站時間排序)。

### 2.3 資料模型 (Return Object)

#### `BusInfo` (Dataclass)

`plan_route` 回傳的最終物件結構：

| 欄位 | 類型 | 說明 |
| :--- | :--- | :--- |
| `route_name` | `str` | 路線名稱 (如 "307") |
| `arrival_time_text` | `str` |顯示文字 (如 "3分", "進站中", "未發車") |
| `raw_time` | `int` | 秒數，用於排序 (-1: 進站中, 99999: 未發車) |
| `direction_text` | `str` | 去程/返程 |
| `path_stops` | `List[StopInfo]` | 實際經過的站點列表 (含座標)，可用於繪製地圖 |
| `start_geo` / `end_geo` | `GeoLocation` | 起點與終點座標 |

-----

## 3\. 程式執行流程 (Workflow)

當呼叫 `service.plan_route("A", "B")` 時，內部執行步驟如下：

1.  **解析 SID**：查詢 `StopRepository` 將 A, B 轉為 `Set(SID)`。
2.  **靜態過濾**：呼叫 `StaticRouteRepository` 找出所有連結 A $\to$ B 的路線變體 (Rid)。
3.  **解析 SLID**：從篩選出的路線起點 SID，反查出需要爬取的 `SLID` (Stop Location ID)。
4.  **動態並發**：使用 `TaipeiBusClient` 批次非同步爬取這些 SLID 的即時動態 (HTML/JSON)。
5.  **資料合併**：將靜態路線資料與動態到站時間 (Arrival Time) 結合。
6.  **快取 (Cache)**：寫入 `route_validation_cache.json`，供短時間內重複查詢使用。

-----

## 4\. 快速上手 (Quick Start)

```python
import asyncio
from busPlanner import BusPlannerService

async def main():
    # 1. 初始化 Service
    service = BusPlannerService()
    
    # 2. 執行規劃 (非同步)
    print("正在查詢 捷運公館站 -> 師大 ...")
    buses = await service.plan_route("捷運公館站", "師大")
    
    # 3. 處理結果
    if not buses:
        print("查無路線")
        return

    # 4. 輸出資訊
    for bus in buses:
        print(f"[{bus.route_name}] {bus.direction_text}")
        print(f"狀態: {bus.arrival_time_text}")
        print(f"經過站數: {bus.stop_count}")
        print("-" * 20)

if __name__ == "__main__":
    asyncio.run(main())
```

### 常見問題與限制

  * **資料檔案缺失**：若無 json 檔，Service 會自動回傳空陣列並 Log Error。
  * **站名模糊匹配**：目前僅支援 `stop_id_map_v3.json` 內的精確 Key 查詢，未實作 Fuzzy Search。
  * **代理限制**：程式預設使用 `codetabs` 代理存取 `pda5284.gov.taipei`，若代理失效需更換 `BASE_URL`。