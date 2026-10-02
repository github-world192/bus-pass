"""
Taipei Bus Planner - Refactored Version
=======================================
台北公車即時路線規劃器

此模組提供查詢台北公車路線、預估到站時間以及規劃最佳路徑的功能。
它整合了靜態站點資料庫與政府公開的 Web 介面查詢。

Refactored by: Principal Refactoring Architect
Date: 2025-12-09
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
from typing import Any, Dict, List, Optional, Set, Tuple, Union
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
    TIMEOUT_SECONDS: int = 20
    MAX_CONCURRENT_REQUESTS: int = 5
    
    # File Paths
    STOP_DB_FILE: Path = Path('stop_id_map_v3.json')
    METRO_ROUTES_FILE: Path = Path('metro_bus_routes.json')
    ROUTE_CACHE_FILE: Path = Path('route_validation_cache.json')
    
    # Logic Constants
    TIME_NEAREST: int = -1
    TIME_NOT_DEPARTED: int = 99999
    TIME_ARRIVING: int = 0
    TIME_UNKNOWN: int = 88888


CONFIG = AppSettings()

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
        stripped_code = str(code).strip()
        return mapping.get(stripped_code, code)


class DataLoadError(Exception):
    pass


# ========== 資料模型 (Data Models) ==========

@dataclass(frozen=True)
class GeoLocation:
    lat: float
    lon: float


@dataclass
class StopInfo:
    name: str
    sid: str
    geo: Optional[GeoLocation] = None


@dataclass
class BusInfo:
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
        return asdict(self)


# ========== 解析工具 (Parsers) ==========

class TimeParser:
    """負責處理時間字串與秒數轉換"""

    @staticmethod
    def parse_text_to_seconds(text: str) -> int:
        text = text.strip()
        
        # Priority text matches
        if "進站" in text:
            return CONFIG.TIME_NEAREST
        if "將到" in text:
            return CONFIG.TIME_ARRIVING
        if any(keyword in text for keyword in ["未發車", "末班", "今日未"]):
            return CONFIG.TIME_NOT_DEPARTED
        if ":" in text:
            return CONFIG.TIME_UNKNOWN

        return TimeParser._parse_numeric_time(text)

    @staticmethod
    def _parse_numeric_time(text: str) -> int:
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
        text = BusStatus.from_code(code)
        
        # Return mapped text if found or if it looks like a clock time
        known_statuses = {e.value for e in BusStatus}
        if text in known_statuses or ":" in text:
            return text
            
        return TimeParser._format_seconds_from_code(code)

    @staticmethod
    def _format_seconds_from_code(code: str) -> str:
        try:
            secs = int(code)
            if secs < 0:
                return BusStatus.NOT_DEPARTED.value
            if secs < 180:
                return "將到站"
            return f"{math.floor(secs / 60)}分"
        except (ValueError, TypeError):
            return BusStatus.NOT_DEPARTED.value


class HtmlParser:
    """負責 HTML 解析邏輯"""

    @staticmethod
    def parse_stop_page(html: str) -> Dict[str, Dict[str, str]]:
        """Returns: Dict[dynamic_id, {'route': name, 'rid': rid}]"""
        route_map = {}
        soup = BeautifulSoup(html, 'html.parser')
        
        for row in soup.find_all('tr'):
            HtmlParser._process_row(row, route_map)
            
        return route_map

    @staticmethod
    def _process_row(row, route_map: Dict[str, Dict[str, str]]) -> None:
        link_route = row.find('a', href=re.compile(r'route\.jsp'))
        if not link_route:
            return
        
        # Find dynamic ID (starts with 'tte')
        dyn_id_node = row.find(id=re.compile(r'^tte'))
        if not dyn_id_node:
            return

        rid = parse_qs(urlparse(link_route['href']).query).get('rid', [''])[0]
        dyn_id = dyn_id_node.get('id', '').replace('tte', '')
        
        if dyn_id:
            route_map[dyn_id] = {
                'route': link_route.text.strip(), 
                'rid': rid
            }


# ========== 資料存取層 (Repositories) ==========

class StopRepository:
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
        raw = self._stops.get(sid)
        if not raw:
            return None
        
        name, slid, g_idx = raw[0], raw[1], raw[2]
        res = {"name": name, "slid": slid, "sid": sid}
        
        if 0 <= g_idx < len(self._geo_pool):
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
        if not (0 <= g_idx < len(self._geo_pool)):
            return None
            
        geo = self._geo_pool[g_idx]
        return GeoLocation(lat=geo[0], lon=geo[1])

    def get_unique_slids(self, name: str) -> List[Tuple[str, str]]:
        """Returns: List[(slid, representative_sid)]"""
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
            # Do not cache dynamic time data
            data.pop('arrival_time_text', None)
            data.pop('raw_time', None)
            serialized.append(data)
        self._cache[key] = serialized
        self.save()


class StaticRouteRepository:
    """讀取 metro_bus_routes.json 並提供離線路徑查找"""
    def __init__(self, db_file: Path):
        self.db_file = db_file
        self._routes: List[Dict] = self._load()

    def _load(self) -> List[Dict]:
        if not self.db_file.exists():
            logger.error(f"靜態路線檔不存在: {self.db_file}")
            return []
        try:
            return json.loads(self.db_file.read_text(encoding='utf-8'))
        except json.JSONDecodeError as e:
            logger.error(f"靜態路線檔格式錯誤: {e}")
            return []

    def find_routes_between(self, start_sids: Set[str], end_sids: Set[str]) -> List[Dict]:
        """
        找出所有「經過 Start 且之後經過 End」的路線變體。
        Returns: List[{...route_obj..., 'match_range': (start_idx, end_idx)}]
        """
        candidates = []
        for route in self._routes:
            matched_variant = self._match_single_route(route, start_sids, end_sids)
            if matched_variant:
                candidates.extend(matched_variant)
        return candidates

    def _match_single_route(self, route: Dict, start_sids: Set[str], end_sids: Set[str]) -> List[Dict]:
        stops = route.get('stops_sid', [])
        
        # 1. Identify indices
        start_indices = [i for i, sid in enumerate(stops) if sid in start_sids]
        end_indices = [i for i, sid in enumerate(stops) if sid in end_sids]
        
        if not start_indices or not end_indices:
            return []

        variants = []
        for s_idx in start_indices:
            # Find the nearest subsequent end stop
            first_valid_end = next((e for e in end_indices if e > s_idx), None)
            
            if first_valid_end is not None:
                r_copy = route.copy()
                r_copy['match_range'] = (s_idx, first_valid_end)
                variants.append(r_copy)
                
        return variants


# ========== 網路客戶端 (Client) ==========

class TaipeiBusClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def _fetch(self, url: str, is_json: bool = False, retries: int = 3) -> Any: 
        headers = {'User-Agent': CONFIG.USER_AGENT}
        
        for attempt in range(1, retries + 1):
            try:
                async with self.session.get(url, headers=headers, timeout=CONFIG.TIMEOUT_SECONDS) as response:
                    if response.status != 200:
                        await self._handle_fetch_error(attempt, retries, f"Status {response.status}", url)
                        continue
                    return await response.json(content_type=None) if is_json else await response.text()
            
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                await self._handle_fetch_error(attempt, retries, str(e), url)
                
        return None

    async def _handle_fetch_error(self, attempt: int, retries: int, error: str, url: str) -> None:
        if attempt < retries:
            await asyncio.sleep(0.5 * attempt)
        else:
            logger.error(f"Request failed [{url}]: {error}")

    async def get_stop_html(self, sid: str) -> Optional[str]:
        return await self._fetch(f"{CONFIG.BASE_URL}/stop.jsp?sid={sid}")
    
    async def get_stop_location_html(self, slid: str) -> Optional[str]:
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
    def __init__(self, 
                 db_file: Path = CONFIG.STOP_DB_FILE, 
                 cache_file: Path = CONFIG.ROUTE_CACHE_FILE,
                 static_route_file: Path = CONFIG.METRO_ROUTES_FILE):
        self.repo = StopRepository(db_file)
        self.cache = RouteCacheRepository(cache_file)
        self.static_routes = StaticRouteRepository(static_route_file)

    async def _batch_execute(self, tasks: List[Any]) -> List[Any]:
        """批次執行非同步任務以控制併發數量"""
        limit = CONFIG.MAX_CONCURRENT_REQUESTS
        results = []
        for i in range(0, len(tasks), limit):
            batch = tasks[i : i + limit]
            if batch:
                batch_results = await asyncio.gather(*batch)
                results.extend(batch_results)
        return results
    
    async def fetch_realtime_by_slid(self, client: TaipeiBusClient, slid: str, rep_sid: str) -> List[Dict]:
        """使用 SLID 直接查詢即時資訊"""
        task_html = client.get_stop_location_html(slid)
        task_json = client.get_stop_json_dyna(slid)
        
        results = await asyncio.gather(task_html, task_json)
        html_content, json_data = results[0], results[1]

        if not html_content:
            return []

        route_map = HtmlParser.parse_stop_page(html_content)
        buses = []

        # JSON 資料整合
        if json_data and "Stop" in json_data:
            for stop_item in json_data["Stop"]:
                self._process_json_bus_entry(stop_item, route_map, rep_sid, buses)

        # 填補 HTML 有但 JSON 無的車次 (未發車/延遲)
        for _, info in route_map.items():
             buses.append({
                'route': info['route'], 
                'rid': info['rid'], 
                'sid': rep_sid,
                'time_text': "更新中", 
                'raw_time': CONFIG.TIME_NOT_DEPARTED
            })
        
        return buses

    def _process_json_bus_entry(self, stop_item: Dict, route_map: Dict, rep_sid: str, buses: List):
        """處理單筆即時動態 JSON"""
        vals = stop_item.get("n1", "").split(',')
        if len(vals) < 8: 
            return
        
        # Magic index mapping
        j_dyn_id = vals[1]
        j_time_code = vals[7]
        
        if j_dyn_id not in route_map:
            return

        route_info = route_map.pop(j_dyn_id)
        time_text = TimeParser.format_status_code(j_time_code)
        
        buses.append({
            'route': route_info['route'], 
            'rid': route_info['rid'], 
            'sid': rep_sid,
            'time_text': time_text, 
            'raw_time': TimeParser.parse_text_to_seconds(time_text)
        })
    
    async def fetch_realtime_by_sid(self, client: TaipeiBusClient, sid: str) -> List[Dict]:
        info = self.repo.get_info(sid)
        if not info or not info.get('slid'):
            logger.warning(f"SID {sid} 無法對應至有效的 SLID")
            return []
        return await self.fetch_realtime_by_slid(client, info['slid'], sid)

    async def update_cached_buses(self, client: TaipeiBusClient, cached_buses: List[Dict]) -> List[BusInfo]:
        """針對已快取的路線，重新抓取即時時間"""
        slid_groups = self._group_buses_by_slid(cached_buses)
        
        tasks = [self._update_single_group(client, slid, buses) for slid, buses in slid_groups.items()]
        results = await self._batch_execute(tasks)
        
        merged_buses = [bus for group in results for bus in group]
        merged_buses.sort(key=lambda x: x.raw_time)
        return merged_buses

    def _group_buses_by_slid(self, buses: List[Dict]) -> Dict[str, List[Dict]]:
        groups = {}
        for b in buses:
            info = self.repo.get_info(b['sid'])
            slid = info.get('slid') if info else None
            if slid:
                groups.setdefault(slid, []).append(b)
        return groups

    async def _update_single_group(self, client: TaipeiBusClient, slid: str, group_buses: List[Dict]) -> List[BusInfo]:
        if not group_buses:
            return []
            
        rep_sid = group_buses[0]['sid']
        realtime_list = await self.fetch_realtime_by_slid(client, slid, rep_sid)
        realtime_map = {x['rid']: x for x in realtime_list}
        
        updated_group = []
        for bus_data in group_buses:
            updated_group.append(self._reconstruct_bus_info(bus_data, realtime_map))
        return updated_group

    def _reconstruct_bus_info(self, bus_data: Dict, realtime_map: Dict) -> BusInfo:
        rt = realtime_map.get(bus_data['rid'])
        arrival = rt['time_text'] if rt else "更新中"
        raw = rt['raw_time'] if rt else CONFIG.TIME_NOT_DEPARTED
        
        path_objs = [
            StopInfo(
                name=p['name'], 
                sid=p.get('sid', ''), 
                geo=GeoLocation(**p['geo']) if p.get('geo') else None
            ) for p in bus_data.get('path_stops', [])
        ]

        return BusInfo(
            route_name=bus_data['route_name'], 
            rid=bus_data['rid'], 
            sid=bus_data['sid'], 
            arrival_time_text=arrival, 
            raw_time=raw,
            direction_text=bus_data.get('direction_text', '未知'), 
            stop_count=bus_data.get('stop_count', 0),
            start_geo=GeoLocation(**bus_data['start_geo']) if bus_data.get('start_geo') else None,
            end_geo=GeoLocation(**bus_data['end_geo']) if bus_data.get('end_geo') else None,
            path_stops=path_objs
        )

    async def plan_route(self, start_name: str, end_name: str) -> List[BusInfo]: 
        logger.info(f"開始規劃: {start_name} -> {end_name}")

        async with aiohttp.ClientSession() as session:
            client = TaipeiBusClient(session)

            # 0. Cache Check
            cached_data = self.cache.get(start_name, end_name)
            if cached_data:
                logger.info("命中快取，更新即時時間...")
                return await self.update_cached_buses(client, cached_data)

            # 1. Static Route Matching
            matched_routes = self._find_static_candidates(start_name, end_name)
            if not matched_routes:
                return []
            logger.info(f"靜態資料庫找到 {len(matched_routes)} 條可能路線")

            # 2. Realtime Fetching
            all_realtime_buses = await self._fetch_all_realtime(client, matched_routes)

            # 3. Intersection & Construction
            final_buses = self._merge_route_data(matched_routes, all_realtime_buses)

            # 4. Finalize
            if final_buses:
                final_buses.sort(key=lambda x: x.raw_time)
                self.cache.set(start_name, end_name, final_buses)
                
            return final_buses

    def _find_static_candidates(self, start_name: str, end_name: str) -> List[Dict]:
        start_sids = set(self.repo.get_sids_by_name(start_name))
        end_sids = set(self.repo.get_sids_by_name(end_name))
        
        if not start_sids or not end_sids:
            logger.warning(f"站名無法解析: {start_name} 或 {end_name}")
            return []

        return self.static_routes.find_routes_between(start_sids, end_sids)

    async def _fetch_all_realtime(self, client: TaipeiBusClient, matched_routes: List[Dict]) -> List[Dict]:
        """Identify required SLIDs and fetch realtime data for all of them."""
        # Extract unique start SIDs from matched routes
        target_start_sids = {
            r['stops_sid'][r['match_range'][0]] 
            for r in matched_routes
        }

        # Resolve SIDs to SLIDs
        slid_sid_map = []
        seen_slids = set()
        
        for sid in target_start_sids:
            info = self.repo.get_info(sid)
            slid = info.get('slid') if info else None
            
            if slid and slid not in seen_slids:
                seen_slids.add(slid)
                slid_sid_map.append((slid, sid))

        # Batch fetch
        tasks = [
            self.fetch_realtime_by_slid(client, slid, rep_sid) 
            for slid, rep_sid in slid_sid_map
        ]
        
        # Flatten results
        nested_results = await self._batch_execute(tasks)
        return [b for sublist in nested_results for b in sublist]

    def _merge_route_data(self, matched_routes: List[Dict], realtime_buses: List[Dict]) -> List[BusInfo]:
        """Match static routes with realtime data to create final BusInfo objects."""
        
        # Create lookup: SLID -> List[Bus]
        slid_to_realtime = self._build_realtime_lookup(realtime_buses)
        final_buses = []

        for r in matched_routes:
            bus_info = self._construct_single_bus_info(r, slid_to_realtime)
            if bus_info:
                final_buses.append(bus_info)
                
        return final_buses

    def _build_realtime_lookup(self, realtime_buses: List[Dict]) -> Dict[str, List[Dict]]:
        lookup = {}
        for bus in realtime_buses:
            info = self.repo.get_info(bus['sid'])
            if info and info.get('slid'):
                lookup.setdefault(info['slid'], []).append(bus)
        return lookup

    def _construct_single_bus_info(self, route_dict: Dict, slid_to_realtime: Dict) -> Optional[BusInfo]:
        rid = route_dict['rid']
        start_idx, end_idx = route_dict['match_range']
        start_sid = route_dict['stops_sid'][start_idx]
        
        # Resolve SLID
        s_info = self.repo.get_info(start_sid)
        if not s_info:
            return None
        slid = s_info.get('slid')
        
        # Find matching realtime bus
        buses_at_stop = slid_to_realtime.get(slid, [])
        match_bus = next((b for b in buses_at_stop if b['rid'] == rid), None)
        
        arrival_text = match_bus['time_text'] if match_bus else "未發車"
        raw_time = match_bus['raw_time'] if match_bus else CONFIG.TIME_NOT_DEPARTED

        # Build path stops
        path_sids = route_dict['stops_sid'][start_idx : end_idx + 1]
        path_objs = []
        for psid in path_sids:
            p_info = self.repo.get_info(psid)
            p_geo = self.repo.get_geo(psid)
            p_name = p_info['name'] if p_info else "未知站點"
            path_objs.append(StopInfo(name=p_name, sid=psid, geo=p_geo))

        if not path_objs:
            return None

        return BusInfo(
            route_name=route_dict['route_name'],
            rid=rid,
            sid=start_sid,
            arrival_time_text=arrival_text,
            raw_time=raw_time,
            direction_text=route_dict['direction_text'],
            stop_count=len(path_objs) - 1,
            start_geo=path_objs[0].geo,
            end_geo=path_objs[-1].geo,
            path_stops=path_objs
        )


# ========== 程式入口 (Main) ==========

async def main():
    start_spot = "捷運淡水站"
    end_spot = "台電宿舍"

    service = BusPlannerService()
    
    print(f"🚀 [BusPlanner] 規劃路線: {start_spot} -> {end_spot}")
    print("=" * 70)

    try:
        buses = await service.plan_route(start_spot, end_spot)
    except Exception as e:
        logger.exception(f"執行過程發生未預期錯誤: {e}")
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
        
        if i < 2:
            start_geo = f"{bus.start_geo.lat:.4f},{bus.start_geo.lon:.4f}" if bus.start_geo else "N/A"
            print(f"   ↳ RID: {bus.rid} | Geo: {start_geo}")
    
if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())