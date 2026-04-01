# -*- coding: utf-8 -*-
"""
高德地图服务

设计原则：
1. 后端直调高德 REST API，Key 完全自控
2. 返回结构化文本注入 system prompt，模型基于真实数据回答（反幻觉）
3. LLM 提取意图，正则降级兜底
4. 按需触发，不影响普通对话
"""

import os
import re
import json
import asyncio
from typing import Optional

import aiohttp
from openai import AsyncOpenAI

from src.utils.logger import get_logger

logger = get_logger(__name__)

AMAP_BASE = "https://restapi.amap.com"


def _get_key() -> str:
    return os.getenv("AMAP_KEY", "")


# ── 基础 HTTP ─────────────────────────────────────────────────────────────────

async def _get(path: str, params: dict) -> Optional[dict]:
    """基础 GET 请求，带超时和错误处理。"""
    key = _get_key()
    if not key:
        logger.warning("amap: AMAP_KEY not configured")
        return None
    params["key"] = key
    params["output"] = "JSON"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{AMAP_BASE}{path}", params=params, timeout=timeout
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("status") == "1":
                        return data
                    logger.warning(f"amap: api error | info={data.get('info')} path={path}")
    except asyncio.TimeoutError:
        logger.warning(f"amap: request timeout | path={path}")
    except Exception as e:
        logger.warning(f"amap: request failed | path={path} error={e}")
    return None


async def _geocode(address: str, city: str = "") -> Optional[str]:
    """地名转经纬度，返回 'lon,lat' 字符串。"""
    params: dict = {"address": address}
    if city:
        params["city"] = city
    data = await _get("/v3/geocode/geo", params)
    if data and data.get("geocodes"):
        return data["geocodes"][0].get("location")
    return None


# ── 查询函数 ──────────────────────────────────────────────────────────────────

async def query_transit(origin: str, destination: str, city: str) -> str:
    """
    公交/地铁换乘查询。
    origin/destination 支持地名或 'lon,lat' 经纬度。
    返回格式化换乘方案文本，无结果返回空字符串。
    """
    coord_pattern = r"^\d+\.\d+,\d+\.\d+$"

    origin_coord = (
        origin if re.match(coord_pattern, origin) else await _geocode(origin, city) or ""
    )
    dest_coord = (
        destination
        if re.match(coord_pattern, destination)
        else await _geocode(destination, city) or ""
    )

    params: dict = {"city": city, "cityd": city, "strategy": 0}

    if origin_coord:
        params["origin"] = origin_coord
    else:
        search = await _get("/v3/place/text", {"keywords": origin, "city": city, "offset": 1})
        if search and search.get("pois"):
            params["origin"] = search["pois"][0].get("location", "")
        else:
            return ""

    if dest_coord:
        params["destination"] = dest_coord
    else:
        search = await _get(
            "/v3/place/text", {"keywords": destination, "city": city, "offset": 1}
        )
        if search and search.get("pois"):
            params["destination"] = search["pois"][0].get("location", "")
        else:
            return ""

    if not params.get("origin") or not params.get("destination"):
        return ""

    data = await _get("/v3/direction/transit/integrated", params)
    if not data:
        return ""

    transits = data.get("route", {}).get("transits", [])
    logger.info(
        f"amap transit | origin={origin_coord} dest={dest_coord} plans={len(transits)}"
    )
    if not transits:
        return ""

    results = []
    for i, t in enumerate(transits[:3]):
        duration = int(t.get("duration", 0)) // 60
        walking_distance = int(t.get("walking_distance", 0))
        cost = t.get("cost", "")

        segments = []
        for seg in t.get("segments", []):
            lines = seg.get("bus", {}).get("buslines", [])
            walking = seg.get("walking", {})
            if lines:
                line = lines[0]
                dep = line.get("departure_stop", {}).get("name", "")
                arr = line.get("arrival_stop", {}).get("name", "")
                via = line.get("via_num", 0)
                segments.append(f"乘{line.get('name', '')}（{dep}→{arr}，经过{via}站）")
            elif walking and int(walking.get("distance", 0)) > 50:
                segments.append(f"步行{walking.get('distance', '')}米")

        if segments:
            plan = f"方案{i + 1}：{'→'.join(segments)}，全程约{duration}分钟"
            if cost:
                plan += f"，票价{cost}元"
            if walking_distance:
                plan += f"，步行{walking_distance}米"
            results.append(plan)

    return "\n".join(results) if results else ""


async def query_weather(city: str) -> str:
    """天气查询，返回格式化文本。"""
    data = await _get("/v3/weather/weatherInfo", {"city": city, "extensions": "base"})
    if not data:
        return ""
    lives = data.get("lives", [])
    if not lives:
        return ""
    w = lives[0]
    return (
        f"{w.get('city', city)}当前天气：{w.get('weather', '')}，"
        f"气温{w.get('temperature', '')}℃，"
        f"{w.get('winddirection', '')}风{w.get('windpower', '')}级，"
        f"湿度{w.get('humidity', '')}%"
    )


async def query_around(
    keyword: str, location: str, city: str, radius: int = 3000
) -> str:
    """
    周边搜索。
    location 支持地名或经纬度，返回 POI 列表文本。
    """
    coord_pattern = r"^\d+\.\d+,\d+\.\d+$"
    if re.match(coord_pattern, location):
        loc_coord: str | None = location
    else:
        loc_coord = await _geocode(location, city)
        if not loc_coord:
            search = await _get(
                "/v3/place/text", {"keywords": location, "city": city, "offset": 1}
            )
            if search and search.get("pois"):
                loc_coord = search["pois"][0].get("location", "")

    if not loc_coord:
        return ""

    data = await _get(
        "/v3/place/around",
        {
            "keywords": keyword,
            "location": loc_coord,
            "radius": radius,
            "sortrule": "distance",
            "offset": 5,
        },
    )
    if not data:
        return ""

    pois = data.get("pois", [])
    if not pois:
        return f"{location}附近没有找到相关的{keyword}"

    results = []
    for p in pois:
        item = f"- {p.get('name', '')}"
        if p.get("distance"):
            item += f"（{p['distance']}米）"
        if p.get("address"):
            item += f"，地址：{p['address']}"
        if p.get("tel"):
            item += f"，电话：{p['tel']}"
        results.append(item)

    return f"{location}附近的{keyword}：\n" + "\n".join(results)


async def query_text_search(keyword: str, city: str) -> str:
    """关键词搜索地点，返回格式化文本。"""
    data = await _get(
        "/v3/place/text",
        {"keywords": keyword, "city": city, "citylimit": "true", "offset": 5},
    )
    if not data:
        return ""
    pois = data.get("pois", [])
    if not pois:
        return f"{city}没有找到：{keyword}"

    results = []
    for p in pois:
        item = f"- {p.get('name', '')}"
        if p.get("address"):
            item += f"，地址：{p['address']}"
        if p.get("tel"):
            item += f"，电话：{p['tel']}"
        results.append(item)

    return f"搜索「{keyword}」结果：\n" + "\n".join(results)


# ── LLM 意图提取 ──────────────────────────────────────────────────────────────

async def _llm_extract_intent(user_text: str, user_city: str) -> dict:
    """
    用轻量 LLM 提取地图查询意图，替代脆弱的正则。
    返回: {"intent": str, "origin": str, "destination": str, "city": str,
           "keyword": str, "location": str, "stops": list}
    """
    try:
        client = AsyncOpenAI(
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key=os.getenv("DOUBAO_API_KEY"),
        )
        extract_model = os.getenv("LLM_EXTRACT_MODEL", os.getenv("DOUBAO_MAIN_EP", ""))
        prompt = (
            f"从用户消息中提取地图查询意图，只输出JSON，不要解释。\n\n"
            f"用户城市背景：{user_city or '未知'}\n"
            f"用户消息：{user_text}\n\n"
            "输出格式：\n"
            '{{\n'
            '  "intent": "transit | weather | around | search | transit+around | none",\n'
            '  "stops": ["出发地", "中转地1", "目的地"],\n'
            '  "origin": "出发地",\n'
            '  "destination": "最终目的地",\n'
            '  "city": "涉及城市，没有就用用户城市背景",\n'
            '  "keyword": "搜索关键词，多个用顿号分隔，不能遗漏任何搜索目标",\n'
            '  "location": "周边搜索的中心地点（目的地附近时填目的地）"\n'
            "}}\n\n"
            "intent 说明：\n"
            "- transit：只问路线\n"
            "- around：只问周边\n"
            "- transit+around：同时问路线和目的地周边\n"
            "- weather：天气\n"
            "- search：查具体地点\n"
            "- none：无地图需求\n\n"
            "关键要求：\n"
            "- stops 包含所有途经点，顺序正确，至少2个元素\n"
            "- transit+around 时 location 默认填 destination\n"
            "- keyword 包含所有搜索目标，不能遗漏\n"
            "- 只输出 JSON，不要任何其他文字"
        )

        resp = await client.chat.completions.create(
            model=extract_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"amap: llm intent extraction failed, falling back to regex: {e}")
        return {}


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def build_amap_context(user_text: str, user_city: str = "") -> str:
    """
    主入口：LLM 提取意图 → 调对应高德接口 → 返回注入 system prompt 的真实数据文本。
    失败时降级到正则提取。
    """
    text = user_text.strip()
    fallback_city = user_city or _extract_city(text) or "深圳"

    intent_data = await _llm_extract_intent(text, fallback_city)
    intent = intent_data.get("intent", "none")
    city = intent_data.get("city") or fallback_city

    logger.info(f"amap: intent={intent} city={city}")

    if intent == "transit+around":
        stops = intent_data.get("stops", [])
        origin = intent_data.get("origin", "")
        dest = intent_data.get("destination", "")
        keyword = intent_data.get("keyword", "")
        location = intent_data.get("location") or dest or city

        if not stops:
            stops = [origin, dest] if origin and dest else []

        transit_task = None
        around_tasks = []

        if len(stops) >= 2:
            async def _do_transit() -> str:
                segs = []
                for i in range(len(stops) - 1):
                    r = await query_transit(stops[i], stops[i + 1], city)
                    if r:
                        label = (
                            f"第{i+1}段（{stops[i]}→{stops[i+1]}）：\n{r}"
                            if len(stops) > 2
                            else f"从{stops[i]}到{stops[i+1]}的公交方案：\n{r}"
                        )
                        segs.append(label)
                return "\n\n".join(segs) if segs else ""

            transit_task = _do_transit()

        if keyword and location:
            keywords = [k.strip() for k in re.split(r"或者|或|和|、|,|，", keyword) if k.strip()]
            around_tasks = [query_around(kw, location, city, radius=5000) for kw in keywords]

        all_tasks = ([transit_task] if transit_task else []) + around_tasks
        if all_tasks:
            results = await asyncio.gather(*all_tasks, return_exceptions=True)
            parts = []
            transit_result = results[0] if transit_task else None
            around_results = list(results[1:] if transit_task else results)

            if transit_result and not isinstance(transit_result, Exception) and transit_result:
                parts.append(f"【高德实时路线数据】\n{transit_result}")
            for r in around_results:
                if r and not isinstance(r, Exception):
                    parts.append(f"【高德实时周边数据】\n{r}")
            if parts:
                return "\n\n".join(parts)

    if intent == "transit":
        stops = intent_data.get("stops", [])
        origin = intent_data.get("origin", "")
        dest = intent_data.get("destination", "")
        if not stops:
            stops = [origin, dest] if origin and dest else []
        elif len(stops) < 2 and origin and dest:
            stops = [origin, dest]

        if len(stops) >= 2:
            all_segments = []
            for i in range(len(stops) - 1):
                result = await query_transit(stops[i], stops[i + 1], city)
                if result:
                    label = (
                        f"第{i+1}段（{stops[i]}→{stops[i+1]}）：\n{result}"
                        if len(stops) > 2
                        else f"从{stops[i]}到{stops[i+1]}的公交方案：\n{result}"
                    )
                    all_segments.append(label)
            if all_segments:
                return "【高德实时路线数据】\n" + "\n\n".join(all_segments)

    elif intent == "weather":
        result = await query_weather(city)
        if result:
            return f"【高德实时天气数据】\n{result}"

    elif intent == "around":
        keyword = intent_data.get("keyword", "")
        location = intent_data.get("location") or city
        if keyword:
            keywords = [k.strip() for k in re.split(r"或者|或|和|、|,|，", keyword) if k.strip()]
            all_results = []
            for kw in keywords or [keyword]:
                r = await query_around(kw, location, city, radius=5000)
                if r:
                    all_results.append(r)
            if all_results:
                return "【高德实时周边数据】\n" + "\n\n".join(all_results)

    elif intent == "search":
        keyword = intent_data.get("keyword", "")
        if keyword:
            result = await query_text_search(keyword, city)
            if result:
                return f"【高德实时地点数据】\n{result}"

    # LLM 返回 none 或提取失败时，降级到正则
    if not intent or intent == "none":
        return await _build_amap_context_regex(text, fallback_city)

    return ""


# ── 正则降级 ──────────────────────────────────────────────────────────────────

async def _build_amap_context_regex(text: str, city: str) -> str:
    """降级方案：正则提取意图（LLM 失败时使用）。"""
    # 天气
    if any(kw in text for kw in ["天气", "下雨", "气温", "几度", "要带伞", "会下雨"]):
        result = await query_weather(_extract_city(text) or city)
        if result:
            return f"【高德实时天气数据】\n{result}"

    # 换乘
    if any(kw in text for kw in ["怎么去", "怎么走", "怎么坐", "坐地铁", "坐公交", "换乘", "路线"]):
        origin, dest = _extract_od(text)
        if origin and dest:
            result = await query_transit(origin, dest, city)
            if result:
                return f"【高德实时路线数据】从{origin}到{dest}的公交方案：\n{result}"

    # 周边
    if any(kw in text for kw in ["附近", "周边", "旁边", "哪里有"]):
        keyword = _extract_search_keyword(text)
        location = _extract_location(text) or city
        if keyword:
            result = await query_around(keyword, location, city)
            if result:
                return f"【高德实时周边数据】\n{result}"

    # 地点
    if any(kw in text for kw in ["在哪", "地址", "怎么找"]):
        keyword = _extract_search_keyword(text)
        if keyword:
            result = await query_text_search(keyword, city)
            if result:
                return f"【高德实时地点数据】\n{result}"

    return ""


# ── 正则辅助 ──────────────────────────────────────────────────────────────────

def _extract_city(text: str) -> str:
    cities = [
        "北京", "上海", "广州", "深圳", "成都", "杭州", "武汉", "南京", "西安",
        "重庆", "天津", "苏州", "长沙", "郑州", "青岛", "厦门", "宁波", "合肥",
        "昆明", "哈尔滨", "沈阳", "大连", "济南", "福州", "贵阳", "太原", "石家庄",
        "南昌", "南宁", "乌鲁木齐", "兰州", "银川", "西宁", "海口", "三亚",
        "东莞", "佛山", "中山", "珠海", "惠州", "无锡", "常州", "南通",
    ]
    for city in cities:
        if city in text:
            return city
    return ""


def _extract_od(text: str) -> tuple[str, str]:
    noise = r"(?:怎么|如何|路线|换乘|最近|附近|步行|驾车|骑行|公交|地铁|$)"
    patterns = [
        rf"从(.{{2,15}})[到去至](.{{2,15}}?){noise}",
        rf"(.{{2,15}})[到去至](.{{2,15}}?){noise}",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            origin = re.sub(r"[的地]+$", "", m.group(1).strip())
            dest = re.sub(r"[的地最近附近步行驾车骑行]+$", "", m.group(2).strip())
            if len(origin) >= 2 and len(dest) >= 2:
                return origin, dest
    return "", ""


def _extract_location(text: str) -> str:
    m = re.search(r"(.{2,15}?)(?:附近|周边|旁边)", text)
    return m.group(1).strip() if m else ""


def _extract_search_keyword(text: str) -> str:
    clean = text
    city = _extract_city(text)
    if city:
        clean = clean.replace(city, "")
    clean = re.sub(r"[一-龥]{1,4}[市区县镇街道]", "", clean)
    clean = re.sub(r"^[市省区县镇]", "", clean).strip()
    clean = re.sub(
        r"附近|周边|旁边|在哪|地址|怎么找|哪里有|有没有|有吗|么|吗|呢|啊|哦", "", clean
    )
    clean = re.sub(r"帮我找|帮我搜|我想找|我要找|查一下|找一下", "", clean)
    clean = re.sub(r"[？?。，,！!\s]", "", clean).strip()
    return clean if len(clean) >= 2 else ""
