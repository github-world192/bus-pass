import json
import os

# ========== 設定 ==========
SOURCE_DB = 'station_id_map.json'
OUTPUT_INDEX = 'sid_lookup_map.json'

def build_sid_index():
    if not os.path.exists(SOURCE_DB):
        print(f"❌ 找不到 {SOURCE_DB}，請先執行 fetchStopData.py")
        return

    print(f"正在讀取 {SOURCE_DB} ...")
    with open(SOURCE_DB, 'r', encoding='utf-8') as f:
        station_map = json.load(f)

    # 建立反向索引: SID -> {slid, name}
    # 這樣我們只要有 SID，就能反查出它屬於哪個站名和哪個 SLID，進而取得座標
    sid_lookup = {}
    
    count = 0
    for name, slids_data in station_map.items():
        for slid, slid_info in slids_data.items():
            # 檢查是否有 sids 欄位
            if 'sids' in slid_info:
                for sid in slid_info['sids']:
                    sid_lookup[sid] = {
                        'slid': slid,
                        'name': name
                    }
                    count += 1

    print(f"索引建立完成，共索引 {count} 個 SID。")
    
    with open(OUTPUT_INDEX, 'w', encoding='utf-8') as f:
        json.dump(sid_lookup, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 已輸出至 {OUTPUT_INDEX}")
    
    # 測試範例
    if sid_lookup:
        sample_sid = next(iter(sid_lookup))
        print(f"\n範例查詢 SID [{sample_sid}]:")
        print(sid_lookup[sample_sid])

if __name__ == "__main__":
    build_sid_index()