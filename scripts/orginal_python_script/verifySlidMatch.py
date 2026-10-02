import os

import requests
import json

# ========== 設定區 (請填入您的 TDX 金鑰) ==========
CLIENT_ID = '41347902S-573d3f05-4c94-4a27'
CLIENT_SECRET = os.environ["TDX_CLIENT_SECRET"]
# ===============================================

def get_auth_token():
    auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    data = {
        'grant_type': 'client_credentials',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET
    }
    response = requests.post(auth_url, data=data)
    return response.json().get('access_token')

def test_stop_mapping(stop_name):
    token = get_auth_token()
    headers = {'authorization': f'Bearer {token}'}
    
    # 針對臺北市進行查詢，使用 OData 語法過濾站名
    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/Stop/City/Taipei?$filter=StopName/Zh_tw eq '{stop_name}'&$format=JSON"
    
    print(f"正在查詢站名: {stop_name} ...")
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        with open('verifySlidMatch_output.json', 'w', encoding='utf-8') as f:
            json.dump(response.json(), f, ensure_ascii=False, indent=4)
        stops = response.json()
        print(f"找到 {len(stops)} 筆資料：")
        print("-" * 40)
        for stop in stops:
            s_name = stop['StopName']['Zh_tw']
            s_id = stop.get('StopID', 'N/A')
            s_slid = stop.get('StationID', '找不到此欄位') # 關鍵檢查點
            print(f"站名: {s_name} | StopID: {s_id} | StopLocationID (slid): {s_slid}")
        print("-" * 40)
        
        # 驗證邏輯
        found_slid = [s.get('StationID') for s in stops]
        if "1000036" in found_slid:
             print("✅ 驗證成功！找到了 slid=1000036 (對應 ntnuBranch.html)")
        else:
             print("⚠️ 驗證注意：未直接找到 1000036，請檢查輸出結果。")
    else:
        print(f"查詢失敗，狀態碼: {response.status_code}")

# 執行測試
if __name__ == "__main__":
    test_stop_mapping("師大分部")