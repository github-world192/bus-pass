import requests
import json
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse, parse_qs
import concurrent.futures

# ========== 設定區 ==========
STATION_DB_FILE = 'station_id_map.json'
BASE_URL = "https://pda5284.gov.taipei/MQS/"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}
# ==========================

class TaipeiBusPlanner:
    def __init__(self, db_file):
        self.station_map = self._load_db(db_file)
        self.session = requests.Session()
        self.route_cache = {} # 避免重複下載同一條路線頁面

    def _load_db(self, db_file):
        try:
            with open(db_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"❌ 錯誤：找不到 {db_file}，請先執行上一步的腳本建立資料庫。")
            return {}

    def get_slids(self, station_name):
        return self.station_map.get(station_name, [])

    def fetch_page(self, url):
        try:
            resp = self.session.get(url, headers=HEADERS, timeout=10)
            resp.encoding = 'utf-8'
            return BeautifulSoup(resp.text, 'html.parser')
        except Exception as e:
            print(f"⚠️ 連線錯誤 ({url}): {e}")
            return None

    # 核心邏輯 1: 獲取起點站牌上的所有公車資訊
    def get_buses_at_start(self, slids):
        candidates = [] # 儲存結構: {'route': '252', 'rid': '10746', 'slid': '...', 'direction_text': '去程', 'time': '3分'}
        
        def process_slid(slid):
            url = f"{BASE_URL}stoplocation.jsp?slid={slid}"
            soup = self.fetch_page(url)
            if not soup: return
            
            # 解析表格 (參考 ntnuBranch.html 結構)
            # 通常結構為 tr -> td (Route) -> td (Stop) -> td (Dir) -> td (Time)
            rows = soup.find_all('tr', class_=re.compile(r'tte(go|back)\d+'))
            
            for row in rows:
                cols = row.find_all('td')
                if len(cols) < 4: continue
                
                # 提取路線連結中的 rid
                route_link = cols[0].find('a')
                if not route_link: continue
                
                route_name = route_link.text.strip()
                href = route_link.get('href', '')
                parsed = parse_qs(urlparse(href).query)
                rid = parsed.get('rid', [''])[0]
                
                direction_text = cols[2].text.strip() # "去程" 或 "返程"
                arrival_time = cols[3].text.strip()   # "將到站", "15分", "未發車"
                
                if rid:
                    candidates.append({
                        'route': route_name,
                        'rid': rid,
                        'slid': slid,
                        'dir_type': 'go' if '去程' in direction_text else 'back',
                        'raw_dir_text': direction_text,
                        'time': arrival_time
                    })

        # 並行處理多個 slid (例如師大分部有雙向兩個站牌)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.map(process_slid, slids)
        return candidates

    # 核心邏輯 2: 檢查路線順序 (是否先經過 start, 後經過 end)
    def check_route_direction(self, rid, start_name, end_name):
        # 檢查快取
        cache_key = f"{rid}"
        if cache_key in self.route_cache:
            route_data = self.route_cache[cache_key]
        else:
            # 下載路線詳細頁面 (參考 252.html)
            url = f"{BASE_URL}route.jsp?rid={rid}"
            soup = self.fetch_page(url)
            if not soup: return {'go': False, 'back': False}
            
            # 解析去程 (Go) 與 返程 (Back) 的站序
            # pda5284 結構通常是大表格分成左右兩欄
            stops_go = []
            stops_back = []
            
            # 抓取所有站名連結
            # 這裡簡化邏輯：假設左邊是去程，右邊是返程 (或是依賴 class ttego / tteback)
            go_rows = soup.find_all('tr', class_=re.compile(r'ttego\d+'))
            back_rows = soup.find_all('tr', class_=re.compile(r'tteback\d+'))
            
            for r in go_rows:
                a = r.find('a')
                if a: stops_go.append(a.text.strip())
            
            for r in back_rows:
                a = r.find('a')
                if a: stops_back.append(a.text.strip())
                
            route_data = {'go_stops': stops_go, 'back_stops': stops_back}
            self.route_cache[cache_key] = route_data

        # 判斷邏輯
        valid_directions = []
        
        # 檢查 "Go" (去程) 序列
        try:
            idx_start = route_data['go_stops'].index(start_name)
            idx_end = route_data['go_stops'].index(end_name)
            if idx_start < idx_end: # 起點在終點之前 -> 有效
                valid_directions.append('go')
        except ValueError:
            pass # 站牌不在這個序列中

        # 檢查 "Back" (返程) 序列
        try:
            idx_start = route_data['back_stops'].index(start_name)
            idx_end = route_data['back_stops'].index(end_name)
            if idx_start < idx_end: # 起點在終點之前 -> 有效
                valid_directions.append('back')
        except ValueError:
            pass
        
        return valid_directions

    def plan_journey(self, start_station, end_station):
        print(f"🔍 正在搜尋從 [{start_station}] 到 [{end_station}] 的公車...\n")
        
        # 1. 取得起點的所有 SLID
        start_slids = self.get_slids(start_station)
        if not start_slids:
            print(f"❌ 找不到起點站牌「{start_station}」。請確認 station_id_map.json 是否建立。")
            return

        # 2. 取得所有經過起點的公車候選名單
        candidates = self.get_buses_at_start(start_slids)
        print(f"📋 在起點共找到 {len(candidates)} 班次資訊，正在過濾方向...\n")
        
        valid_routes = []
        checked_rids = set()
        
        # 3. 過濾：找出方向正確的公車
        for bus in candidates:
            # 我們只需要檢查路線一次，不用每班車都查
            unique_key = (bus['rid'], start_station, end_station)
            
            # 判斷該路線是否有效 (這裡會去爬 route.jsp)
            valid_dirs = self.check_route_direction(bus['rid'], start_station, end_station)
            
            # 比對：
            # bus['dir_type'] 是 'go' (去程) 或 'back' (返程)
            # valid_dirs 包含有效的方向 ['go'] 或 ['back'] 或 ['go', 'back']
            if bus['dir_type'] in valid_dirs:
                valid_routes.append(bus)

        # 4. 輸出結果
        if not valid_routes:
            print("❌ 找不到直達公車。可能需要轉乘，或該方向無車。")
        else:
            print(f"✅ 找到 {len(valid_routes)} 班直達公車：")
            print("="*50)
            print(f"{'路線':<8} {'方向':<8} {'預估時間':<10} {'完整資訊'}")
            print("-" * 50)
            
            # 排序：將 "將到站" 排在最前面，其餘按時間排序 (這裡簡單處理字串)
            valid_routes.sort(key=lambda x: x['time'] if x['time'] != '將到站' else '00')
            
            for r in valid_routes:
                print(f"{r['route']:<8} {r['raw_dir_text']:<8} {r['time']:<10} (RID:{r['rid']})")
            print("="*50)

# ========== 主程式執行 ==========
if __name__ == "__main__":
    planner = TaipeiBusPlanner(STATION_DB_FILE)
    
    # 測試案例
    start = "師大分部"
    end = "大坪林" 
    
    planner.plan_journey(start, end)  # 範例 RID，可替換為其他路線測試