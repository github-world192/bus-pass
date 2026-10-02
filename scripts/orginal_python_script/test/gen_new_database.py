import json
from pathlib import Path

def generate_optimized_db(input_file: str = 'stop_id_map.json', output_file: str = 'stop_id_map_v3.json'):
    """
    Generate v3 Schema: Geo Pooling
    - 保留所有 SID 的精確座標資訊
    - 使用 Geo Pool 移除重複的經緯度資料
    """
    src_path = Path(input_file)
    if not src_path.exists():
        print(f"❌ 找不到來源檔案: {input_file}")
        return

    print(f"📂 讀取來源: {input_file} ...")
    try:
        raw_data = json.loads(src_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        print("❌ JSON 格式錯誤")
        return

    by_sid = raw_data.get("by_sid", {})
    
    # 1. 建立座標池 (Geo Pool)
    # 使用 dict 來去重複: "lat,lon" -> index
    geo_map = {} 
    geo_pool = []
    
    # 2. 準備新的資料結構
    # s: sid -> [name, slid, geo_index]
    # n: name -> [sid...]
    new_stops = {}
    new_names = {}

    print("🔄 執行座標池化與索引建置...")
    
    for sid, info in by_sid.items():
        name = info.get("name", "未知")
        slid = info.get("slid", "")
        
        # 處理座標索引
        g_idx = -1
        if "lat" in info and "lon" in info:
            try:
                # 轉為 float 確保格式一致，再轉字串做為 key
                lat = float(info["lat"])
                lon = float(info["lon"])
                key = f"{lat},{lon}"
                
                if key in geo_map:
                    g_idx = geo_map[key]
                else:
                    g_idx = len(geo_pool)
                    geo_pool.append([lat, lon]) # 存入 float array
                    geo_map[key] = g_idx
            except (ValueError, TypeError):
                pass 

        # 儲存 SID 資料: [Name, SLID, GeoIndex]
        # 即使沒有座標 (g_idx = -1)，也保留結構以便程式判斷
        new_stops[sid] = [name, slid, g_idx]

        # 建立 Name Index
        if name not in new_names:
            new_names[name] = []
        new_names[name].append(sid)

    # 組裝最終結構
    final_db = {
        "v": 3,
        "g": geo_pool,
        "s": new_stops,
        "n": new_names
    }

    # 寫入檔案
    out_path = Path(output_file)
    out_path.write_text(json.dumps(final_db, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    
    print(f"✅ 轉換完成 (v3)！")
    print(f"   - 總 SID 數: {len(new_stops)}")
    print(f"   - 唯一座標數 (Geo Pool): {len(geo_pool)} (原始重複量: {len(new_stops)})")
    print(f"   - 輸出檔案: {output_file}")
    print(f"   - 原始大小: {src_path.stat().st_size / 1024 / 1024:.2f} MB")
    print(f"   - 優化大小: {out_path.stat().st_size / 1024 / 1024:.2f} MB")

if __name__ == "__main__":
    generate_optimized_db()