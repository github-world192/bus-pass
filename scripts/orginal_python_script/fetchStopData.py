import requests
import json
import time
import re
import logging

# ========== 設定區 ==========
CLIENT_ID = '41347902S-573d3f05-4c94-4a27'
CLIENT_SECRET = '1d257004-04ad-4082-8987-c37629240044'
OUTPUT_FILE = 'stop_id_map.json'
# ==========================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def get_auth_token():
    token_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    data = {'grant_type': 'client_credentials', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}
    try:
        resp = requests.post(token_url, data=data)
        resp.raise_for_status()
        return resp.json().get('access_token')
    except Exception as e:
        logging.error(f"Token 取得失敗: {e}")
        return None

def fetch_stop_data_with_slid():
    token = get_auth_token()
    if not token: return
    headers = {'authorization': f'Bearer {token}'}
    cities = ['Taipei', 'NewTaipei']
    
    # 資料結構設計
    # by_sid:  "35536" -> {"lat": 25.0, "lon": 121.0, "name": "師大分部", "slid": "1000036"}
    # by_name: "師大分部" -> ["35536", "35537"]
    result_map = {
        "by_sid": {},
        "by_name": {}
    }
    
    logging.info("開始下載站牌資料 (含 StationID/SLID)...")

    for city in cities:
        logging.info(f"正在下載 [{city}] 資料...")
        
        # [關鍵修改] 在 $select 中加入 StationID
        url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/Stop/City/{city}?$select=StopName,StopUID,StopPosition,StationID&$format=JSON"
        
        try:
            resp = requests.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
            processed_count = 0
            for stop in data:
                try:
                    name = stop['StopName']['Zh_tw']
                    full_uid = stop.get('StopUID')     # 例如 "TPE35536"
                    slid = stop.get('StationID')       # 例如 "1000036"
                    pos = stop.get('StopPosition', {})
                    lat = pos.get('PositionLat')
                    lon = pos.get('PositionLon')
                except (KeyError, TypeError):
                    continue
                
                # 基本檢核: 必須要有 名稱、UID、座標
                # StationID (slid) 有些新設站牌可能真的沒有，可以允許為空或None，但在台北市通常都有
                if name and full_uid and lat and lon:
                    
                    # 1. 提取 SID 數字部分 (移除 TPE, NWT 等前綴)
                    numeric_part_match = re.search(r'\d+', full_uid)
                    if not numeric_part_match:
                        continue
                    sid_num = numeric_part_match.group()
                    
                    # 2. 建立 SID -> Info (加入 slid)
                    result_map["by_sid"][sid_num] = {
                        "lat": lat,
                        "lon": lon,
                        "name": name,
                        "slid": slid,      # [新增] 儲存 slid
                        "full_uid": full_uid
                    }
                    
                    # 3. 建立 Name -> SIDs List
                    if name not in result_map["by_name"]:
                        result_map["by_name"][name] = []
                    
                    if sid_num not in result_map["by_name"][name]:
                        result_map["by_name"][name].append(sid_num)
                        
                    processed_count += 1
            
            logging.info(f"  - [{city}] 處理完成，共 {processed_count} 筆。")
            
        except Exception as e:
            logging.error(f"  - [{city}] 錯誤: {e}")
        time.sleep(1)

    # 儲存
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result_map, f, ensure_ascii=False, indent=2)
    
    logging.info(f"\n資料庫建立完成！儲存於 {OUTPUT_FILE}")
    
    # 驗證範例
    if "35536" in result_map["by_sid"]:
        logging.info(f"驗證 SID 35536 (師大分部): {result_map['by_sid']['35536']}")
    else:
        # 若找不到剛剛的範例，隨機印一筆有 slid 的
        for k, v in result_map["by_sid"].items():
            if v.get('slid'):
                logging.info(f"隨機驗證 SID {k}: {v}")
                break

if __name__ == "__main__":
    fetch_stop_data_with_slid()