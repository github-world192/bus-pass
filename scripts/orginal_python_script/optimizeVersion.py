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
    BASE_URL: str = "https://pda5284.gov.taipei/MQS"
    USER_AGENT: str = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleKit/537.36 (KHTML, like Gecko) '
        'Chrome/91.0.4472.124 Safari/537.36'
    )
    TIMEOUT_SECONDS: int = 10
    STOP_DB_FILE: Path = Path('stop_id_map.json')
    ROUTE_CACHE_FILE: Path = Path('route_validation_cache.json')
    
    # 邏輯常數
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
        if any(x in text for x in ["進站", "將到"]):
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
    """負責靜態站點資料 (JSON DB) 的讀取與查詢"""

    def __init__(self, db_file: Path):
        self.db_file = db_file
        self._data: Dict[str, Any] = self._load_db()

    def _load_db(self) -> Dict[str, Any]:
        if not self.db_file.exists():
            logger.error(f"資料庫檔案不存在: {self.db_file}")
            return {"by_sid": {}, "by_name": {}}
        try:
            return json.loads(self.db_file.read_text(encoding='utf-8'))
        except json.JSONDecodeError as e:
            logger.error(f"資料庫格式錯誤: {e}")
            return {"by_sid": {}, "by_name": {}}

    def get_info(self, sid: str) -> Optional[Dict]:
        return self._data.get("by_sid", {}).get(sid)

    def get_sids_by_name(self, name: str) -> List[str]:
        return self._data.get("by_name", {}).get(name, [])

    def get_geo(self, sid: str) -> Optional[GeoLocation]:
        info = self.get_info(sid)
        if info and 'lat' in info and 'lon' in info:
            try:
                return GeoLocation(lat=float(info['lat']), lon=float(info['lon']))
            except ValueError:
                return None
        return None

    def get_representative_sids(self, name: str) -> List[str]:
        """
        過濾同一站名下重複的 SID。
        優先選擇有 'slid' (Station Location ID) 的站點，
        避免重複查詢相同的物理站牌。
        """
        all_sids = self.get_sids_by_name(name)
        seen_slids = set()
        representatives = []
        
        for sid in all_sids:
            info = self.get_info(sid)
            slid = info.get('slid') if info else None
            
            if slid and slid not in seen_slids:
                seen_slids.add(slid)
                representatives.append(sid)
            elif not slid:
                # 若無 slid，為了安全起見仍保留
                representatives.append(sid)
        return representatives


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

    async def _fetch(self, url: str, is_json: bool = False) -> Any:
        try:
            headers = {'User-Agent': CONFIG.USER_AGENT}
            async with self.session.get(url, headers=headers, timeout=CONFIG.TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None) if is_json else await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug(f"HTTP請求失敗 [{url}]: {e}")
            return None

    async def get_stop_html(self, sid: str) -> Optional[str]:
        return await self._fetch(f"{CONFIG.BASE_URL}/stop.jsp?sid={sid}")

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

    async def fetch_realtime_at_sid(self, client: TaipeiBusClient, sid: str) -> List[Dict]:
        """取得特定 SID 的即時公車列表"""
        info = self.repo.get_info(sid)
        slid = info.get('slid') if info else None

        # 平行發送請求：HTML (靜態列表) + JSON (即時秒數)
        task_html = client.get_stop_html(sid)
        task_json = client.get_stop_json_dyna(slid) if slid else asyncio.sleep(0)
        
        results = await asyncio.gather(task_html, task_json)
        html_content = results[0]
        json_data = results[1] if slid else None

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
                        'sid': sid,
                        'time_text': time_text, 
                        'raw_time': TimeParser.parse_text_to_seconds(time_text)
                    })

        # 3. 處理 HTML 有但 JSON 沒有的車次 (通常是未發車或資料延遲)
        for _, info in route_map.items():
             buses.append({
                'route': info['route'], 
                'rid': info['rid'], 
                'sid': sid,
                'time_text': "更新中", 
                'raw_time': CONFIG.TIME_NOT_DEPARTED
            })
        
        return buses

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
        1. 在路線中找到起點 (比對 SID 或 SLID)。
        2. 從起點之後搜尋終點 (比對 SID、SLID 或站名)。
        """
        start_info = self.repo.get_info(start_sid)
        target_slid = start_info.get('slid') if start_info else None

        # 1. 定位起點索引
        start_index = -1
        for i, stop in enumerate(stops):
            curr_sid = stop.get('sid')
            
            # A: 直接 SID 命中
            if curr_sid == start_sid:
                start_index = i
                break
            
            # B: SLID 命中 (容錯機制)
            if target_slid:
                curr_info = self.repo.get_info(curr_sid)
                if curr_info and curr_info.get('slid') == target_slid:
                    start_index = i
                    break
        
        if start_index == -1:
            return None

        # 2. 定位終點索引 (從起點後開始找)
        path_slice = stops[start_index:]
        found_index = -1
        
        # 優化：預先計算終點的 SLID 集合
        end_slids_set = set()
        for esid in end_sids:
            e_info = self.repo.get_info(esid)
            if e_info and e_info.get('slid'):
                end_slids_set.add(e_info['slid'])

        for i, stop in enumerate(path_slice):
            if i == 0: continue # 跳過起點

            curr_sid = stop.get('sid')
            curr_info = self.repo.get_info(curr_sid)
            curr_slid = curr_info.get('slid') if curr_info else None

            # 命中條件：SID 吻合 OR SLID 吻合 OR 站名吻合
            is_match = (
                (curr_sid in end_sids) or 
                (curr_slid and curr_slid in end_slids_set) or 
                (stop.get('name') == end_name)
            )
            
            if is_match:
                found_index = i
                break
        
        if found_index != -1:
            return path_slice[:found_index + 1]
            
        return None

    async def update_cached_buses(self, client: TaipeiBusClient, cached_buses: List[Dict]) -> List[BusInfo]:
        """針對已快取的路線，重新抓取即時時間"""
        sid_groups = {}
        for b in cached_buses:
            sid_groups.setdefault(b['sid'], []).append(b)

        async def _process_group(sid: str, group_buses: List[Dict]):
            realtime_list = await self.fetch_realtime_at_sid(client, sid)
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
                    sid=sid,
                    arrival_time_text=arrival, 
                    raw_time=raw,
                    direction_text=bus_data['direction_text'], 
                    stop_count=bus_data['stop_count'],
                    start_geo=GeoLocation(**bus_data['start_geo']) if bus_data.get('start_geo') else None,
                    end_geo=GeoLocation(**bus_data['end_geo']) if bus_data.get('end_geo') else None,
                    path_stops=path_objs
                ))
            return updated_group

        tasks = [_process_group(sid, buses) for sid, buses in sid_groups.items()]
        results = await asyncio.gather(*tasks)
        
        # Flatten and sort
        flat_list = [b for group in results for b in group]
        flat_list.sort(key=lambda x: x.raw_time)
        return flat_list

    async def plan_route(self, start_name: str, end_name: str) -> List[BusInfo]:
        """主入口：規劃路徑"""
        logger.info(f"開始規劃: {start_name} -> {end_name}")
        
        async with aiohttp.ClientSession() as session:
            client = TaipeiBusClient(session)

            # 1. 檢查快取
            cached_data = self.cache.get(start_name, end_name)
            if cached_data:
                logger.info("命中快取，更新即時時間...")
                return await self.update_cached_buses(client, cached_data)

            # 2. 準備站點 ID
            start_sids = self.repo.get_representative_sids(start_name)
            end_sids_full = self.repo.get_sids_by_name(end_name)
            end_sids_rep = self.repo.get_representative_sids(end_name)
            end_sids_set = set(end_sids_full)
            
            if not start_sids or not end_sids_rep:
                logger.warning(f"找不到站點資料: {start_name} 或 {end_name}")
                return []

            # 3. 平行查詢：起點的所有車次 + 終點的所有經過路線
            # 這是為了過濾掉不可能到達終點的路線，減少後續 HTML 請求
            t_start = asyncio.gather(*[self.fetch_realtime_at_sid(client, sid) for sid in start_sids])
            t_end = asyncio.gather(*[client.get_stop_html(sid) for sid in end_sids_rep])
            
            res_start, res_end_html = await asyncio.gather(t_start, t_end)
            
            # 解析終點經過的所有 RID
            end_rids_union = set()
            for html in res_end_html:
                if html:
                    end_rids_union.update(HtmlParser.extract_rids_from_stop(html))

            # 篩選候選車次
            candidates = [b for sublist in res_start for b in sublist]
            valid_candidates = [c for c in candidates if c['rid'] in end_rids_union]
            
            if not valid_candidates:
                return []

            logger.info(f"驗證 {len(valid_candidates)} 條潛在路線詳細路徑...")
            
            # 4. 取得路線詳細資料並驗證方向
            # 使用 set 來避免同一個 RID 重複抓取 (雖然有 memory cache，但減少 task 建立更好)
            unique_rids = {c['rid'] for c in valid_candidates}
            await asyncio.gather(*[self.get_route_structure(client, rid) for rid in unique_rids])

            final_buses = []
            seen_keys = set() # (rid, sid)

            for cand in valid_candidates:
                rid, sid = cand['rid'], cand['sid']
                if (rid, sid) in seen_keys:
                    continue
                
                route_struct = await self.get_route_structure(client, rid)
                
                # 先試去程
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
                
                if path:
                    seen_keys.add((rid, sid))
                    
                    # 建立含座標的完整路徑
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
                        sid=sid,
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
    start_spot = "捷運公館站"
    end_spot = "師大"

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
    async with aiohttp.ClientSession() as session:
        buses = await service.fetch_realtime_at_sid(TaipeiBusClient(session), "11457")
        for bus in buses:
            print(f"{bus['route']} - {bus['time_text']}, RID: {bus['rid']}, SID: {bus['sid']}")

if __name__ == "__main__":
    # Windows 平台 asyncio 策略修正
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())