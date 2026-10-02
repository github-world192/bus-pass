import requests
import json
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse, parse_qs
import concurrent.futures
import math

# ========== 設定區 ==========
STATION_DB_FILE = 'station_id_map.json'
BASE_URL = "https://pda5284.gov.taipei/MQS/"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}
STATUS_CODES = {'0': '進站中', '': '未發車', '-1': '未發車', '-2': '交管不停', '-3': '末班已過', '-4': '今日未營運'}
# ==========================

class TaipeiBusPlanner:
    def __init__(self, db_file):
        self.station_map = self._load_db(db_file)
        self.session = requests.Session()
        self.route_cache = {} 

    def _load_db(self, db_file):
        try:
            with open(db_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"❌ 錯誤：找不到 {db_file}。")
            return {}

    def get_slids(self, station_name):
        data = self.station_map.get(station_name, {})
        return list(data.keys())

    def get_station_geo(self, station_name, slid):
        return self.station_map.get(station_name, {}).get(slid, {'lat': 'N/A', 'lon': 'N/A'})

    # [新增] 取得站名的代表座標 (取第一筆 SLID 的座標)
    def get_representative_geo(self, station_name):
        slids = self.get_slids(station_name)
        if slids:
            # 取第一個找到的 SLID 的座標當作該站代表座標
            return self.get_station_geo(station_name, slids[0])
        return {'lat': 'N/A', 'lon': 'N/A'}

    def fetch_page(self, url):
        try:
            resp = self.session.get(url, headers=HEADERS, timeout=10)
            resp.encoding = 'utf-8'
            return resp
        except Exception as e:
            print(f"⚠️ 連線錯誤 ({url}): {e}")
            return None

    def _format_arrival_time(self, time_value):
        if time_value in STATUS_CODES: return STATUS_CODES[time_value]
        try:
            seconds = int(time_value)
            if seconds == 0: return "進站中"
            elif 0 < seconds < 180: return "將到站"
            elif seconds >= 180: return f"{math.floor(seconds / 60)}分"
            else: return STATUS_CODES.get(str(seconds), "數據異常")
        except ValueError: return "資料格式錯誤"

    def get_buses_at_start(self, slids):
        candidates = [] 
        def process_slid(slid):
            # 1. 靜態頁面
            url_static = f"{BASE_URL}stoplocation.jsp?slid={slid}"
            resp_static = self.fetch_page(url_static)
            if not resp_static: return

            soup = BeautifulSoup(resp_static.text, 'html.parser')
            bus_rows = soup.find_all('tr', class_=['ttego1', 'ttego2'])
            route_map = {}
            
            for row in bus_rows:
                cols = row.find_all('td')
                if len(cols) < 4: continue
                
                route_link = cols[0].find('a')
                if not route_link: continue
                route_name = route_link.text.strip()
                
                href = route_link.get('href', '')
                parsed = parse_qs(urlparse(href).query)
                rid = parsed.get('rid', [''])[0]
                raw_dir_text = cols[2].text.strip()
                
                time_td = cols[3]
                if time_td and time_td.get('id'):
                    dynamic_id = time_td.get('id').replace('tte', '')
                    route_map[dynamic_id] = {'route': route_name, 'rid': rid, 'dir': raw_dir_text, 'slid': slid}

            # 2. 動態 JSON
            url_dyna = f"{BASE_URL}StopLocationDyna?stoplocationid={slid}"
            resp_dyna = self.fetch_page(url_dyna)
            if not resp_dyna: return

            try:
                json_data = resp_dyna.json()
                for stop_entry in json_data.get("Stop", []):
                    if "n1" not in stop_entry: continue
                    n1_parts = stop_entry["n1"].split(',')
                    if len(n1_parts) < 8: continue
                    
                    stop_dynamic_id = n1_parts[1]
                    raw_time = n1_parts[7]
                    
                    if stop_dynamic_id in route_map:
                        info = route_map[stop_dynamic_id]
                        candidates.append({
                            'route': info['route'],
                            'rid': info['rid'],
                            'slid': info['slid'],
                            'dir_text': info['dir'], 
                            'time': self._format_arrival_time(raw_time),
                            'raw_time': raw_time
                        })
            except json.JSONDecodeError: pass

        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.map(process_slid, slids)
        return candidates

# [修正版] 支援多重站點出現的距離計算邏輯
    def check_route_direction(self, rid, start_name, end_name):
        cache_key = f"{rid}"
        if cache_key in self.route_cache:
            route_data = self.route_cache[cache_key]
        else:
            url = f"{BASE_URL}route.jsp?rid={rid}"
            resp = self.fetch_page(url)
            if not resp: return {} 
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # 提取完整站牌列表
            stops_go = [a.text.strip() for r in soup.find_all('tr', class_=re.compile(r'ttego\d+')) if (a := r.find('a'))]
            stops_back = [a.text.strip() for r in soup.find_all('tr', class_=re.compile(r'tteback\d+')) if (a := r.find('a'))]
            
            route_data = {'go_stops': stops_go, 'back_stops': stops_back}
            self.route_cache[cache_key] = route_data

        valid_directions_map = {}
        
        # 核心修正：計算最短有效距離
        def get_min_stop_distance(stops, s_name, e_name):
            # 1. 找出起點在列表中的「所有」索引位置
            start_indices = [i for i, x in enumerate(stops) if x == s_name]
            
            if not start_indices:
                return None
            
            possible_distances = []
            
            # 2. 針對每一個起點位置，檢查後面是否有終點
            for s_idx in start_indices:
                # 截取該起點之後的列表
                sub_list = stops[s_idx + 1:]
                
                if e_name in sub_list:
                    # 找到該起點後的第一個終點
                    # 距離 = 子列表索引 + 1 (因為子列表是從 s_idx+1 開始)
                    dist = sub_list.index(e_name) + 1
                    possible_distances.append(dist)
            
            # 3. 如果有有效路徑，回傳最短的那個距離
            if possible_distances:
                return min(possible_distances)
                
            return None

        # 檢查去程
        dist_go = get_min_stop_distance(route_data['go_stops'], start_name, end_name)
        if dist_go is not None: valid_directions_map['去程'] = dist_go

        # 檢查返程
        dist_back = get_min_stop_distance(route_data['back_stops'], start_name, end_name)
        if dist_back is not None: valid_directions_map['返程'] = dist_back

        return valid_directions_map

    def plan_journey(self, start_station, end_station):
        print(f"🔍 搜尋: [{start_station}] -> [{end_station}]\n")
        
        start_slids = self.get_slids(start_station)
        if not start_slids:
            print(f"❌ 找不到起點站牌。")
            return

        # 預先取得終點站的座標資訊 (取第一筆做為代表)
        end_geo = self.get_representative_geo(end_station)
        
        candidates = self.get_buses_at_start(start_slids)
        valid_routes = []
        checked_routes_cache = {} 
        
        for bus in candidates:
            rid = bus['rid']
            if rid not in checked_routes_cache:
                checked_routes_cache[rid] = self.check_route_direction(rid, start_station, end_station)
            
            valid_dirs_map = checked_routes_cache[rid]
            if bus['dir_text'] in valid_dirs_map:
                bus['stop_count'] = valid_dirs_map[bus['dir_text']]
                
                # 注入地理資訊
                bus['start_geo'] = self.get_station_geo(start_station, bus['slid'])
                bus['end_geo'] = end_geo # 注入終點座標
                
                valid_routes.append(bus)

        if not valid_routes:
            print("❌ 找不到直達公車。")
        else:
            print(f"✅ 找到 {len(valid_routes)} 班直達公車：")
            print("="*110)
            # 調整 Header 寬度以容納兩組座標
            header = f"{'路線':<6} {'方向':<6} {'時間':<8} {'距離':<6} {'起點座標 (Lat, Lon)':<25} {'終點座標 (Lat, Lon)'}"
            print(header)
            print("-" * 110)
            
            def sort_key(item):
                t = item['raw_time']
                try:
                    val = int(t)
                    if val < 0: return 99998 
                    return val
                except: return 99999 
            
            valid_routes.sort(key=sort_key)
            
            for r in valid_routes:
                start_geo_str = f"{r['start_geo']['lat']}, {r['start_geo']['lon']}"
                end_geo_str = f"{r['end_geo']['lat']}, {r['end_geo']['lon']}"
                
                print(f"{r['route']:<6} {r['dir_text']:<6} {r['time']:<8} {r['stop_count']}站   {start_geo_str:<25} {end_geo_str}")
            print("="*110)

if __name__ == "__main__":
    planner = TaipeiBusPlanner(STATION_DB_FILE)
    planner.plan_journey("捷運淡水站","台電宿舍")