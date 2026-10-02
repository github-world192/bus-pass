import json
import os

def count_unique_geos(json_filepath: str):
    """
    統計 stop_id_map.json 中所有不同的經緯度 (lat/lon) 座標的總數。
    經緯度以 (lat, lon) 嚴格浮點數對進行比較。
    """
    
    # 檢查 JSON 文件是否存在
    if not os.path.exists(json_filepath):
        print(f"錯誤：找不到檔案 {json_filepath}")
        return

    try:
        # 1. 讀取 JSON 資料
        with open(json_filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        print(f"錯誤：檔案 {json_filepath} 不是有效的 JSON 格式")
        return

    # 確保 'by_sid' 鍵存在
    stops_by_sid = data.get("by_sid", {})
    
    # 使用集合 (set) 來儲存唯一的 (lat, lon) 座標，自動處理重複
    unique_geos = set()
    
    # 2. 遍歷所有站點並提取經緯度
    for sid, stop_info in stops_by_sid.items():
        # 檢查 'lat' 和 'lon' 鍵是否存在且值非空
        if 'lat' in stop_info and 'lon' in stop_info and \
           stop_info['lat'] is not None and stop_info['lon'] is not None:
            
            # 將 (lat, lon) 作為 tuple 加入集合，以便進行嚴格的浮點數比較
            geo_tuple = (stop_info['lat'], stop_info['lon'])
            unique_geos.add(geo_tuple)

    # 3. 輸出結果
    total_unique_geos = len(unique_geos)
    
    print("---------------------------------------")
    print(f"✅ 經緯度統計完成")
    print(f"在 {json_filepath} 中，總共有 **{total_unique_geos}** 個相異的經緯度座標。")
    print("---------------------------------------")
    
    return total_unique_geos


# 執行函式
# 假設 json 檔案與程式碼在同一個目錄下
count_unique_geos('stop_id_map.json')