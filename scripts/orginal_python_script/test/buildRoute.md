# 🚌 雙北公車路線拓撲整合技術筆記 (TDX Route Topology Integration)

**版本**：v6.0 (Final Stable)
**日期**：2025-12-09
**作者**：Solution Architect / Problem Solver

## 1\. 專案目標

從 TDX Transport API 取得雙北（台北市、新北市）所有公車路線的完整站序資料。解決 API 資料破碎化（SubRoutes）、路線變體繁多、以及不同變體間順序衝突的問題，產出一個「最大聯集且順序正確」的標準化 JSON 檔。

## 2\. 演算法演進歷程

在開發 `getRoute.py` 的過程中，我們針對真實世界的資料髒亂與複雜度，進行了六次主要的邏輯迭代：

| 版本 | 遭遇問題 | 解決方案 | 核心技術 |
| :--- | :--- | :--- | :--- |
| **V1** | 基礎實作 | 直接呼叫 API 並儲存。 | Simple Fetch |
| **V2** | **站序錯亂**：API 回傳的 `Stops` 陣列未必依照 `StopSequence` 排序。 | 加入強制排序邏輯。 | `sorted(key='StopSequence')` |
| **V3** | **缺站 (Route 886)**：只抓到「區間車」或「短變體」，導致繞駛站點遺失。 | 實作「最大路徑策略」，同路線取站數最多者。 | Max-Length Selection |
| **V4** | **拓撲錯亂 (Route 685)**：平行路徑（直達 vs 繞駛）導致合併後順序跳動。 | 捨棄單純插入，改用**圖論拓撲排序**建立全域依賴。 | Kahn's Algorithm (Topological Sort) |
| **V5** | **優先級錯置 (Route 595)**：繞駛站點因無權重，被排序演算法擠到路線末端。 | 引入**插值權重 (Interpolated Priority)**，為新站點計算虛擬位置。 | Linear Interpolation |
| **V6** | **死結與逆向 (Route 872, 128)**：資料庫存在逆向邊導致迴圈 (Cycle) 或死結。 | 加入**骨架權威 (斬斷逆向邊)** 與 **死結強制救援**。 | Cycle Detection & Rescue |

-----

## 3\. 核心邏輯詳解 (V6 Architecture)

### A. 動態權重插值 (Dynamic Priority Interpolation)

為了解決拓撲排序中「平行節點」誰先誰後的問題，我們先選出最長變體作為「骨架 (Skeleton)」，並賦予整數權重 ($0, 1, 2...$)。
對於夾在骨架站點 $A(10)$ 與 $B(11)$ 之間的新站點 $X, Y$，我們給予小數權重 ($10.33, 10.66$)，確保它們乖乖排在中間。

### B. 骨架權威與逆向過濾 (Skeleton Authority)

若一條變體宣稱順序是 $B \to A$，但骨架已經定義 $A \to B$。我們信任骨架（因為它是最長的主線），視 $B \to A$ 為雜訊並將其邊 (Edge) 剪斷，防止圖中出現迴圈 (Loop)。

### C. 殘局救援 (Deadlock Rescue)

當 Kahn 演算法的 Queue 清空，但圖中仍有節點未輸出時（代表存在無法解開的微小迴圈），系統會強制挑選優先級最高（權重數字最小）的孤兒節點強行輸出，避免資料遺失。

-----

## 4\. 程式碼檔案庫

### 4.1 資料產出核心：`getRoute.py` (V6 Final)

```python
import requests
import json
import re
import time
from collections import defaultdict

# =================設定區=================
CLIENT_ID = 'YOUR_CLIENT_ID'          # 請替換為您的 ID
CLIENT_SECRET = 'YOUR_CLIENT_SECRET'  # 請替換為您的 Secret
OUTPUT_FILENAME = 'metro_bus_routes.json'
# =======================================

class TDXFetcher:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
        self.token = None

    def get_token(self):
        headers = {'content-type': 'application/x-www-form-urlencoded'}
        data = {
            'grant_type': 'client_credentials',
            'client_id': self.client_id,
            'client_secret': self.client_secret
        }
        try:
            response = requests.post(self.auth_url, headers=headers, data=data)
            response.raise_for_status()
            self.token = response.json()['access_token']
            print("✅ 成功取得 Access Token")
        except Exception as e:
            print(f"❌ 取得 Token 失敗: {e}")
            exit(1)

    def fetch_routes(self, city):
        if not self.token:
            self.get_token()
        # RouteUID: 路線唯一碼, Direction: 0去/1返
        url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/StopOfRoute/City/{city}?$select=RouteName,RouteUID,Direction,Stops&$format=JSON"
        headers = {'Authorization': f'Bearer {self.token}', 'Accept-Encoding': 'gzip'}
        print(f"📥 正在下載 {city} 公車資料...")
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ 下載 {city} 失敗: {e}")
            return []

    def clean_sid(self, raw_sid):
        """移除 SID 中的非數字字元 (例如 TPE123 -> 123)"""
        return re.sub(r'\D', '', str(raw_sid))

    def calculate_dynamic_priority(self, variants, longest_variant_sids):
        """
        [V5] 插值權重計算：為不在骨架上的站點計算虛擬分數
        """
        priority_map = {sid: float(i) for i, sid in enumerate(longest_variant_sids)}
        
        # 執行兩次掃描確保權重傳遞
        for _ in range(2):
            for route in variants:
                raw_stops = sorted(route.get('Stops', []), key=lambda x: x.get('StopSequence', 0))
                sids = [self.clean_sid(s['StopUID']) for s in raw_stops]
                
                i = 0
                while i < len(sids):
                    if sids[i] in priority_map:
                        i += 1
                        continue
                    
                    # 尋找區間 [Known_Prev ... Unknowns ... Known_Next]
                    prev_known_idx = i - 1
                    prev_score = priority_map[sids[prev_known_idx]] if prev_known_idx >= 0 else -1.0
                    
                    j = i
                    while j < len(sids) and sids[j] not in priority_map:
                        j += 1
                    
                    next_known_idx = j
                    next_score = priority_map[sids[next_known_idx]] if next_known_idx < len(sids) else prev_score + 100.0
                    
                    # 線性插值
                    gap_size = j - i
                    step = (next_score - prev_score) / (gap_size + 1)
                    
                    for k in range(gap_size):
                        priority_map[sids[i + k]] = prev_score + step * (k + 1)
                    i = j
        return priority_map

    def topological_sort_stops(self, variants):
        """
        [V6] 拓撲排序 + 逆向斬斷 + 死結救援
        """
        # 1. 準備骨架資料
        variants.sort(key=lambda x: len(x.get('Stops', [])), reverse=True)
        longest_variant_sids = []
        if variants:
            raw = sorted(variants[0].get('Stops', []), key=lambda x: x.get('StopSequence', 0))
            longest_variant_sids = [self.clean_sid(s['StopUID']) for s in raw]
        
        skeleton_indices = {sid: i for i, sid in enumerate(longest_variant_sids)}
        node_priority = self.calculate_dynamic_priority(variants, longest_variant_sids)

        # 2. 建圖 (加入逆向過濾)
        graph = defaultdict(set)
        in_degree = defaultdict(int)
        all_nodes = set()
        
        for route in variants:
            raw_stops = sorted(route.get('Stops', []), key=lambda x: x.get('StopSequence', 0))
            sids = [self.clean_sid(s['StopUID']) for s in raw_stops]
            
            for i in range(len(sids) - 1):
                u, v = sids[i], sids[i+1]
                all_nodes.add(u)
                all_nodes.add(v)
                
                # [V6] 斬斷逆向邊 (Skeleton Authority)
                if u in skeleton_indices and v in skeleton_indices:
                    if skeleton_indices[u] > skeleton_indices[v]:
                        continue 

                if v not in graph[u]:
                    graph[u].add(v)
                    in_degree[v] += 1
                if u not in in_degree: in_degree[u] = 0

        # 3. 執行排序
        queue = [n for n in all_nodes if in_degree[n] == 0]
        # 關鍵：依據插值權重排序佇列
        queue.sort(key=lambda x: node_priority.get(x, float('inf')))
        
        result = []
        
        while len(result) < len(all_nodes):
            if not queue:
                # [V6] 死結救援 (Cycle Rescue)
                remaining = [n for n in all_nodes if n not in result]
                if not remaining: break
                rescue_node = min(remaining, key=lambda x: node_priority.get(x, float('inf')))
                queue.append(rescue_node)
                # 模擬斷開指向救援點的邊
                in_degree[rescue_node] = 0 

            u = queue.pop(0)
            result.append(u)
            
            neighbors = sorted(list(graph[u]), key=lambda x: node_priority.get(x, float('inf')))
            
            for v in neighbors:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)
            
            queue.sort(key=lambda x: node_priority.get(x, float('inf')))
            
        return result

    def process_data(self, raw_data_list):
        grouped_data = defaultdict(list)
        print("🔄 正在構建路線拓撲網路 (Graph Topology V6)...")

        # 依據 RID+Direction 分組
        for route in raw_data_list:
            clean_rid = self.clean_sid(route['RouteUID'])
            direction_code = route.get('Direction', 0)
            key = (clean_rid, direction_code)
            grouped_data[key].append(route)

        final_routes = []

        for key, variants in grouped_data.items():
            rid, direction = key
            # 對每一組執行拓撲合併
            sorted_sids = self.topological_sort_stops(variants)
            
            route_name = variants[0]['RouteName']['Zh_tw']
            direction_map = {0: '去程', 1: '返程', 2: '迴圈', 255: '未知'}
            
            route_obj = {
                "route_name": route_name,
                "rid": rid,
                "direction": direction,
                "direction_text": direction_map.get(direction, '未知'),
                "stops_sid": sorted_sids
            }
            final_routes.append(route_obj)

        return final_routes

def main():
    start_time = time.time()
    fetcher = TDXFetcher(CLIENT_ID, CLIENT_SECRET)
    
    taipei_data = fetcher.fetch_routes("Taipei")
    new_taipei_data = fetcher.fetch_routes("NewTaipei")
    all_raw_data = taipei_data + new_taipei_data
    
    print(f"📊 原始資料共 {len(all_raw_data)} 筆變體")

    final_result = fetcher.process_data(all_raw_data)

    with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
        json.dump(final_result, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 處理完成！最終輸出 {len(final_result)} 條路線")
    print(f"⏱️  耗時: {time.time() - start_time:.2f} 秒")

if __name__ == "__main__":
    main()
```

### 4.2 驗證工具：`verify_routes.py` (Visual Diff)

用於比對 JSON 產出結果與 5284 網頁資料的差異，支援視覺化對齊。

```python
import json
import random
import re
import requests
import logging
import difflib
from bs4 import BeautifulSoup
from urllib.parse import parse_qs, urlparse

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("Verifier")

class HtmlParser:
    @staticmethod
    def parse_route_page(html: str):
        soup = BeautifulSoup(html, 'html.parser')
        def _extract_stops(class_regex: str):
            stops = []
            for row in soup.find_all('tr', class_=re.compile(class_regex)):
                link = row.find('a')
                if not link: continue
                qs = parse_qs(urlparse(link.get('href', '')).query)
                clean_sid = re.sub(r'\D', '', str(qs.get('sid', [''])[0]))
                stops.append({'name': link.text.strip(), 'sid': clean_sid})
            return stops
        return {'go_stops': _extract_stops(r'ttego\d+'), 'back_stops': _extract_stops(r'tteback\d+')}

def print_diff_table(route_name, json_sids, web_stops):
    web_sids = [s['sid'] for s in web_stops]
    matcher = difflib.SequenceMatcher(None, json_sids, web_sids)
    
    print(f"\n🚫 [差異分析] 路線: {route_name}")
    print(f"   JSON 數量: {len(json_sids)} | WEB 數量: {len(web_sids)}")
    print("-" * 85)
    print(f"{'IDX':<4} | {'JSON SID':<10} | {'狀態':^6} | {'WEB SID':<10} | {'WEB 站名 (參考)':<20}")
    print("-" * 85)

    web_idx = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        j_segment = json_sids[i1:i2]
        w_segment = web_stops[j1:j2]
        max_len = max(len(j_segment), len(w_segment))
        
        for k in range(max_len):
            j_val = j_segment[k] if k < len(j_segment) else ""
            w_obj = w_segment[k] if k < len(w_segment) else None
            w_val = w_obj['sid'] if w_obj else ""
            w_name = w_obj['name'] if w_obj else ""
            
            status = ""
            if tag == 'equal': status = "== "
            elif tag == 'replace': status = "!= "
            elif tag == 'delete': 
                status = ">> "
                w_val, w_name = "---", "(API 獨有)"
            elif tag == 'insert': 
                status = "<< "
                j_val = "---"

            print(f"{web_idx:<4} | {j_val:<10} | {status} | {w_val:<10} | {w_name:<20}")
            if w_val and w_val != "---": web_idx += 1
    print("-" * 85)

def verify_random_routes(json_file='metro_bus_routes.json', sample_size=5):
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError: return

    candidates = [r for r in data if r['direction'] == 0 and len(r['rid']) < 6]
    if not candidates: return
    samples = random.sample(candidates, min(sample_size, len(candidates)))

    print(f"🔍 開始驗證 {len(samples)} 條路線...\n")
    headers = {'User-Agent': 'Mozilla/5.0'}

    for route in samples:
        rid = route['rid']
        name = route['route_name']
        json_sids = route['stops_sid']
        
        url = f"https://pda5284.gov.taipei/MQS/route.jsp?rid={rid}"
        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except: continue

        web_data = HtmlParser.parse_route_page(resp.text)
        web_stops = web_data.get('go_stops', [])

        if not web_stops: continue
        web_sids = [s['sid'] for s in web_stops]

        if json_sids == web_sids:
            print(f"✅ [{name}] (RID:{rid}) 通過驗證！ ({len(json_sids)} 站完全匹配)")
        else:
            print_diff_table(name, json_sids, web_stops)

if __name__ == "__main__":
    verify_random_routes()
```

### 4.3 資料格式範例：`metro_bus_routes.json`

這是最終產出的 JSON 結構範例。

```json
[
  {
    "route_name": "303區",
    "rid": "10272",
    "direction": 0,
    "direction_text": "去程",
    "stops_sid": [
      "20647",
      "57654",
      "20648",
      "...",
      "186144",
      "222922", 
      "222923", 
      "..." 
    ]
  },
  {
    "route_name": "303區",
    "rid": "10272",
    "direction": 1,
    "direction_text": "返程",
    "stops_sid": [ "..." ]
  }
]
```