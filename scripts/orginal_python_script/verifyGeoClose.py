import json
import math

# 設定檔案名稱
MAP_FILE = 'station_id_map.json'

def verify_coordinates():
    print(f"正在讀取 {MAP_FILE}...")
    try:
        with open(MAP_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"錯誤: 找不到檔案 {MAP_FILE}，請確認檔案位於同一目錄下。")
        return

    stats = {
        "total_sids": 0,
        "exact_match": 0,
        "close_match": 0,  # 差異極小 (< 1公尺)
        "mismatch": 0      # 差異顯著
    }
    
    # 設定容許誤差 (0.0001 度約等於 1.1 公尺)
    TOLERANCE = 0.0003

    print("開始比對 SLID 與 SID 座標...\n")

    for station_name, slids in data.items():
        for slid, slid_info in slids.items():
            # 取得 SLID (父層) 座標
            slid_lat = slid_info.get('lat')
            slid_lon = slid_info.get('lon')
            
            sids_map = slid_info.get('sids', {})
            
            for sid, sid_info in sids_map.items():
                stats["total_sids"] += 1
                
                # 取得 SID (子層) 座標
                sid_lat = sid_info.get('lat')
                sid_lon = sid_info.get('lon')
                
                # 檢查資料完整性
                if None in (slid_lat, slid_lon, sid_lat, sid_lon):
                    continue
                
                # 1. 檢查完全一致
                if slid_lat == sid_lat and slid_lon == sid_lon:
                    stats["exact_match"] += 1
                else:
                    # 2. 計算差異
                    lat_diff = abs(slid_lat - sid_lat)
                    lon_diff = abs(slid_lon - sid_lon)
                    
                    if lat_diff < TOLERANCE and lon_diff < TOLERANCE:
                        stats["close_match"] += 1
                    else:
                        stats["mismatch"] += 1
                        # 只印出前 5 筆不符的案例供參考
                        if stats["mismatch"] <= 5:
                            print(f"⚠️  發現差異 [{station_name}]")
                            print(f"    SLID({slid}): {slid_lat}, {slid_lon}")
                            print(f"    SID ({sid}):  {sid_lat}, {sid_lon}")
                            print(f"    差異: lat={lat_diff:.6f}, lon={lon_diff:.6f}\n")

    # 輸出統計結果
    total = stats["total_sids"]
    if total == 0:
        print("沒有找到任何 SID 資料。")
        return

    print("-" * 40)
    print("📊 驗證統計結果")
    print("-" * 40)
    print(f"總檢查 SID 數: {total}")
    
    p_exact = (stats['exact_match'] / total) * 100
    p_close = (stats['close_match'] / total) * 100
    p_diff = (stats['mismatch'] / total) * 100
    
    print(f"✅ 完全一致: {stats['exact_match']:>6} 筆 ({p_exact:.2f}%)")
    print(f"🆗 極微差異: {stats['close_match']:>6} 筆 ({p_close:.2f}%) (視為相同)")
    print(f"❌ 顯著差異: {stats['mismatch']:>6} 筆 ({p_diff:.2f}%)")
    print("-" * 40)
    
    if p_exact + p_close > 90:
        print("結論: 您的假設成立。絕大多數情況下，SLID 座標與 SID 座標相同。")
    else:
        print("結論: 您的假設不完全成立，有相當比例的站牌座標與站位座標不同。")

if __name__ == "__main__":
    verify_coordinates()