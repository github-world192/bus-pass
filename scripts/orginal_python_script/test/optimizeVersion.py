"""
Taipei Bus Planner - Refactored Version
=======================================
台北公車即時路線規劃器

此模組提供查詢台北公車路線、預估到站時間以及規劃最佳路徑的功能。
它整合了靜態站點資料庫與政府公開的 Web 介面查詢。

Author: Gemini (Refactored from original)
Date: 2025-12-05
"""

import asyncio
import json
import logging
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp
from bs4 import BeautifulSoup

# ========== 配置與常數 (Configuration) ==========

@dataclass(frozen=True)
class AppSettings:
    """應用程式全域配置"""
    BASE_URL: str = "https://api.codetabs.com/v1/proxy?quest=https://pda5284.gov.taipei/MQS"
    USER_AGENT: str = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleKit/537.36 (KHTML, like Gecko) '
        'Chrome/91.0.4472.124 Safari/537.36'
    )
    TIMEOUT_SECONDS: int = 10
    STOP_DB_FILE: Path = Path('stop_id_map_v3.json')
    ROUTE_CACHE_FILE: Path = Path('route_validation_cache.json')
    
    # 邏輯常數
    TIME_NEAREST: int = -1
    TIME_NOT_DEPARTED: int = 99999
    TIME_ARRIVING: int = 0
    TIME_UNKNOWN: int = 88888


CONFIG = AppSettings()

# 設定 Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("BusPlanner")


# ========== 列舉與例外 (Enums & Exceptions) ==========

class BusStatus(Enum):
    """公車狀態碼對照"""
    ARRIVING = '進站中'
    NOT_DEPARTED = '未發車'
    TRAFFIC_CONTROL = '交管不停'
    LAST_PASSED = '末班已過'
    NOT_OPERATING = '今日未營運'
    UNKNOWN = '未知'

    @classmethod
    def from_code(cls, code: str) -> str:
        mapping = {
            '0': cls.ARRIVING.value,
            '': cls.NOT_DEPARTED.value,
            '-1': cls.NOT_DEPARTED.value,
            '-2': cls.TRAFFIC_CONTROL.value,
            '-3': cls.LAST_PASSED.value,
            '-4': cls.NOT_OPERATING.value
        }
        return mapping.get(str(code).strip(), code)


class DataLoadError(Exception):
    """資料載入相關錯誤"""
    pass


# ========== 資料模型 (Data Models) ==========

@dataclass(frozen=True)
class GeoLocation:
    """地理座標"""
    lat: float
    lon: float


@dataclass
class StopInfo:
    """單一站點資訊"""
    name: str
    sid: str
    geo: Optional[GeoLocation] = None


@dataclass
class BusInfo:
    """公車即時資訊與路徑"""
    route_name: str
    rid: str
    sid: str
    arrival_time_text: str
    raw_time: int
    direction_text: str = "未知"
    stop_count: int = 0
    start_geo: Optional[GeoLocation] = None
    end_geo: Optional[GeoLocation] = None
    path_stops: List[StopInfo] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """序列化為字典"""
        return asdict(self)


# ========== 解析工具 (Parsers) ==========

class TimeParser:
    """負責處理時間字串與秒數轉換"""

    @staticmethod
    def parse_text_to_seconds(text: str) -> int:
        """將顯示文字轉換為用於排序的秒數權重"""
        text = text.strip()
        if "進站" in text:
            return CONFIG.TIME_NEAREST
        if "將到" in text:
            return CONFIG.TIME_ARRIVING
        if any(x in text for x in ["未發車", "末班", "今日未"]):
            return CONFIG.TIME_NOT_DEPARTED
        if ":" in text:
            return CONFIG.TIME_UNKNOWN

        try:
            digits = re.sub(r'\D', '', text)
            if not digits:
                return CONFIG.TIME_NOT_DEPARTED
            val = int(digits)
            return val * 60 if "分" in text else val
        except (ValueError, TypeError):
            return CONFIG.TIME_NOT_DEPARTED

    @staticmethod
    def format_status_code(code: str) -> str:
        """將 API 回傳的狀態碼轉換為人類可讀文字"""
        text = BusStatus.from_code(code)
        # 如果不是標準狀態文字，嘗試解析為秒數
        if text not in [e.value for e in BusStatus] and ":" not in text:
            try:
                secs = int(code)
                if secs < 0:
                    return BusStatus.NOT_DEPARTED.value
                if secs < 180:
                    return "將到站"
                return f"{math.floor(secs / 60)}分"
            except (ValueError, TypeError):
                return BusStatus.NOT_DEPARTED.value
        return text


class HtmlParser:
    """負責 HTML 解析邏輯"""

    @staticmethod
    def parse_stop_page(html: str) -> Dict[str, Dict[str, str]]:
        """
        解析站牌頁面，提取 Route ID 與動態 ID 的對應關係。
        Returns:
            Dict: key=dynamic_id, value={'route': name, 'rid': rid}
        """
        route_map = {}
        soup = BeautifulSoup(html, 'html.parser')
        
        for row in soup.find_all('tr'):
            link_route = row.find('a', href=re.compile(r'route\.jsp'))
            if not link_route:
                continue
            
            rid = parse_qs(urlparse(link_route['href']).query).get('rid', [''])[0]
            
            # 尋找動態 ID (tte...)
            dyn_id = next(
                (td.get('id', '').replace('tte', '') 
                 for td in row.find_all('td') if td.get('id', '').startswith('tte')), 
                None
            )
            
            if dyn_id:
                route_map[dyn_id] = {
                    'route': link_route.text.strip(), 
                    'rid': rid
                }
        return route_map

    @staticmethod
    def parse_route_page(html: str) -> Dict[str, List[Dict[str, str]]]:
        """解析路線詳情頁，提取去程與回程站點"""
        soup = BeautifulSoup(html, 'html.parser')

        def _extract_stops(class_regex: str) -> List[Dict[str, str]]:
            stops = []
            for row in soup.find_all('tr', class_=re.compile(class_regex)):
                link = row.find('a')
                if link:
                    qs = parse_qs(urlparse(link.get('href', '')).query)
                    stops.append({
                        'name': link.text.strip(), 
                        'sid': qs.get('sid', [''])[0]
                    })
            return stops

        return {
            'go_stops': _extract_stops(r'ttego\d+'),
            'back_stops': _extract_stops(r'tteback\d+')
        }

    @staticmethod
    def extract_rids_from_stop(html: str) -> Set[str]:
        """從站牌頁面快速提取所有經過的 Route ID"""
        return set(re.findall(r'route\.jsp\?rid=(\d+)', html))


# ========== 資料存取層 (Repositories) ==========

class StopRepository:
    """負責靜態站點資料 (JSON DB v3 GeoPooled) 的讀取與查詢"""

    def __init__(self, db_file: Path):
        self.db_file = db_file
        # _geo_pool: List[[lat, lon]]
        # _stops: sid -> [name, slid, geo_index]
        # _names: name -> [sid, ...]
        self._geo_pool, self._stops, self._names = self._load_db()

    def _load_db(self) -> Tuple[List, Dict, Dict]:
        if not self.db_file.exists():
            logger.error(f"資料庫檔案不存在: {self.db_file}")
            return [], {}, {}
        try:
            data = json.loads(self.db_file.read_text(encoding='utf-8'))
            return data.get("g", []), data.get("s", {}), data.get("n", {})
        except json.JSONDecodeError as e:
            logger.error(f"資料庫格式錯誤: {e}")
            return [], {}, {}

    def get_info(self, sid: str) -> Optional[Dict]:
        """
        還原為舊版字典結構供 Service 層使用
        """
        raw = self._stops.get(sid)
        if not raw:
            return None
        
        # raw = [name, slid, geo_index]
        name, slid, g_idx = raw[0], raw[1], raw[2]
        
        res = {"name": name, "slid": slid, "sid": sid}
        
        # 從 Pool 取得座標
        if g_idx >= 0 and g_idx < len(self._geo_pool):
            geo = self._geo_pool[g_idx]
            res["lat"] = geo[0]
            res["lon"] = geo[1]
            
        return res

    def get_sids_by_name(self, name: str) -> List[str]:
        return self._names.get(name, [])

    def get_geo(self, sid: str) -> Optional[GeoLocation]:
        raw = self._stops.get(sid)
        if not raw:
            return None
            
        g_idx = raw[2]
        if g_idx >= 0 and g_idx < len(self._geo_pool):
            geo = self._geo_pool[g_idx]
            return GeoLocation(lat=geo[0], lon=geo[1])
        return None

    def get_unique_slids(self, name: str) -> List[Tuple[str, str]]:
        """
        取得該站名下所有不重複的 SLID。
        Returns: List[(slid, representative_sid)]
        """
        sids = self.get_sids_by_name(name)
        seen_slids = set()
        results = []
        
        for sid in sids:
            raw = self._stops.get(sid)
            if not raw:
                continue
            
            slid = raw[1]
            if slid and slid not in seen_slids:
                seen_slids.add(slid)
                results.append((slid, sid))
        return results

class RouteCacheRepository:
    """負責路線驗證結果的本地快取"""
    
    def __init__(self, cache_file: Path):
        self.cache_file = cache_file
        self._cache: Dict[str, List[Dict]] = self._load()

    def _load(self) -> Dict[str, List[Dict]]:
        if not self.cache_file.exists():
            return {}
        try:
            content = self.cache_file.read_text(encoding='utf-8')
            return json.loads(content)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"快取讀取失敗 ({e})，已重置")
            return {}

    def save(self) -> None:
        try:
            self.cache_file.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2), 
                encoding='utf-8'
            )
        except IOError as e:
            logger.error(f"寫入快取失敗: {e}")

    def get(self, start: str, end: str) -> Optional[List[Dict]]:
        return self._cache.get(f"{start}|{end}")

    def set(self, start: str, end: str, buses: List[BusInfo]) -> None:
        key = f"{start}|{end}"
        serialized = []
        for bus in buses:
            data = bus.to_dict()
            # 移除動態時間欄位，只儲存靜態路線結構
            data.pop('arrival_time_text', None)
            data.pop('raw_time', None)
            serialized.append(data)
        self._cache[key] = serialized
        self.save()


# ========== 網路客戶端 (Client) ==========

class TaipeiBusClient:
    """負責所有對外 HTTP 請求"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def _fetch(self, url: str, is_json: bool = False, retries: int = 3) -> Any: 
        """ 發送 HTTP 請求，包含自動重試機制以應對 Proxy 不穩定的問題。 """ 
        headers = {'User-Agent': CONFIG.USER_AGENT}
        for attempt in range(1, retries + 1):
            try:
                async with self.session.get(url, headers=headers, timeout=CONFIG.TIMEOUT_SECONDS) as response:
                    if response.status != 200:
                        # 若非 200，視為失敗，等待後重試
                        if attempt < retries:
                            await asyncio.sleep(0.5 * attempt)
                            continue
                        logger.warning(f"請求失敗 [{response.status}]: {url}")
                        return None
                    
                    return await response.json(content_type=None) if is_json else await response.text()
            
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < retries:
                    # 指數退避 (Exponential Backoff) 避免癱瘓伺服器
                    await asyncio.sleep(0.5 * attempt)
                    continue
                logger.error(f"HTTP請求最終失敗 [{url}]: {e}")
                return None
        return None

    async def get_stop_html(self, sid: str) -> Optional[str]:
        return await self._fetch(f"{CONFIG.BASE_URL}/stop.jsp?sid={sid}")
    
    async def get_stop_location_html(self, slid: str) -> Optional[str]:
        """獲取 StopLocation ID (slid) 對應的公車列表頁面"""
        return await self._fetch(f"{CONFIG.BASE_URL}/stoplocation.jsp?slid={slid}")

    async def get_stop_json_dyna(self, slid: str) -> Optional[Dict]:
        return await self._fetch(
            f"{CONFIG.BASE_URL}/StopLocationDyna?stoplocationid={slid}", 
            is_json=True
        )

    async def get_route_html(self, rid: str) -> Optional[str]:
        return await self._fetch(f"{CONFIG.BASE_URL}/route.jsp?rid={rid}")


# ========== 業務邏輯層 (Service) ==========

class BusPlannerService:
    """路線規劃核心服務"""

    def __init__(self, db_file: Path = CONFIG.STOP_DB_FILE, cache_file: Path = CONFIG.ROUTE_CACHE_FILE):
        self.repo = StopRepository(db_file)
        self.cache = RouteCacheRepository(cache_file)
        # 記憶體內快取，避免單次請求中重複抓取同一路線詳情
        self._memory_route_cache: Dict[str, dict] = {}
        self._route_locks: Dict[str, asyncio.Lock] = {}
    
    async def fetch_realtime_by_slid(self, client: TaipeiBusClient, slid: str, rep_sid: str) -> List[Dict]:
        """使用 SLID 直接查詢即時資訊"""
        # 平行發送請求：HTML (stoplocation.jsp) + JSON (StopLocationDyna)
        task_html = client.get_stop_location_html(slid)
        task_json = client.get_stop_json_dyna(slid)
        
        results = await asyncio.gather(task_html, task_json)
        html_content = results[0]
        json_data = results[1]

        if not html_content:
            return []

        # 1. 解析 HTML 建立 route 結構
        route_map = HtmlParser.parse_stop_page(html_content)
        buses = []

        # 2. 結合 JSON 資料填入精確時間
        if json_data and "Stop" in json_data:
            for stop_item in json_data["Stop"]:
                vals = stop_item.get("n1", "").split(',')
                if len(vals) < 8: 
                    continue
                
                # vals[1]=dynamic_id, vals[7]=status_code/seconds
                j_dyn_id, j_time_code = vals[1], vals[7]
                
                if j_dyn_id in route_map:
                    route_info = route_map.pop(j_dyn_id)
                    time_text = TimeParser.format_status_code(j_time_code)
                    buses.append({
                        'route': route_info['route'], 
                        'rid': route_info['rid'], 
                        'sid': rep_sid, # 使用傳入的代表性 SID
                        'time_text': time_text, 
                        'raw_time': TimeParser.parse_text_to_seconds(time_text)
                    })

        # 3. 處理 HTML 有但 JSON 沒有的車次 (通常是未發車或資料延遲)
        for _, info in route_map.items():
             buses.append({
                'route': info['route'], 
                'rid': info['rid'], 
                'sid': rep_sid,
                'time_text': "更新中", 
                'raw_time': CONFIG.TIME_NOT_DEPARTED
            })
        
        return buses
    
    async def fetch_realtime_by_sid(self, client: TaipeiBusClient, sid: str) -> List[Dict]:
        """
        使用 SID 查詢即時資訊 (自動映射至 SLID 以取得精確資料)。
        這是一個便利函式，內部會轉呼叫 fetch_realtime_by_slid。
        """
        info = self.repo.get_info(sid)
        if not info or not info.get('slid'):
            logger.warning(f"SID {sid} 無法對應至有效的 SLID (查無資料或 SLID 缺失)")
            return []
            
        return await self.fetch_realtime_by_slid(client, info['slid'], sid)

    async def get_route_structure(self, client: TaipeiBusClient, rid: str) -> Dict:
        """取得路線的完整站點結構 (含記憶體鎖與快取)"""
        if rid in self._memory_route_cache:
            return self._memory_route_cache[rid]
        
        if rid not in self._route_locks:
            self._route_locks[rid] = asyncio.Lock()
        
        async with self._route_locks[rid]:
            # Double-check locking
            if rid in self._memory_route_cache:
                return self._memory_route_cache[rid]
            
            html = await client.get_route_html(rid)
            data = HtmlParser.parse_route_page(html) if html else {}
            
            self._memory_route_cache[rid] = data
            return data

    def _determine_direction(
        self, 
        stops: List[Dict], 
        start_sid: str, 
        end_sids: Set[str], 
        end_name: str
    ) -> Optional[List[Dict]]:
        """
        核心演算法：判定路線方向與擷取路徑段。
        邏輯：
        1. 掃描路線中所有可能的起點與終點位置。
        2. 計算所有 [起點 -> 終點] 組合的距離。
        3. 回傳距離最短 (站數最少) 的路徑段，以處理循環路線 (如 A->C->D->E->A->B)。
        """
        # 準備比對資料
        start_info = self.repo.get_info(start_sid)
        target_slid = start_info.get('slid') if start_info else None
        
        end_slids_set = set()
        for esid in end_sids:
            e_info = self.repo.get_info(esid)
            if e_info and e_info.get('slid'):
                end_slids_set.add(e_info['slid'])

        found_starts = []
        found_ends = []

        # 單次遍歷找出所有候選位置
        for i, stop in enumerate(stops):
            curr_sid = stop.get('sid')
            curr_info = self.repo.get_info(curr_sid)
            curr_slid = curr_info.get('slid') if curr_info else None
            
            # 判斷是否為起點
            is_start = False
            if curr_sid == start_sid:
                is_start = True
            elif target_slid and curr_slid == target_slid:
                is_start = True
            
            if is_start:
                found_starts.append(i)

            # 判斷是否為終點
            is_end = (
                (curr_sid in end_sids) or 
                (curr_slid and curr_slid in end_slids_set) or 
                (stop.get('name') == end_name)
            )
            if is_end:
                found_ends.append(i)

        # 尋找最短路徑
        best_range = None
        min_dist = float('inf')

        for s_idx in found_starts:
            # 尋找此起點後的第一個終點
            for e_idx in found_ends:
                if e_idx > s_idx:
                    dist = e_idx - s_idx
                    if dist < min_dist:
                        min_dist = dist
                        best_range = (s_idx, e_idx)
                    # 因為 found_ends 是排序的，找到第一個大於 s_idx 的就是離它最近的終點
                    break 
        
        if best_range:
            s, e = best_range
            return stops[s : e + 1]
            
        return None

    async def update_cached_buses(self, client: TaipeiBusClient, cached_buses: List[Dict]) -> List[BusInfo]:
        """針對已快取的路線，重新抓取即時時間 (優化：使用 SLID 聚合查詢)"""
        
        # 1. 將公車按 SLID 分組
        slid_groups: Dict[str, List[Dict]] = {}

        for b in cached_buses:
            sid = b['sid']
            info = self.repo.get_info(sid)
            # 根據前提：所有 SID 皆有 SLID
            slid = info.get('slid') if info else None
            
            if slid:
                slid_groups.setdefault(slid, []).append(b)

        async def _process_group(slid: str, group_buses: List[Dict]):
            # 使用群組中第一個 bus 的 sid 作為代表 (僅用於滿足函式簽名，實際使用 rid 比對)
            rep_sid = group_buses[0]['sid']
            realtime_list = await self.fetch_realtime_by_slid(client, slid, rep_sid)

            realtime_map = {x['rid']: x for x in realtime_list}
            updated_group = []
            
            for bus_data in group_buses:
                rt = realtime_map.get(bus_data['rid'])
                
                # 若抓不到即時資料，預設為未發車/更新中
                arrival, raw = (rt['time_text'], rt['raw_time']) if rt else ("更新中", CONFIG.TIME_NOT_DEPARTED)
                
                # 重建 StopInfo 物件
                path_objs = [
                    StopInfo(
                        name=p['name'], 
                        sid=p.get('sid', ''), 
                        geo=GeoLocation(**p['geo']) if p.get('geo') else None
                    ) for p in bus_data.get('path_stops', [])
                ]

                # 重建 BusInfo
                updated_group.append(BusInfo(
                    route_name=bus_data['route_name'], 
                    rid=bus_data['rid'], 
                    sid=bus_data['sid'], 
                    arrival_time_text=arrival, 
                    raw_time=raw,
                    direction_text=bus_data['direction_text'], 
                    stop_count=bus_data['stop_count'],
                    start_geo=GeoLocation(**bus_data['start_geo']) if bus_data.get('start_geo') else None,
                    end_geo=GeoLocation(**bus_data['end_geo']) if bus_data.get('end_geo') else None,
                    path_stops=path_objs
                ))
            return updated_group

        # 建立並執行任務
        tasks = []
        for slid, buses in slid_groups.items():
            tasks.append(_process_group(slid, buses))

        results = await asyncio.gather(*tasks)
        merged_buses = [bus for group in results for bus in group]
        merged_buses.sort(key=lambda x: x.raw_time)
        return merged_buses

    async def plan_route(self, start_name: str, end_name: str) -> List[BusInfo]: 
        """主入口：規劃路徑 (交集優先路徑規劃演算法)""" 
        logger.info(f"開始規劃: {start_name} -> {end_name}")

        async with aiohttp.ClientSession() as session:
            client = TaipeiBusClient(session)

            # 1. 檢查快取
            cached_data = self.cache.get(start_name, end_name)
            if cached_data:
                logger.info("命中快取，更新即時時間...")
                return await self.update_cached_buses(client, cached_data)

            # ========== 階段一：資料準備 (Data Preparation) ==========
            # 解析 SLID (使用 representative SID 輔助)
            start_slids = self.repo.get_unique_slids(start_name)
            end_slids = self.repo.get_unique_slids(end_name)
            
            if not start_slids or not end_slids:
                logger.warning(f"找不到站點資料 (SLID): {start_name} 或 {end_name}")
                return []

            # 準備終點輔助集合 (用於 _determine_direction 方向判定)
            end_sids_full = self.repo.get_sids_by_name(end_name)
            end_sids_set = set(end_sids_full)

            # ========== 階段二：平行雙向查詢 (Parallel Dual-Side Fetching) ==========
            # Start Group: 取得完整的 BusInfo 候選物件 (含即時預估時間)
            t_start = asyncio.gather(*[
                self.fetch_realtime_by_slid(client, slid, sid) 
                for slid, sid in start_slids
            ])
            # End Group: 僅解析 HTML 以取得經過該站的 RID 列表
            t_end = asyncio.gather(*[
                client.get_stop_location_html(slid) 
                for slid, _ in end_slids
            ])
            
            res_start, res_end_html = await asyncio.gather(t_start, t_end)
            
            # ========== 階段三：交集過濾 (Intersection Filtering) ==========
            # 從終點組的回傳資料中，提取出 end_rid_set
            end_rid_set = set()
            for html in res_end_html:
                if html:
                    end_rid_set.update(HtmlParser.extract_rids_from_stop(html))

            # 遍歷起點組，保留交集內的公車
            all_start_buses = [b for sublist in res_start for b in sublist]
            intersection_candidates = [
                c for c in all_start_buses if c['rid'] in end_rid_set
            ]
            
            if not intersection_candidates:
                return []

            logger.info(f"交集過濾後剩餘 {len(intersection_candidates)} 條路線，進行精確驗證...")
            
            # ========== 階段四：精確驗證 (Precise Validation) ==========
            # 針對 intersection_candidates 中的路線獲取結構 (Batch Prefetch)
            # 使用 set 來避免同一個 RID 重複抓取 (雖然有 memory cache，但減少 task 建立更好)
            unique_rids = {c['rid'] for c in intersection_candidates}
            await asyncio.gather(*[self.get_route_structure(client, rid) for rid in unique_rids])

            final_buses = []
            seen_keys = set() # 避免同一路線同一方向重複添加

            for cand in intersection_candidates:
                rid, sid = cand['rid'], cand['sid']
                
                # 簡單去重 (針對同一 RID 在同一站點多次出現的情況)
                if (rid, sid) in seen_keys:
                    continue
                
                route_struct = await self.get_route_structure(client, rid)
                
                # 方向判定: 先試去程
                direction = "去程"
                path = self._determine_direction(
                    route_struct.get('go_stops', []), sid, end_sids_set, end_name
                )
                
                # 若無，試回程
                if not path:
                    direction = "返程"
                    path = self._determine_direction(
                        route_struct.get('back_stops', []), sid, end_sids_set, end_name
                    )
                
                # 物件建構
                if path:
                    seen_keys.add((rid, sid))
                    
                    # 擴充為完整路徑物件
                    enhanced_path = []
                    for p in path:
                        p_sid = p.get('sid') or sid
                        enhanced_path.append(StopInfo(
                            name=p['name'], 
                            sid=p_sid, 
                            geo=self.repo.get_geo(p_sid)
                        ))
                    
                    final_buses.append(BusInfo(
                        route_name=cand['route'],
                        rid=rid,
                        sid=enhanced_path[0].sid if enhanced_path else sid, # 使用路徑中實際的第一個站點 SID
                        arrival_time_text=cand['time_text'],
                        raw_time=cand['raw_time'],
                        direction_text=direction,
                        stop_count=len(enhanced_path) - 1,
                        start_geo=enhanced_path[0].geo if enhanced_path else self.repo.get_geo(sid),
                        end_geo=enhanced_path[-1].geo if enhanced_path else None,
                        path_stops=enhanced_path
                    ))

            # 5. 排序與寫入快取
            if final_buses:
                final_buses.sort(key=lambda x: x.raw_time)
                self.cache.set(start_name, end_name, final_buses)
                
            return final_buses


# ========== 程式入口 (Main) ==========

async def main():
    """CLI 測試入口"""
    start_spot = "捷運淡水站"
    end_spot = "台電宿舍"

    service = BusPlannerService()
    
    print(f"🚀 [BusPlanner] 規劃路線: {start_spot} -> {end_spot}")
    print("=" * 70)

    try:
        buses = await service.plan_route(start_spot, end_spot)
    except Exception:
        logger.exception("執行過程發生未預期錯誤")
        return

    if not buses:
        print("⚠️ 查無結果。請檢查站名是否正確，或確認 'stop_id_map.json' 是否存在。")
        return

    print(f"✅ 查詢成功! 共找到 {len(buses)} 班公車\n")
    header_fmt = "{:<6} | {:<8} | {:<5} | {:<5} | {:<10}"
    print(header_fmt.format("路線", "預估時間", "方向", "站數", "候車SID"))
    print("-" * 70)

    for i, bus in enumerate(buses):
        print(header_fmt.format(
            bus.route_name, 
            bus.arrival_time_text, 
            bus.direction_text, 
            bus.stop_count,
            bus.sid
        ))
        
        # 針對前兩班車印出詳細除錯資訊
        if i < 2:
            start_geo = f"{bus.start_geo.lat:.4f},{bus.start_geo.lon:.4f}" if bus.start_geo else "N/A"
            print(f"   ↳ RID: {bus.rid} | Geo: {start_geo}")
    
if __name__ == "__main__":
    # Windows 平台 asyncio 策略修正
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())