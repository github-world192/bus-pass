import axios from 'axios';
import * as cheerio from 'cheerio';
import AsyncStorage from '@react-native-async-storage/async-storage';

// 注意：請確保 stop_id_map.json 位於 assets 資料夾，
// 並且在 tsconfig.json 中設定 "resolveJsonModule": true
import stopDataRaw from './stop_id_map.json'; 

// ==========================================
// 1. 型別定義 (Type Definitions)
// ==========================================

export interface GeoLocation {
  lat: number;
  lon: number;
}

export interface StopInfo {
  name: string;
  sid: string;
  geo?: GeoLocation;
  slid?: string;
}

export interface BusInfo {
  route_name: string;
  rid: string;
  sid: string;
  arrival_time_text: string;
  raw_time: number;
  direction_text: string;
  stop_count: number;
  start_geo?: GeoLocation;
  end_geo?: GeoLocation;
  path_stops: StopInfo[];
}

interface StopMapData {
  by_sid: { [key: string]: any };
  by_name: { [key: string]: string[] };
}

// ==========================================
// 2. 配置與常數 (Constants)
// ==========================================

const BASE_URL = "https://pda5284.gov.taipei/MQS";
// 模擬 iPhone Safari 的 User Agent 以確保網頁版面正確
const USER_AGENT = 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1';

const STATUS_CODES: { [key: string]: string } = {
  '0': '進站中', '': '未發車', '-1': '未發車', '-2': '交管不停', '-3': '末班已過', '-4': '今日未營運'
};

// ==========================================
// 3. 資料提供者介面與實作 (Data Provider)
// ==========================================

interface IDataProvider {
  loadData(): Promise<StopMapData>;
  loadCache(): Promise<any>;
  saveCache(data: any): Promise<void>;
}

class ExpoDataProvider implements IDataProvider {
  private cacheKey: string;

  constructor(cacheKey: string = 'route_validation_cache') {
    this.cacheKey = cacheKey;
  }

  // 載入靜態站點資料庫
  async loadData(): Promise<StopMapData> {
    // 在 Expo 中，import 的 JSON 會直接變成物件
    return stopDataRaw as unknown as StopMapData;
  }

  // 讀取快取 (AsyncStorage)
  async loadCache(): Promise<any> {
    try {
      const jsonValue = await AsyncStorage.getItem(this.cacheKey);
      return jsonValue != null ? JSON.parse(jsonValue) : {};
    } catch(e) {
      console.warn("讀取快取失敗", e);
      return {};
    }
  }

  // 儲存快取 (AsyncStorage)
  async saveCache(data: any): Promise<void> {
    try {
      const jsonValue = JSON.stringify(data);
      await AsyncStorage.setItem(this.cacheKey, jsonValue);
    } catch (e) {
      console.error("寫入快取失敗", e);
    }
  }
}

// ==========================================
// 4. 核心邏輯服務 (Core Service)
// ==========================================

export class BusPlannerService {
  private dataProvider: IDataProvider;
  private stopMap: StopMapData = { by_sid: {}, by_name: {} };
  private routeCache: { [key: string]: any } = {}; // 執行期間的記憶體快取 (路線詳情)
  private validationCache: { [key: string]: any } = {}; // 持久化的路徑快取

  constructor() {
    this.dataProvider = new ExpoDataProvider();
  }

  /**
   * 初始化：載入資料庫與快取
   * 建議在 App 啟動或第一次搜尋前呼叫
   */
  async initialize() {
    this.stopMap = await this.dataProvider.loadData();
    this.validationCache = await this.dataProvider.loadCache();
  }

  // --- 基礎查找 Helpers ---

  getSidsByName(name: string): string[] {
    return this.stopMap.by_name[name] || [];
  }

  getSlidBySid(sid: string): string | null {
    const info = this.stopMap.by_sid[sid];
    return info ? info.slid : null;
  }

  getGeoBySid(sid: string): GeoLocation | undefined {
    const info = this.stopMap.by_sid[sid];
    if (info && info.lat) {
      return { lat: parseFloat(info.lat), lon: parseFloat(info.lon) };
    }
    return undefined;
  }

  /**
   * 取得代表性 SIDs (優化關鍵)
   * 針對某個站名，依據 SLID 去重，避免對同一地點重複查詢
   */
  getRepresentativeSids(name: string): string[] {
    const allSids = this.getSidsByName(name);
    if (!allSids.length) return [];

    const seenSlids = new Set<string>();
    const representatives: string[] = [];

    for (const sid of allSids) {
      const slid = this.getSlidBySid(sid);
      // 如果有 SLID 且沒看過，則選為代表
      if (slid && !seenSlids.has(slid)) {
        seenSlids.add(slid);
        representatives.push(sid);
      } 
      // 如果資料庫缺失 SLID (極少見)，為了保險起見也納入
      else if (!slid) {
        representatives.push(sid);
      }
    }
    return representatives;
  }

  // --- 時間處理 ---

  private parseTimeText(text: string): number {
    text = text.trim();
    if (text.includes("進站")) return 0;
    if (text.includes("即將")) return 10;
    if (text.includes("分")) {
      const num = parseInt(text.replace(/\D/g, ''));
      return isNaN(num) ? 99999 : num * 60;
    }
    return 99999;
  }

  private formatTimeFromCode(code: string): string {
    if (code === '0') return "進站中";
    if (STATUS_CODES[code]) return STATUS_CODES[code];
    try {
      const secs = parseInt(code);
      if (secs < 0) return STATUS_CODES[code] || "未發車";
      if (secs < 180) return "將到站";
      return `${Math.floor(secs / 60)}分`;
    } catch { return "未發車"; }
  }

  // --- HTTP Fetchers ---

  private async fetchHtml(url: string): Promise<string | null> {
    try {
      const res = await axios.get(url, { headers: { 'User-Agent': USER_AGENT }, timeout: 8000 });
      return res.status === 200 ? (typeof res.data === 'string' ? res.data : JSON.stringify(res.data)) : null;
    } catch (e) { return null; }
  }

  private async fetchJson(url: string): Promise<any | null> {
    try {
      const res = await axios.get(url, { headers: { 'User-Agent': USER_AGENT }, timeout: 8000 });
      return res.status === 200 ? res.data : null;
    } catch (e) { return null; }
  }

  // --- 爬蟲邏輯 ---

  /**
   * 爬取特定 SID 的公車列表 (混合 HTML 與 JSON)
   */
  async fetchBusesAtSid(sid: string): Promise<any[]> {
    const info = this.stopMap.by_sid[sid];
    if (!info) return [];
    const slid = info.slid;

    const urlHtml = `${BASE_URL}/stop.jsp?sid=${sid}`;
    const urlJson = slid ? `${BASE_URL}/StopLocationDyna?stoplocationid=${slid}` : null;

    const promises: Promise<any>[] = [this.fetchHtml(urlHtml)];
    if (urlJson) promises.push(this.fetchJson(urlJson));

    const results = await Promise.all(promises);
    const html = results[0] as string | null;
    const jsonData = (results.length > 1 ? results[1] : null) as any; 

    if (!html) return [];

    const $ = cheerio.load(html);
    const routeMap: { [key: string]: any } = {};

    // 解析 HTML 表格取得路線基本資訊與 RID
    $('tr').each((_, row) => {
      const link = $(row).find('a[href*="route.jsp"]');
      if (link.length === 0) return;

      let dynId: string | null = null;
      $(row).find('td').each((_, td) => {
        const id = $(td).attr('id');
        if (id && id.startsWith('tte')) {
          dynId = id.replace('tte', '');
        }
      });

      if (dynId) {
        const href = link.attr('href') || '';
        const query = href.split('?')[1] || '';
        const params = new URLSearchParams(query);
        const rid = params.get('rid') || '';
        routeMap[dynId] = { route: link.text().trim(), rid, sid };
      }
    });

    const buses: any[] = [];

    // 整合 JSON 動態時間
    if (jsonData && jsonData.Stop) {
      for (const stopItem of jsonData.Stop) {
        const vals = (stopItem.n1 || "").split(',');
        if (vals.length < 8) continue;
        
        const jDynId = vals[1];
        const jTime = vals[7];

        if (routeMap[jDynId]) {
            const info = routeMap[jDynId];
            const timeText = this.formatTimeFromCode(jTime);
            buses.push({
                route: info.route,
                rid: info.rid,
                sid: sid,
                time_text: timeText,
                raw_time: this.parseTimeText(timeText)
            });
            delete routeMap[jDynId];
        }
      }
    }

    // 處理剩下只有 HTML 但 JSON 沒更新到的路線
    for (const key in routeMap) {
        const info = routeMap[key];
        buses.push({
            route: info.route,
            rid: info.rid,
            sid: sid,
            time_text: "無法取得",
            raw_time: 99999
        });
    }

    return buses;
  }

  async getRidsAtSid(sid: string): Promise<Set<string>> {
    const buses = await this.fetchBusesAtSid(sid);
    return new Set(buses.map(b => b.rid));
  }

  /**
   * 取得路線完整站點詳情 (快取機制)
   */
  async getRouteDetail(rid: string): Promise<any> {
    if (this.routeCache[rid]) return this.routeCache[rid];

    const html = await this.fetchHtml(`${BASE_URL}/route.jsp?rid=${rid}`);
    if (!html) return {};
    
    const $ = cheerio.load(html);

    const parseStops = (classRegex: RegExp) => {
        const stops: any[] = [];
        $('tr').each((_, row) => {
            const className = $(row).attr('class');
            if (className && classRegex.test(className)) {
                const link = $(row).find('a');
                if (link.length) {
                    const href = link.attr('href') || '';
                    const params = new URLSearchParams(href.split('?')[1]);
                    stops.push({
                        name: link.text().trim(),
                        slid: params.get('slid') || ''
                    });
                }
            }
        });
        return stops;
    };

    const data = {
        go_stops: parseStops(/ttego\d+/),
        back_stops: parseStops(/tteback\d+/)
    };
    
    this.routeCache[rid] = data;
    return data;
  }

  /**
   * 驗證路徑並截取該段站點
   * 確保方向正確 (currentSlid 必須與路線上的 SLID 一致)
   */
  getVerifiedPath(stops: any[], startName: string, endName: string, currentSlid: string | null): any[] | null {
    const startIndices = stops.map((s, i) => s.name === startName ? i : -1).filter(i => i !== -1);
    if (startIndices.length === 0) return null;

    let candidates: any[][] = [];

    for (const idx of startIndices) {
        const stopSlid = stops[idx].slid;
        // 方向驗證：如果我們所在的 SLID 與路線上的 SLID 不符，表示方向相反
        if (currentSlid && stopSlid && currentSlid !== stopSlid) continue;

        const subList = stops.slice(idx);
        const endRelIndex = subList.findIndex((s: any) => s.name === endName);
        
        if (endRelIndex !== -1) {
            candidates.push(subList.slice(0, endRelIndex + 1));
        }
    }

    if (candidates.length === 0) return null;
    // 選擇站數最少的路徑
    return candidates.reduce((a, b) => a.length < b.length ? a : b);
  }

  // ==========================================
  // 5. 主功能入口 (Main Plan Function)
  // ==========================================

  async plan(startStation: string, endStation: string): Promise<BusInfo[]> {
    const cacheKey = `${startStation}|${endStation}`;
    
    // TODO: 若命中快取，可在此實作「僅更新時間」的邏輯 (fetchBusesAtSid 並比對 RID)
    // 目前版本若有快取則直接回傳上次結果 (僅作示範)
    // if (this.validationCache[cacheKey]) {
    //     return this.validationCache[cacheKey];
    // }

    // 1. 取得代表性 SIDs (減少請求數)
    const startRepSids = this.getRepresentativeSids(startStation);
    const endRepSids = this.getRepresentativeSids(endStation);

    if (startRepSids.length === 0 || endRepSids.length === 0) return [];

    // 2. 併發查詢：起點公車列表 & 終點路線列表
    const startTask = Promise.all(startRepSids.map(sid => this.fetchBusesAtSid(sid)));
    const endTask = Promise.all(endRepSids.map(sid => this.getRidsAtSid(sid)));

    const [startResultsNested, endRidsSets] = await Promise.all([startTask, endTask]);

    // 3. 計算交集 (Intersection)
    const endRidsUnion = new Set<string>();
    endRidsSets.forEach(set => set.forEach(rid => endRidsUnion.add(rid)));

    const allStartCandidates = startResultsNested.flat();
    const ridFiltered = allStartCandidates.filter(b => endRidsUnion.has(b.rid));

    // 4. 驗證路徑詳情 (Route Validation)
    const validBuses: BusInfo[] = [];
    const seenRoutes = new Set<string>();
    
    // 依時間排序優先處理
    ridFiltered.sort((a, b) => a.raw_time - b.raw_time);

    for (const cand of ridFiltered) {
        if (seenRoutes.has(cand.rid)) continue;

        const routeData = await this.getRouteDetail(cand.rid);
        const candSlid = this.getSlidBySid(cand.sid);

        let direction = "未知";
        let pathStopsData = this.getVerifiedPath(routeData.go_stops, startStation, endStation, candSlid);
        
        if (pathStopsData) {
            direction = "去程";
        } else {
            pathStopsData = this.getVerifiedPath(routeData.back_stops, startStation, endStation, candSlid);
            if (pathStopsData) direction = "返程";
        }

        if (pathStopsData) {
            seenRoutes.add(cand.rid);
            
            // 建構完整路徑資訊
            const enhancedPath: StopInfo[] = pathStopsData.map((p: any) => {
                const refSid = (this.getSidsByName(p.name) || [])[0] || "";
                return {
                    name: p.name,
                    sid: refSid,
                    geo: this.getGeoBySid(refSid)
                };
            });

            const startGeo = this.getGeoBySid(cand.sid);
            const endRefSid = endRepSids[0] || "";
            const endGeo = this.getGeoBySid(endRefSid);

            validBuses.push({
                route_name: cand.route,
                rid: cand.rid,
                sid: cand.sid,
                arrival_time_text: cand.time_text,
                raw_time: cand.raw_time,
                direction_text: direction,
                stop_count: enhancedPath.length - 1,
                start_geo: startGeo,
                end_geo: endGeo,
                path_stops: enhancedPath
            });
        }
    }

    // 5. 寫入快取
    if (validBuses.length > 0) {
        const cacheData = validBuses.map(b => {
            const { arrival_time_text, raw_time, ...rest } = b;
            return rest;
        });
        this.validationCache[cacheKey] = cacheData;
        await this.dataProvider.saveCache(this.validationCache);
    }

    return validBuses;
  }
}