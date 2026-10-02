import json
import requests
import logging
import difflib
import re
from bs4 import BeautifulSoup
from urllib.parse import parse_qs, urlparse

# 設定 Log
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("Debugger")

# ==========================================
# 1. 爬蟲核心 (HtmlParser) - 保持不變
# ==========================================
class HtmlParser:
    @staticmethod
    def parse_route_page(html: str):
        soup = BeautifulSoup(html, 'html.parser')

        def _extract_stops(class_regex: str):
            stops = []
            for row in soup.find_all('tr', class_=re.compile(class_regex)):
                link = row.find('a')
                if not link:
                    continue
                qs = parse_qs(urlparse(link.get('href', '')).query)
                raw_sid = qs.get('sid', [''])[0]
                clean_sid = re.sub(r'\D', '', str(raw_sid))
                
                stops.append({
                    'name': link.text.strip(), 
                    'sid': clean_sid
                })
            return stops

        return {
            'go_stops': _extract_stops(r'ttego\d+'),
            'back_stops': _extract_stops(r'tteback\d+')
        }

# ==========================================
# 2. 差異視覺化核心 (Diff Printer) - 保持不變
# ==========================================
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

# ==========================================
# 3. 針對性偵錯主流程
# ==========================================
def debug_target_routes(json_file='metro_bus_routes.json'):
    #在此列表填入想要偵錯的路線名稱
    TARGET_ROUTES = [
        "837區", 
        "128", 
        "872", 
        "685", 
        "303", 
        "595", 
        "927", 
        "林口-捷運府中站"
    ]

    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error(f"找不到 {json_file}")
        return

    print(f"🎯 開始針對以下路線進行偵錯: {TARGET_ROUTES}\n")
    headers = {'User-Agent': 'Mozilla/5.0'}

    found_count = 0

    for target_name in TARGET_ROUTES:
        # 在 JSON 中尋找該路線 (去程 Direction == 0)
        # 使用模糊匹配或精確匹配，這裡先嘗試精確匹配名稱
        target_obj = next((r for r in data if r['route_name'] == target_name and r['direction'] == 0), None)
        
        # 如果找不到，嘗試模糊搜尋 (因為有些路線名稱可能有後綴)
        if not target_obj:
            target_obj = next((r for r in data if target_name in r['route_name'] and r['direction'] == 0), None)

        if not target_obj:
            logger.warning(f"⚠️  在 JSON 中找不到路線: [{target_name}] (可能名稱不符或無去程資料)")
            continue

        rid = target_obj['rid']
        real_name = target_obj['route_name']
        json_sids = target_obj['stops_sid']
        
        logger.info(f"正在檢查 [{real_name}] (RID: {rid})...")

        # 爬取 Web 資料
        url = f"https://pda5284.gov.taipei/MQS/route.jsp?rid={rid}"
        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except Exception as e:
            logger.error(f"❌ 連線失敗: {real_name} - {e}")
            continue

        web_data = HtmlParser.parse_route_page(resp.text)
        web_stops = web_data.get('go_stops', [])

        if not web_stops:
            # 有些路線在 5284 上可能只顯示返程，或者 RID 對應有誤
            # 嘗試抓取返程看看是否只是方向定義不同
            web_stops = web_data.get('back_stops', [])
            if not web_stops:
                logger.warning(f"⚠️  [{real_name}] 網頁無任何站點資料 (可能 RID 無效)")
                continue
            else:
                logger.info(f"ℹ️  [{real_name}] 網頁僅有返程資料，嘗試比對...")

        web_sids = [s['sid'] for s in web_stops]

        # 比對邏輯
        if json_sids == web_sids:
            print(f"✅ [{real_name}] 通過驗證！ ({len(json_sids)} 站完全匹配)")
        else:
            print_diff_table(real_name, json_sids, web_stops)
        
        found_count += 1
        print("="*60 + "\n")

    print(f"偵錯結束。共處理 {found_count} 條路線。")

if __name__ == "__main__":
    debug_target_routes()