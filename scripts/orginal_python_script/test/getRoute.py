import requests
import json
import re
import time
import random
import os
import urllib.parse
from bs4 import BeautifulSoup

# =================設定區=================
SOURCE_FILENAME = 'metro_bus_routes.json'      # 來源檔案 (提供 RID)
CHECKPOINT_FILENAME = 'checkpoint_5284.jsonl'  # 中間存檔 (斷點紀錄)
OUTPUT_FILENAME = 'metro_bus_routes_5284.json' # 最終產出
MAX_RETRIES = 3                                # 單一 RID 失敗重試次數
# =======================================

class Robust5284Fetcher:
    def __init__(self):
        self.base_url = "https://pda5284.gov.taipei/MQS"
        # 隨機 User-Agent 庫，稍微降低被擋機率
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36',
            'Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Mobile/15E148 Safari/604.1'
        ]
        self.finished_rids = set()

    def load_checkpoint(self):
        """讀取中間檔案，找出已經完成的 RID"""
        if not os.path.exists(CHECKPOINT_FILENAME):
            return

        print(f"📥 正在讀取 Checkpoint: {CHECKPOINT_FILENAME} ...")
        count = 0
        try:
            with open(CHECKPOINT_FILENAME, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip(): continue
                    try:
                        data = json.loads(line)
                        # 一行可能包含去程和返程，我們只記錄 RID 即可
                        # 格式是 list of dicts，取第一個元素的 rid
                        if isinstance(data, list) and len(data) > 0:
                            rid = str(data[0]['rid'])
                            self.finished_rids.add(rid)
                            count += 1
                    except json.JSONDecodeError:
                        continue
            print(f"✅ 已恢復進度：{len(self.finished_rids)} 筆 RID 已完成，將自動跳過。")
        except Exception as e:
            print(f"⚠️ 讀取 Checkpoint 時發生錯誤 (可能檔案損壞): {e}")

    def save_to_checkpoint(self, data_list):
        """將抓取到的資料 (List of Dicts) 寫入 jsonl"""
        if not data_list: return
        
        # 使用 append 模式 ('a')
        with open(CHECKPOINT_FILENAME, 'a', encoding='utf-8') as f:
            # 將整個 list 轉為一行 json string
            json_line = json.dumps(data_list, ensure_ascii=False)
            f.write(json_line + "\n")
            # 強制寫入硬碟，避免緩衝區資料遺失
            f.flush()
            os.fsync(f.fileno())

    def fetch_with_retry(self, url):
        """帶有重試機制的請求發送"""
        for attempt in range(MAX_RETRIES):
            try:
                headers = {'User-Agent': random.choice(self.user_agents)}
                resp = requests.get(url, headers=headers, timeout=15) # 設定 timeout
                if resp.status_code == 200:
                    return resp
                elif resp.status_code in [500, 502, 503, 504]:
                    print(f"   ⚠️ 伺服器錯誤 {resp.status_code}，等待重試 ({attempt+1}/{MAX_RETRIES})...")
                    time.sleep(2 + attempt * 2) # 指數退避
                else:
                    print(f"   ❌ 請求失敗 {resp.status_code}")
                    return None
            except requests.exceptions.RequestException as e:
                print(f"   ⚠️ 網路連線錯誤: {e}，等待重試 ({attempt+1}/{MAX_RETRIES})...")
                time.sleep(2 + attempt * 2)
        return None

    def fetch_stops_by_rid(self, rid, route_name_hint):
        url = f"{self.base_url}/route.jsp?rid={rid}"
        resp = self.fetch_with_retry(url)
        
        if not resp:
            return None # 失敗

        try:
            resp.encoding = 'utf-8'
            if "查無資料" in resp.text:
                # 即使查無資料，也回傳一個空 list，代表「已處理過」(避免無窮迴圈重試)
                # 或是你可以選擇回傳 None 讓它下次再試
                return [] 

            soup = BeautifulSoup(resp.text, 'html.parser')
            
            def _parse_table(class_regex):
                stops = []
                rows = soup.find_all('tr', class_=re.compile(class_regex))
                for row in rows:
                    link = row.find('a')
                    if not link: continue
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(link.get('href', '')).query)
                    raw_sid = qs.get('sid', [''])[0]
                    clean_sid = re.sub(r'\D', '', str(raw_sid))
                    if clean_sid: stops.append(clean_sid)
                return stops

            go_stops = _parse_table(r'ttego\d+')
            back_stops = _parse_table(r'tteback\d+')

            if not go_stops and not back_stops:
                return [] # 解析不到東西

            result = []
            page_title = soup.find('td', class_='tte_header_black')
            real_name = page_title.text.strip() if page_title else route_name_hint

            if go_stops:
                result.append({"route_name": real_name, "rid": rid, "direction": 0, "direction_text": "去程", "stops_sid": go_stops})
            if back_stops:
                result.append({"route_name": real_name, "rid": rid, "direction": 1, "direction_text": "返程", "stops_sid": back_stops})
                
            return result

        except Exception as e:
            print(f"   ❌ 解析例外: {e}")
            return None

    def finalize_json(self):
        """將 checkpoint.jsonl 轉為最終的 JSON 檔案"""
        print(f"\n📦 正在將 Checkpoint 轉換為最終檔案 {OUTPUT_FILENAME} ...")
        final_list = []
        if os.path.exists(CHECKPOINT_FILENAME):
            with open(CHECKPOINT_FILENAME, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        try:
                            # 每一行都是一個 list (包含去返程)
                            data = json.loads(line)
                            final_list.extend(data)
                        except:
                            pass
        
        with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
            json.dump(final_list, f, ensure_ascii=False, indent=2)
        print(f"🎉 轉換完成！總共 {len(final_list)} 筆路線方向資料。")

    def run(self):
        # 1. 讀取待處理清單
        try:
            with open(SOURCE_FILENAME, 'r', encoding='utf-8') as f:
                source_data = json.load(f)
        except FileNotFoundError:
            print(f"❌ 找不到來源檔案 {SOURCE_FILENAME}")
            return

        # 整理 RID (去重)
        targets = {} 
        for item in source_data:
            rid = str(item.get('rid'))
            if rid: targets[rid] = item.get('route_name')

        # 2. 讀取 Checkpoint
        self.load_checkpoint()

        # 3. 過濾掉已完成的 RID
        pending_targets = {rid: name for rid, name in targets.items() if rid not in self.finished_rids}
        
        print(f"📊 總 RID: {len(targets)} | 已完成: {len(self.finished_rids)} | 待處理: {len(pending_targets)}")
        print("🚀 開始執行任務 (按 Ctrl+C 可隨時中斷，下次執行會自動續傳)...")

        total = len(pending_targets)
        for idx, (rid, name) in enumerate(pending_targets.items(), 1):
            print(f"[{idx}/{total}] 處理 {name} (RID: {rid})...", end="", flush=True)
            
            # 隨機延遲，模擬人類行為，防止被 Ban
            sleep_time = random.uniform(0.5, 1.5)
            time.sleep(sleep_time)

            routes = self.fetch_stops_by_rid(rid, name)
            
            if routes is not None:
                # 無論 routes 是空陣列(查無資料)還是有資料，只要不是 None(報錯)，都算執行過
                # 若 routes 有資料才寫入
                if routes:
                    self.save_to_checkpoint(routes)
                    print(f" ✅ 成功 ({len(routes)} 向)")
                else:
                    # 如果查無資料，建議還是記錄到 finished_rids (雖然不寫入 jsonl)，
                    # 但因為這裡簡單實作，如果不寫入 checkpoint，下次會重試。
                    # 若要避免無窮重試無效 RID，可寫入一個 {"rid": rid, "empty": True} 標記
                    # 這裡選擇：僅顯示警告，不寫入，讓使用者人工判斷是否要移除該 RID
                    print(f" ⚠️ 無站點資料 (不存檔)")
            else:
                print(f" ❌ 失敗 (跳過)")

        # 4. 全部跑完後，產出最終 JSON
        self.finalize_json()

if __name__ == "__main__":
    fetcher = Robust5284Fetcher()
    fetcher.run()