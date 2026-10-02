import asyncio
import aiohttp
# 假設您的主程式存檔為 optimizeVersion.py，請確認檔案名稱正確並在此匯入
# 若此腳本直接貼在主程式下方，則不需要這行 import
try:
    from optimizeVersion import TaipeiBusPlanner, BusInfo
except ImportError:
    pass # 假設是在同一檔案內執行

async def audit_907_route(planner: TaipeiBusPlanner):
    start_station = "捷運公館站"
    end_station = "師大"
    target_route = "907"

    print(f"🕵️‍♂️ [診斷啟動] 鎖定路線: {target_route} | {start_station} -> {end_station}")
    print("=" * 100)

    # ==========================================
    # 階段 1: 寬鬆普查 (Ground Truth Collection)
    # ==========================================
    print(f"📊 正在進行寬鬆普查 (僅檢查站名順序)...")
    
    # 1. 取得起點所有 SID
    start_sids = planner.get_sids_by_name(start_station)
    print(f"   -> 起點 '{start_station}' 共關聯 {len(start_sids)} 個 SID: {start_sids}")

    candidates = []

    async with aiohttp.ClientSession() as session:
        # 2. 掃描所有起點 SID 的即時資料
        tasks = [planner.fetch_buses_at_sid(session, sid) for sid in start_sids]
        results = await asyncio.gather(*tasks)

        for sid, buses in zip(start_sids, results):
            # 取得該 SID 的 SLID (關鍵診斷點)
            stop_info = planner._get_stop_info(sid)
            slid = stop_info.get('slid', 'N/A') if stop_info else 'N/A'

            for bus in buses:
                # 只看 907
                if target_route not in bus['route']:
                    continue

                rid = bus['rid']
                # 3. 取得路線詳情
                route_data = await planner.get_route_detail(session, rid)
                
                # 4. 寬鬆驗證：只檢查「站名」順序
                # 檢查去程
                can_reach_go = False
                idx_start_go = -1
                idx_end_go = -1
                for i, s in enumerate(route_data.get('go_stops', [])):
                    if s['name'] == start_station: idx_start_go = i
                    if s['name'] == end_station: idx_end_go = i
                
                if idx_start_go != -1 and idx_end_go != -1 and idx_start_go < idx_end_go:
                    can_reach_go = True

                # 檢查返程
                can_reach_back = False
                idx_start_back = -1
                idx_end_back = -1
                for i, s in enumerate(route_data.get('back_stops', [])):
                    if s['name'] == start_station: idx_start_back = i
                    if s['name'] == end_station: idx_end_back = i
                
                if idx_start_back != -1 and idx_end_back != -1 and idx_start_back < idx_end_back:
                    can_reach_back = True

                # 只要名字順序對，就列入候選
                if can_reach_go or can_reach_back:
                    direction_str = "去程" if can_reach_go else "返程"
                    if can_reach_go and can_reach_back: direction_str = "雙向皆可(罕見)"
                    
                    candidates.append({
                        "sid": sid,
                        "slid": slid,
                        "rid": rid,
                        "time": bus['time_text'],
                        "raw_time": bus['raw_time'],
                        "direction": direction_str,
                        "route_name": bus['route']
                    })

    print(f"   -> 寬鬆普查發現 {len(candidates)} 班潛在可搭班次。")

    # ==========================================
    # 階段 2: 嚴格規劃 (Actual Plan Execution)
    # ==========================================
    print(f"\n⚙️ 執行嚴格規劃 (使用修改後的 SLID 邏輯)...")
    plan_results = await planner.plan(start_station, end_station)
    
    # 建立 Plan 結果的快速查找表 (Key: RID + SID)
    plan_lookup = set()
    for bus in plan_results:
        if target_route in bus.route_name:
            plan_lookup.add((bus.rid, bus.sid))

    # ==========================================
    # 階段 3: 交叉比對與報告 (Diff Report)
    # ==========================================
    print("\n" + "=" * 100)
    print(f"{'判定結果':<10} | {'時間':<8} | {'方向(名)':<6} | {'RID':<6} | {'SID':<6} | {'SLID (關鍵)':<12} | {'說明'}")
    print("-" * 100)

    candidates.sort(key=lambda x: x['raw_time'])

    for cand in candidates:
        key = (cand['rid'], cand['sid'])
        is_included = key in plan_lookup
        
        status = "✅ 命中" if is_included else "❌ 遺漏"
        
        # 簡單分析遺漏原因
        note = ""
        if not is_included:
            if cand['slid'] == 'N/A':
                note = "⚠️ 缺失 SLID，無法通過嚴格驗證"
            else:
                note = "❓ 有 SLID 但仍被過濾 (方向/SLID不匹配)"

        print(f"{status:<10} | {cand['time']:<8} | {cand['direction']:<6} | {cand['rid']:<6} | {cand['sid']:<6} | {cand['slid']:<12} | {note}")

    print("=" * 100)
    print("解讀指引：")
    print("1. 若全是 '✅ 命中'，代表新邏輯運作完美，且資料庫 SLID 完整。")
    print("2. 若出現 '❌ 遺漏' 且 SLID 為 N/A，代表該 SID 在資料庫中沒有 SLID，需補資料或啟用降級機制。")
    print("3. 若出現 '❌ 遺漏' 但有 SLID，代表該 SID 的 SLID 與路線表上的 SLID 不一致 (物理位置判定不符)。")

if __name__ == "__main__":
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    planner = TaipeiBusPlanner()
    asyncio.run(audit_907_route(planner))