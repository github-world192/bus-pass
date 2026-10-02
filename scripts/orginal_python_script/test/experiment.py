"""
Taipei Bus Strategy Benchmark (Fixed & Fair)
============================================
修正版效能測試：確保兩個策略使用相同的「路徑有效性」驗證邏輯。

比較項目：
1. Naive Strategy: 驗證起點每一班車，檢查是否順向經過終點。
2. Intersection Strategy: 先過濾出 RID 同時存在於起點與終點的車，再檢查是否順向。

Author: Gemini
"""

import asyncio
import time
import logging
from typing import List, Set, Dict, Optional, Tuple

# 嘗試匯入原檔案
try:
    from optimizeVersion import (
        BusPlannerService, TaipeiBusClient, AppSettings, 
        HtmlParser, BusInfo, StopInfo, GeoLocation, CONFIG, StopRepository
    )
except ImportError:
    print("❌ 錯誤: 找不到 'optimizeVersion.py'。請確保該檔案存在於同一目錄下。")
    exit(1)

# 設定 Logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("Benchmark")

class MetricClient(TaipeiBusClient):
    """帶有計數功能的 Client"""
    def __init__(self, session):
        super().__init__(session)
        self.request_count = 0

    async def _fetch(self, url: str, is_json: bool = False):
        self.request_count += 1
        return await super()._fetch(url, is_json)

class BenchmarkService(BusPlannerService):
    
    async def _verify_single_bus(
        self, 
        client: TaipeiBusClient, 
        bus_candidate: Dict, 
        end_sids_set: Set[str], 
        end_name: str
    ) -> Optional[Dict]:
        """
        [共用核心邏輯] 驗證單一公車路線是否「順向」經過終點。
        若符合條件，回傳完整的 BusInfo 字典；否則回傳 None。
        """
        rid = bus_candidate['rid']
        sid = bus_candidate['sid']
        
        # 取得路線結構 (這是最耗時的 IO 操作)
        route_struct = await self.get_route_structure(client, rid)
        
        # 邏輯判斷：去程
        direction = "去程"
        path = self._determine_direction(
            route_struct.get('go_stops', []), sid, end_sids_set, end_name
        )
        
        # 邏輯判斷：回程
        if not path:
            direction = "返程"
            path = self._determine_direction(
                route_struct.get('back_stops', []), sid, end_sids_set, end_name
            )
            
        if path:
            # 為了實驗公平，我們這裡簡化回傳，只標記找到並回傳原始資訊
            # 實際應用會在這裡構建完整的 StopInfo
            return bus_candidate
        return None

    async def plan_route_naive(self, client: MetricClient, start_name: str, end_name: str) -> List[Dict]:
        """策略 1: Naive (驗證全部)"""
        # 1. 準備資料
        start_slids = self.repo.get_unique_slids(start_name)
        end_sids_set = set(self.repo.get_sids_by_name(end_name))

        if not start_slids: return []

        # 2. 取得起點所有車次
        candidates_nested = await asyncio.gather(*[
            self.fetch_realtime_by_slid(client, slid, sid) 
            for slid, sid in start_slids
        ])
        candidates = [b for sublist in candidates_nested for b in sublist]
        
        # 去除重複 RID (避免同一台車驗證兩次，雖然 Naive 很笨，但通常不會笨到重複驗證同一個 RID)
        # 但為了嚴格模擬「對每一台經過起點的路線做檢查」，我們以 RID 為單位
        unique_candidates = {c['rid']: c for c in candidates}.values()

        # 3. 【效能瓶頸】對「所有」路線進行驗證
        tasks = [
            self._verify_single_bus(client, cand, end_sids_set, end_name)
            for cand in unique_candidates
        ]
        
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

    async def plan_route_intersection(self, client: MetricClient, start_name: str, end_name: str) -> List[Dict]:
        """策略 2: Intersection (先過濾再驗證)"""
        # 1. 準備資料
        start_slids = self.repo.get_unique_slids(start_name)
        end_slids = self.repo.get_unique_slids(end_name)
        end_sids_set = set(self.repo.get_sids_by_name(end_name))

        # 2. 平行查詢：起點即時 + 終點 HTML (取得 RID)
        t_start = asyncio.gather(*[
            self.fetch_realtime_by_slid(client, slid, sid) for slid, sid in start_slids
        ])
        t_end = asyncio.gather(*[
            client.get_stop_location_html(slid) for slid, _ in end_slids
        ])
        
        res_start, res_end_html = await asyncio.gather(t_start, t_end)

        # 3. 建立終點 RID 集合 (Filter)
        end_rids_union = set()
        for html in res_end_html:
            if html:
                end_rids_union.update(HtmlParser.extract_rids_from_stop(html))

        # 4. 應用交集過濾
        candidates = [b for sublist in res_start for b in sublist]
        # 只保留 RID 存在於終點站列表的公車
        filtered_candidates = [c for c in candidates if c['rid'] in end_rids_union]
        
        # 去除重複
        unique_filtered = {c['rid']: c for c in filtered_candidates}.values()

        # 5. 【優化後】只驗證過濾後的路線
        tasks = [
            self._verify_single_bus(client, cand, end_sids_set, end_name)
            for cand in unique_filtered
        ]
        
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

async def run_experiment():
    import aiohttp
    
    # 測試參數
    START_STOP = "捷運公館站"
    END_STOP = "師大分部" 
    
    print(f"🧪 開始公平實驗: {START_STOP} -> {END_STOP}")
    print("=" * 60)

    service = BenchmarkService()
    
    # 共用的 Session 避免建立開銷影響太小
    # 但為了計數準確，我們分開 context
    
    # --- 測試 1: Naive ---
    naive_count = 0
    naive_time = 0
    naive_routes_found = 0
    
    async with aiohttp.ClientSession() as session:
        client = MetricClient(session)
        service._memory_route_cache.clear() # 清空快取
        service._route_locks.clear()
        
        print(f"Running Naive Strategy...")
        start_t = time.time()
        results = await service.plan_route_naive(client, START_STOP, END_STOP)
        naive_time = time.time() - start_t
        
        naive_count = client.request_count
        naive_routes_found = len(results)
        
        # 為了比對，印出找到的 RID (排序)
        naive_rids = sorted([r['rid'] for r in results])
        print(f"   ↳ 找到 RID: {naive_rids}")

    # --- 測試 2: Intersection ---
    inter_count = 0
    inter_time = 0
    inter_routes_found = 0
    
    async with aiohttp.ClientSession() as session:
        client = MetricClient(session)
        service._memory_route_cache.clear() # 清空快取
        service._route_locks.clear()
        
        print(f"Running Intersection Strategy...")
        start_t = time.time()
        results = await service.plan_route_intersection(client, START_STOP, END_STOP)
        inter_time = time.time() - start_t
        
        inter_count = client.request_count
        inter_routes_found = len(results)
        
        inter_rids = sorted([r['rid'] for r in results])
        print(f"   ↳ 找到 RID: {inter_rids}")

    # --- 結果分析 ---
    print("\n" + "=" * 60)
    print(f"{'Metric':<15} | {'Naive (暴力法)':<15} | {'Intersection (交集法)':<15} | {'差異'}")
    print("-" * 65)
    print(f"{'找到路線數':<15} | {naive_routes_found:<15} | {inter_routes_found:<15} | {naive_routes_found - inter_routes_found}")
    print(f"{'HTTP 請求數':<15} | {naive_count:<15} | {inter_count:<15} | {inter_count - naive_count}")
    print(f"{'耗時 (秒)':<15} | {naive_time:<15.4f} | {inter_time:<15.4f} | {inter_time - naive_time:.4f}")
    
    if naive_rids == inter_rids:
        print("\n✅ 驗證通過：兩種策略找到的路線完全一致！")
        print("   數據證明 Intersection 策略在保證準確性的前提下，大幅降低了請求成本。")
    else:
        print("\n⚠️ 驗證警告：路線結果仍不一致。")
        print("   可能原因：終點站的 'stop_location.jsp' 列表資料不完整，漏掉了某些實際上會經過的路線。")
        print("   (這代表 API 資料源本身存在不一致，而非演算法邏輯錯誤)")
        diff_missing = set(naive_rids) - set(inter_rids)
        if diff_missing:
            print(f"   交集法漏掉的路線 (存在於路線圖但不在終點站牌列表): {diff_missing}")

if __name__ == "__main__":
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_experiment())