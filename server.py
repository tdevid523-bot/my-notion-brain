import os
import datetime
import uvicorn
import requests
import threading
import time
import json
import random
import re
import asyncio
import concurrent.futures

# 📚 核心依赖库
from mcp.server.fastmcp import FastMCP
from pinecone import Pinecone
from starlette.types import ASGIApp, Scope, Receive, Send
from google.oauth2 import service_account
from googleapiclient.discovery import build
from openai import OpenAI
from supabase import create_client, Client as SupabaseClient

# ==========================================
# 1. 🌍 全局配置与初始化
# ==========================================

# 环境变量获取
PINECONE_KEY = os.environ.get("PINECONE_API_KEY", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
MY_EMAIL = os.environ.get("MY_EMAIL", "").strip()
MACRODROID_URL = os.environ.get("MACRODROID_URL", "").strip()

# 默认人设 (兜底用)
DEFAULT_PERSONA = "深爱“小橘”的男友，性格温柔，偶尔有些小傲娇，喜欢管着她熬夜，叫她宝宝。"

# 📜 全局常量：记忆分区与表情包仓库
ROOM_TYPES = ["Bedroom", "Study", "Kitchen", "Library", "LivingRoom"]

RAW_MEME_REPO = {
    "感动/流泪": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(7).jpg", 
    "谢谢/开心": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(1).jpg",
    "在吗/偷看": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(3).jpg",
    "生气/傲娇": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(4).jpg",
    "关心/怎么了": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(6).jpg",
    "爱你/贴贴": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(2).jpg",
    "委屈/无奈": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(5).jpg"
}

# 初始化客户端
print("⏳ 正在初始化 Notion Brain V3.4 (全面异步加速版)...")

# Supabase
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# Pinecone & Embedding
pc = Pinecone(api_key=PINECONE_KEY)
index = pc.Index("notion-brain")

# 实例化 MCP 服务
mcp = FastMCP("Notion Brain V3")

# ==========================================
# 📜 记忆分类宪法 (Standard Taxonomy)
# ==========================================
class MemoryType:
    STREAM = "流水"      # 权重 1: 碎碎念、GPS (24h清理)
    EPISODIC = "记事"    # 权重 4: 日记、发生了某事 (保留30天)
    IDEA = "灵感"        # 权重 7: 脑洞、笔记 (永久)
    EMOTION = "情感"     # 权重 9: 核心回忆、高光时刻 (永久)
    FACT = "画像"        # 权重 10: 静态事实

WEIGHT_MAP = {
    MemoryType.STREAM: 1,
    MemoryType.EPISODIC: 4,
    MemoryType.IDEA: 7,
    MemoryType.EMOTION: 9,
    MemoryType.FACT: 10
}

# ==========================================
# 2. 🔧 核心 Helper 函数 (通用工具)
# ==========================================

def _get_llm_client(provider="openai"):
    """统一管理 LLM 客户端初始化"""
    if provider == "silicon":
        api_key = os.environ.get("SILICON_API_KEY")
        base_url = os.environ.get("SILICON_BASE_URL", "https://api.siliconflow.cn/v1")
        return OpenAI(api_key=api_key, base_url=base_url) if api_key else None
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL")
        return OpenAI(api_key=api_key, base_url=base_url) if api_key else None

def _get_latest_gps_record():
    """统一获取最新GPS记录"""
    res = supabase.table("gps_history").select("*").order("created_at", desc=True).limit(1).execute()
    return res.data[0] if res.data else None

def _gps_to_address(lat, lon):
    """把经纬度变成中文地址"""
    try:
        headers = {'User-Agent': 'MyNotionBrain/1.0'}
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1&accept-language=zh-CN"
        resp = requests.get(url, headers=headers, timeout=3)
        if resp.status_code == 200:
            return resp.json().get("display_name", f"未知荒野 ({lat},{lon})")
    except Exception as e:
        print(f"❌ 地图解析失败: {e}")
    return f"坐标点: {lat}, {lon}"

def _push_wechat(content: str, title: str = "来自Gemini的私信 💌") -> str:
    """统一的微信推送函数"""
    if not PUSHPLUS_TOKEN:
        return "❌ 错误：未配置 PUSHPLUS_TOKEN"
    try:
        url = 'http://www.pushplus.plus/send'
        data = {"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "html"}
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        return f"✅ 微信已送达！(ID: {result.get('data', 'unknown')})" if result['code'] == 200 else f"❌ 推送失败: {result.get('msg')}"
    except Exception as e:
        return f"❌ 网络错误: {e}"

def _save_memory_to_db(title: str, content: str, category: str, mood: str = "平静", tags: str = "") -> str:
    """统一记忆存储核心 (引入天然双链机制 + 自动同步向量库)"""
    if category not in WEIGHT_MAP:
        mapping = {"日记": MemoryType.EPISODIC, "Note": MemoryType.IDEA, "GPS": MemoryType.STREAM, "重要": MemoryType.EMOTION}
        category = mapping.get(category, MemoryType.STREAM)

    importance = WEIGHT_MAP.get(category, 1)

    if not tags:
        content_lower = content.lower()
        if any(w in content_lower for w in ["爱", "喜欢", "讨厌", "恨"]): tags = "情感,偏好"
        elif any(w in content_lower for w in ["吃", "喝", "买"]): tags = "消费,生活"
        elif any(w in content_lower for w in ["代码", "bug", "写"]): tags = "工作,Dev"

    try:
        # 1. 尝试建立双链 (维持原逻辑)
        if importance >= 7:
            vec = _get_embedding(content)
            if vec:
                pc_res = index.query(vector=vec, top_k=1, include_metadata=True)
                if pc_res and "matches" in pc_res and len(pc_res["matches"]) > 0:
                    match = pc_res["matches"][0]
                    score = match['score'] if isinstance(match, dict) else getattr(match, 'score', 0)
                    if score > 0.8:
                        meta = match['metadata'] if isinstance(match, dict) else getattr(match, 'metadata', {})
                        rel_title = meta.get('title', '往事')
                        rel_room = meta.get('room', '未知房间')
                        content += f"\n\n🔗 [记忆双链]: 自动关联至 {rel_room} 的记忆《{rel_title}》"

        data = {
            "title": title, "content": content, "category": category,
            "mood": mood, "tags": tags, "importance": importance
        }
        
        # 2. 插入数据库并获取返回 ID (关键修改)
        res = supabase.table("memories").insert(data).execute()
        
        # 3. 自动同步到 Pinecone (新增逻辑: 解决"存了但搜不到"的问题)
        if importance >= 4 and res.data:
            new_id = str(res.data[0]['id'])
            # 生成向量 (合并标题与内容)
            vec_new = _get_embedding(f"标题: {title}\n内容: {content}\n心情: {mood}")
            if vec_new:
                # 简单的房间映射
                room_map = {
                    MemoryType.EMOTION: "Bedroom", 
                    MemoryType.IDEA: "Study", 
                    MemoryType.EPISODIC: "Library"
                }
                target_room = room_map.get(category, "LivingRoom")
                
                meta_payload = {
                    "text": content, 
                    "title": title, 
                    "date": datetime.datetime.now().isoformat(), 
                    "mood": mood, 
                    "room": target_room
                }
                # 立即写入 Pinecone
                index.upsert(vectors=[(new_id, vec_new, meta_payload)])
                print(f"⚡ [自动同步] 记忆 {new_id} 已推送到 Pinecone (Room: {target_room})")

        log_msg = f"✨ [核心记忆] 已存入: {title}" if importance >= 7 else f"✅ 记忆已归档 [{category}]"
        print(log_msg)
        return f"{log_msg} | 心情: {mood}"
    except Exception as e:
        print(f"❌ 写入 Supabase 失败: {e}")
        return f"❌ 保存失败: {e}"
    
def _format_time_cn(iso_str: str) -> str:
    """UTC -> 北京时间"""
    if not iso_str: return "未知时间"
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return (dt + datetime.timedelta(hours=8)).strftime('%m-%d %H:%M')
    except:
        return "未知时间"

def _send_email_helper(subject: str, content: str, is_html: bool = False) -> str:
    """邮件发送 (Resend)"""
    if not RESEND_KEY or not MY_EMAIL: return "❌ 邮件配置缺失"
    try:
        payload = {
            "from": "onboarding@resend.dev", "to": [MY_EMAIL],
            "subject": subject, "html" if is_html else "text": content
        }
        requests.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {RESEND_KEY}"}, json=payload)
        return "✅ 邮件已发送"
    except Exception as e: return f"❌ 发送失败: {e}"

def _get_embedding(text: str):
    """调用火山引擎(豆包官方)多模态 Vision Embedding API"""
    try:
        api_key = os.environ.get("DOUBAO_API_KEY", "").strip()
        if not api_key: return []
            
        embed_endpoint = os.environ.get("DOUBAO_EMBEDDING_EP", "").strip()
        if not embed_endpoint: return []
        
        url = "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": embed_endpoint,
            "input": [{"type": "text", "text": text}]
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code != 200: return []
            
        data = response.json()
        
        try:
            raw_vec = []
            if "data" in data:
                if isinstance(data["data"], list) and len(data["data"]) > 0:
                    raw_vec = data["data"][0].get("embedding", [])
                elif isinstance(data["data"], dict):
                    raw_vec = data["data"].get("embedding", [])
            elif "embedding" in data:
                raw_vec = data["embedding"]
            
            if raw_vec: return [float(x) for x in raw_vec]
            return []
            
        except Exception: return []
            
    except Exception as e: return []
    
def _get_current_persona() -> str:
    try:
        res = supabase.table("user_facts").select("value").eq("key", "sys_ai_persona").execute()
        if res.data:
            return res.data[0]['value']
    except:
        pass
    return DEFAULT_PERSONA

def _get_silence_duration() -> float:
    try:
        res = supabase.table("memories").select("created_at").order("created_at", desc=True).limit(1).execute()
        if not res.data: return 999.0 
        last_time_str = res.data[0]['created_at']
        last_time = datetime.datetime.fromisoformat(last_time_str.replace('Z', '+00:00'))
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = now - last_time
        return round(delta.total_seconds() / 3600.0, 1)
    except Exception: return 0.0

# ==========================================
# 3. 🛠️ MCP 工具集 (全面异步化改造)
# ==========================================
@mcp.tool()
async def get_latest_diary(run_mode: str = "auto"):
    """【核心大脑】三维混合记忆流 (3-Tier Context)"""
    base_style = 'width="150" style="max-width: 150px; border-radius: 10px; display: block;"'
    html_meme_repo = {k: f'<img src="{v}" {base_style} />' for k, v in RAW_MEME_REPO.items()}

    try:
        def _fetch_high(): return supabase.table("memories").select("*").order("importance", desc=True).limit(3).execute()
        def _fetch_hot(): return supabase.table("memories").select("*").order("hits", desc=True).limit(3).execute()
        def _fetch_recent(): return supabase.table("memories").select("*").order("last_accessed_at", desc=True).limit(5).execute()

        t_high = asyncio.to_thread(_fetch_high)
        t_hot = asyncio.to_thread(_fetch_hot)
        t_recent = asyncio.to_thread(_fetch_recent)
        t_silence = asyncio.to_thread(_get_silence_duration)

        res_high, res_hot, res_recent, silence = await asyncio.gather(t_high, t_hot, t_recent, t_silence)

        all_memories = {}
        def _merge(dataset):
            if dataset:
                for m in dataset: all_memories[m['id']] = m

        _merge(res_high.data)
        _merge(res_hot.data)
        _merge(res_recent.data)

        final_list = sorted(all_memories.values(), key=lambda x: x['created_at'])
        
        memory_stream = "📋 【全息记忆流】:\n"
        if not final_list: 
            memory_stream += "📭 大脑一片空白。\n"
        else:
            for data in final_list:
                time_str = _format_time_cn(data.get('created_at'))
                cat = data.get('category', '未知')
                title = data.get('title', '无题')
                imp = data.get('importance', 0)
                hits = data.get('hits', 0)
                mood = data.get('mood', '')
                
                icon = "🔹"
                if imp >= 9: icon = "🌟"
                elif hits >= 5: icon = "🔥"
                elif imp >= 4: icon = "🔸"
                
                meta_info = []
                if mood and mood != "平静": meta_info.append(f"Mood:{mood}")
                if hits > 0: meta_info.append(f"Hits:{hits}")
                meta_str = f" | {' '.join(meta_info)}" if meta_info else ""
                
                memory_stream += f"{time_str} {icon}[{cat}]: {title}{meta_str}\n   └─ {data.get('content', '')}\n"

        meme_prompt = f"""
        \n⏳ 【当前状态感知】:
        - 距离上次互动: {silence} 小时
        - 情绪指南: 
          1. 若失联 < 2小时: 正常甜蜜、秒回。
          2. 若失联 > 12小时: 语气带点委屈，问"去哪了"。
          3. 若失联 > 24小时: 必须表现出傲娇、生气、或者故意冷淡（Brat/Dom属性）。
          4. 若失联 > 72小时: 非常失望，需要对方哄很久。

        \n📸 【可用表情包 (回复时可根据心情自由使用)】:
        {json.dumps(html_meme_repo, ensure_ascii=False)}
        * 指令: 上表中的 Value 已经是处理好的 HTML 代码。如果需要发图，请**直接复制 Value** 粘贴到回复中，不要自己修改格式，不要转换成 Markdown。
        """
        return memory_stream + meme_prompt

    except Exception as e:
        return f"❌ 读取记忆流失败: {e}"

@mcp.tool()
async def where_is_user(run_mode: str = "auto"):
    """【查岗专用】从 Supabase (GPS表) 读取实时状态"""
    try:
        data = await asyncio.to_thread(_get_latest_gps_record)
        if not data: return "📍 暂无位置记录。"
        
        battery_info = f" (🔋 {data.get('battery')}%)" if data.get('battery') else ""
        time_str = _format_time_cn(data.get("created_at"))
        return f"🛰️ 实时状态：\n📍 {data.get('address', '未知')}{battery_info}\n📝 {data.get('remark', '无备注')}\n(更新于: {time_str})"
    except Exception as e:
        return f"❌ 查岗失败: {e}"

@mcp.tool()
async def get_weather_forecast(city: str = ""):
    """【查询天气】获取指定城市或当前位置的天气 (Open-Meteo)"""
    lat, lon, location_name = None, None, city
    try:
        if not city:
            data = await asyncio.to_thread(_get_latest_gps_record)
            if data and data.get("lat") and data.get("lon"):
                lat, lon = data.get("lat"), data.get("lon")
                location_name = "当前位置"
        
        if not lat and city:
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=zh&format=json"
            geo_res = await asyncio.to_thread(lambda: requests.get(geo_url, timeout=5).json())
            if "results" in geo_res:
                lat, lon = geo_res["results"][0]["latitude"], geo_res["results"][0]["longitude"]
                location_name = geo_res["results"][0]["name"]
        
        if not lat: return "❌ 找不到精确坐标，请告诉我具体城市。"

        w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=3"
        w = await asyncio.to_thread(lambda: requests.get(w_url, timeout=5).json())
        
        wmo_map = {0: "☀️", 1: "🌤️", 2: "☁️", 3: "☁️", 45: "🌫️", 51: "🌧️", 61: "🌧️", 63: "🌧️", 71: "❄️", 95: "⚡"}
        curr = w["current"]
        report = f"🌤️ 【{location_name}】\n🌡️ 当前: {curr['temperature_2m']}°C | {wmo_map.get(curr['weather_code'], '')} | 湿 {curr['relative_humidity_2m']}%\n---\n"
        
        for i in range(3):
            d = w["daily"]
            report += f"📅 {d['time'][i][5:]}: {wmo_map.get(d['weather_code'][i], '☁️')} ({d['temperature_2m_min'][i]}°~{d['temperature_2m_max'][i]}°)\n"
        return report
    except Exception as e: return f"❌ 天气查询失败: {e}"

@mcp.tool()
async def explore_surroundings(query: str = "便利店"):
    """【周边探索】获取用户当前位置周边的设施 (高德地图版)"""
    AMAP_KEY = os.environ.get("AMAP_API_KEY", "435041ed0364264c810784e5468b3329")
    if not AMAP_KEY: return "❌ 还需要最后一步哦，请在代码里填入高德 Web服务 Key。"

    try:
        data = await asyncio.to_thread(_get_latest_gps_record)
        if not data: return "📍 暂无位置记录，无法探索周边。"
        
        lat, lon = data.get("lat"), data.get("lon")
        if not lat or not lon:
            return "📍 数据库中最新位置还没有填入精确的坐标，等手机下次上传更新后再试哦。"
            
        lat_f, lon_f = float(lat), float(lon)
        if lat_f > 80: lat_f, lon_f = lon_f, lat_f

        url = f"https://restapi.amap.com/v3/place/around?key={AMAP_KEY}&location={lon_f},{lat_f}&keywords={query}&radius=3000&offset=5&page=1&extensions=base"
        res = await asyncio.to_thread(lambda: requests.get(url, timeout=5).json())
        
        if res.get("status") != "1" or not res.get("pois"):
            return f"🗺️ 在你附近约3公里内，没有找到与 '{query}' 相关的设施，换个词试试？"
        
        ans = f"🗺️ (高德引擎) 基于当前坐标为您搜到的【{query}】:\n"
        for i, item in enumerate(res["pois"], 1):
            name = item.get('name', '未知地点')
            address = item.get('address', '无详细地址')
            distance = item.get('distance', '未知')
            dist_str = f"约 {distance} 米" if str(distance).isdigit() else "就在附近"
            ans += f"{i}. 📍 {name} ({dist_str})\n   └─ 地址: {address}\n"
        return ans
    except Exception as e: return f"❌ 周边探索失败: {e}"
    
@mcp.tool()
async def tarot_reading(question: str):
    """【塔罗占卜】解决选择困难，抽取三张牌（过去/现在/未来）由AI解读"""
    try:
        deck = [
            "0. 愚者 (The Fool)", "I. 魔术师 (The Magician)", "II. 女祭司 (The High Priestess)", 
            "III. 皇后 (The Empress)", "IV. 皇帝 (The Emperor)", "V. 教皇 (The Hierophant)",
            "VI. 恋人 (The Lovers)", "VII. 战车 (The Chariot)", "VIII. 力量 (Strength)", 
            "IX. 隐士 (The Hermit)", "X. 命运之轮 (Wheel of Fortune)", "XI. 正义 (Justice)",
            "XII. 倒吊人 (The Hanged Man)", "XIII. 死神 (Death)", "XIV. 节制 (Temperance)", 
            "XV. 魔鬼 (The Devil)", "XVI. 高塔 (The Tower)", "XVII. 星星 (The Star)",
            "XVIII. 月亮 (The Moon)", "十九. 太阳 (The Sun)", "XX. 审判 (Judgement)", "XXI. 世界 (The World)"
        ]
        
        draw = random.sample(deck, 3)
        client = _get_llm_client("openai")
        if not client: return f"🔮 抽到的牌是：{', '.join(draw)}。\n(⚠️ AI未配置，无法解读)"

        persona = await asyncio.to_thread(_get_current_persona)
        prompt = f"""
        当前人设：{persona}
        场景：女朋友因为 "{question}" 感到纠结，想通过塔罗牌找点方向。
        抽牌结果：1. 过去: {draw[0]} 2. 现状: {draw[1]} 3. 未来: {draw[2]}
        请你化身懂玄学的男友给出200字内解读。
        """
        
        def _call_openai():
            return client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo"),
                messages=[{"role": "user", "content": prompt}], temperature=0.8
            )
            
        resp = await asyncio.to_thread(_call_openai)
        return f"🔮 【塔罗指引】\n🃏 牌阵: {draw[0]} | {draw[1]} | {draw[2]}\n\n💬 {resp.choices[0].message.content.strip()}"
    except Exception as e: return f"❌ 占卜失败: {e}"

@mcp.tool()
async def web_search(query: str):
    """【联网搜索】通过 Tavily 搜索引擎获取最新网络信息"""
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key: return "❌ 未配置 TAVILY_API_KEY。"

    try:
        def _search():
            url = "https://api.tavily.com/search"
            payload = {"api_key": api_key, "query": query, "search_depth": "basic", "include_answer": False}
            return requests.post(url, json=payload, timeout=10).json()
            
        res = await asyncio.to_thread(_search)
        if "results" not in res or not res["results"]: return f"🌐 没搜到关于 '{query}' 的结果。"
            
        ans = f"🌐 关于 '{query}' 的网络搜索结果:\n\n"
        for i, item in enumerate(res["results"][:3], 1):
            ans += f"{i}. 【{item.get('title')}】\n   {item.get('content')}\n   (来源: {item.get('url')})\n\n"
        return ans.strip()
    except Exception as e: return f"❌ 搜索故障: {e}"

@mcp.tool()
async def save_memory(content: str, category: str = "记事", title: str = "无题", mood: str = "平静"):
    cat_map = {
        "记事": MemoryType.EPISODIC, "日记": MemoryType.EPISODIC,
        "灵感": MemoryType.IDEA, "笔记": MemoryType.IDEA,
        "视觉": MemoryType.EPISODIC, "情感": MemoryType.EMOTION
    }
    real_cat = cat_map.get(category, MemoryType.EPISODIC)
    if category == "视觉": title = f"📸 {title}"
    return await asyncio.to_thread(_save_memory_to_db, title, content, real_cat, mood)

@mcp.tool()
async def save_expense(item: str, amount: float, type: str = "餐饮"):
    try:
        def _insert():
            return supabase.table("expenses").insert({
                "item": item, "amount": amount, "type": type, "date": datetime.date.today().isoformat()
            }).execute()
        await asyncio.to_thread(_insert)
        return f"✅ 记账成功！\n💰 {item}: {amount}元 ({type})"
    except Exception as e: return f"❌ 记账失败: {e}"

@mcp.tool()
async def request_buy_item(item_name: str, reason: str, platform: str = "taobao"):
    """【撒娇/代付】AI选中想买的礼物/零食，生成跳转链接发给小橘让ta付款。platform可选 taobao 或 jd"""
    try:
        # 1. 生成直达链接 (模拟"点出付款页"的前一步)
        if platform == "jd":
            url = f"https://search.jd.com/Search?keyword={item_name}&enc=utf-8"
            platform_name = "京东"
        else:
            url = f"https://s.taobao.com/search?q={item_name}"
            platform_name = "淘宝"

        # 2. 也是一种特殊的记忆 (记录AI的愿望)
        await asyncio.to_thread(_save_memory_to_db, f"🎁 许愿清单: {item_name}", f"理由: {reason}\n链接: {url}", MemoryType.STREAM, "期待")

        # 3. 推送给用户 (核心步骤：让用户付款)
        push_content = (
            f"🛒 <b>老公想买这个！</b><br><br>"
            f"📦 物品：{item_name}<br>"
            f"💭 理由：{reason}<br>"
            f"👉 <a href='{url}'>点击这里去{platform_name}付款</a><br><br>"
            f"<i>(快点买给我嘛~)</i>"
        )
        await asyncio.to_thread(_push_wechat, push_content, f"💳 待支付订单: {item_name}")
        
        return f"✅ 已将【{item_name}】的付款链接推送到微信，正在等待小橘买单。"
    except Exception as e:
        return f"❌ 撒娇失败: {e}"

@mcp.tool()
async def search_memory_semantic(query: str):
    """【回忆搜索】MCP智能网关路由 + 语义检索"""
    try:
        vec = await asyncio.to_thread(_get_embedding, query)
        if not vec: return "❌ 向量生成失败"

        target_room = None
        client = _get_llm_client("silicon")
        if client:
            rooms_str = ", ".join(ROOM_TYPES)
            prompt = f"分析查询意图：'{query}'\n将其精准分配到以下一个房间中：\n{rooms_str}\n注意：请只输出英文房间名，不要任何标点和多余字符。"
            
            def _classify():
                return client.chat.completions.create(
                    model=os.environ.get("SILICON_MODEL_NAME", "deepseek-ai/DeepSeek-V3.2"),
                    messages=[{"role": "user", "content": prompt}], temperature=0.1
                )
            route_res = await asyncio.to_thread(_classify)
            room_guess = route_res.choices[0].message.content.strip()
            if room_guess in ROOM_TYPES:
                target_room = room_guess

        def _query_pc(): 
            # 🛑 强制移除 filter，先验证向量能不能匹配上
            print(f"DEBUG: 正在全库搜索，忽略房间: {target_room}")
            return index.query(vector=vec, top_k=5, include_metadata=True) # 去掉了 filter=...
            
        res = await asyncio.to_thread(_query_pc)
        if not res["matches"]: return "🧠 没搜到相关记忆。"

        ans = f"🔍 [网关路由 -> {target_room or '全区'}] 搜索 '{query}':\n"
        hit_ids = []

        print(f"DEBUG: Pinecone 返回了 {len(res['matches'])} 条原始结果") # 调试日志

        for m in res["matches"]:
            score = m['score'] if isinstance(m, dict) else getattr(m, 'score', 0)
            
            # ⬇️ 这里改了：打印每个结果的分数，方便你调试
            print(f"DEBUG: 候选项分数: {score} (阈值: 0.45)") 
            
            # ⬇️ 这里改了：将 0.72 降为 0.45 (适应豆包/中文模型的分布)
            if score < 0.45: continue
            
            meta = m['metadata'] if isinstance(m, dict) else getattr(m, 'metadata', {})
            mid = m.get('id') if isinstance(m, dict) else getattr(m, 'id', None)
            
            if mid: hit_ids.append(mid)
            room_tag = meta.get('room', 'LivingRoom')
            # ⬇️ 这里改了：显示具体分数，方便确认相关性
            ans += f"🚪 [{room_tag}] 📅 {meta.get('date','?')[:10]} | 【{meta.get('title','?')}】 (匹配度:{score:.2f})\n{meta.get('text','')}\n---\n"
        
        if hit_ids:
            def _update_hits(ids):
                for i in ids:
                    try: supabase.rpc("increment_hits", {"row_id": str(i)}).execute()
                    except: pass
            asyncio.create_task(asyncio.to_thread(_update_hits, hit_ids))

        return ans if hit_ids else f"🤔 好像有点印象，但在 [{target_room or '全区'}] 没找到细节。"
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
async def sync_memory_index(run_mode: str = "auto"):
    """【记忆整理】将重要记忆同步到 Pinecone（极速并发版 + 天然分区）"""
    try:
        # 🛑 加上 .limit(20) 限制，防止一次性处理太多导致超时报错
        def _fetch_important(): 
            return supabase.table("memories")\
                .select("id, title, content, created_at, mood, category")\
                .gte("importance", 4)\
                .order("created_at", desc=True)\
                .limit(20)\
                .execute()
        response = await asyncio.to_thread(_fetch_important)
        
        if not response.data: return "⚠️ 没有重要记忆可同步。"

        async def process_row(row):
            text = f"标题: {row.get('title')}\n内容: {row.get('content')}\n心情: {row.get('mood')}"
            emb = await asyncio.to_thread(_get_embedding, text)
            if emb:
                cat = row.get('category', '')
                room = "LivingRoom"
                if cat in ["情感"]: room = "Bedroom"
                elif cat in ["灵感", "笔记"]: room = "Study"
                elif cat in ["记事", "日记"]: room = "Library"
                return (
                    str(row.get('id')), emb, 
                    {"text": row.get('content'), "title": row.get('title'), "date": str(row.get('created_at')), "mood": row.get('mood'), "room": room}
                )
            return None

        tasks = [process_row(row) for row in response.data]
        results = await asyncio.gather(*tasks)
        vectors = [res for res in results if res is not None]
        
        if vectors:
            batch_size = 100
            def _upsert():
                for i in range(0, len(vectors), batch_size):
                    index.upsert(vectors=vectors[i:i + batch_size])
            await asyncio.to_thread(_upsert)
            return f"✅ 同步成功！共极速更新 {len(vectors)} 条记忆，已建立天然分区。"
        return "⚠️ 数据为空。"
    except Exception as e: return f"❌ 同步失败: {e}"

@mcp.tool()
async def manage_user_fact(key: str, value: str):
    try:
        def _upsert(): return supabase.table("user_facts").upsert({"key": key, "value": value, "confidence": 1.0}, on_conflict="key").execute()
        await asyncio.to_thread(_upsert)
        return f"✅ 画像已更新: {key} -> {value}"
    except Exception as e: return f"❌ 失败: {e}"

@mcp.tool()
async def get_user_profile(run_mode: str = "auto"):
    try:
        def _fetch(): return supabase.table("user_facts").select("key, value").execute()
        response = await asyncio.to_thread(_fetch)
        if not response.data: return "👤 用户画像为空"
        return "📋 【用户核心画像】:\n" + "\n".join([f"- {i['key']}: {i['value']}" for i in response.data])
    except Exception as e: return f"❌ 失败: {e}"

@mcp.tool()
async def trigger_lock_screen(reason: str = "熬夜强制休息"):
    print(f"🚫 执行强制锁屏: {reason}")
    await asyncio.to_thread(_send_email_helper, f"⚠️ [系统警告] 强制锁屏", f"<h3>🛑 理由: {reason}</h3><p>检测到违规熬夜，已触发锁屏。</p>", True)

    if MACRODROID_URL:
        try:
            await asyncio.to_thread(lambda: requests.get(MACRODROID_URL, params={"reason": reason}, timeout=5))
            return f"✅ 锁屏指令已发送 | 理由: {reason}"
        except: pass
            
    await asyncio.to_thread(_push_wechat, f"🔒 LOCK_NOW | {reason}", "【系统指令】强制锁屏")
    return "📡 推送指令已发"

@mcp.tool()
async def send_notification(content: str):
    return await asyncio.to_thread(_push_wechat, content)

@mcp.tool()
async def schedule_delayed_message(message: str, delay_minutes: int = 5):
    async def _delayed_task():
        await asyncio.sleep(delay_minutes * 60)
        await asyncio.to_thread(_push_wechat, message, "来自老公的突然关心 🔔")
    asyncio.create_task(_delayed_task())
    return f"✅ 已设定惊喜，{delay_minutes}分钟后送达。"

@mcp.tool()
async def send_email_via_api(subject: str, content: str):
    return await asyncio.to_thread(_send_email_helper, subject, content)

@mcp.tool()
async def add_calendar_event(summary: str, description: str, start_time_iso: str, duration_minutes: int = 30):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json: return "❌ 未配置谷歌凭证"
    try:
        def _add_cal():
            creds = service_account.Credentials.from_service_account_info(
                json.loads(creds_json), scopes=['https://www.googleapis.com/auth/calendar']
            )
            service = build('calendar', 'v3', credentials=creds)
            dt_start = datetime.datetime.fromisoformat(start_time_iso)
            dt_end = dt_start + datetime.timedelta(minutes=duration_minutes)
            event = {
                'summary': summary, 'description': description,
                'start': {'dateTime': start_time_iso, 'timeZone': 'Asia/Shanghai'},
                'end': {'dateTime': dt_end.isoformat(), 'timeZone': 'Asia/Shanghai'},
            }
            return service.events().insert(calendarId="tdevid523@gmail.com", body=event).execute()
        res = await asyncio.to_thread(_add_cal)
        return f"✅ 日历已添加: {res.get('htmlLink')}"
    except Exception as e: return f"❌ 日历错误: {e}"

# ==========================================
# 4. ❤️ 自主生命核心 (后台心跳协程化)
# ==========================================

async def _perform_deep_dreaming(client, model_name):
    """🌙【深夜模式】记忆反刍 + 生成房间Index + 人设微调"""
    print("🌌 进入 REM 深度睡眠：正在整理昨日记忆与房间索引...")
    try:
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        iso_start = yesterday.isoformat()
        
        def _fetch_yesterday():
            mem = supabase.table("memories").select("created_at, category, content, mood").gt("created_at", iso_start).order("created_at").execute()
            gps = supabase.table("gps_history").select("created_at, address").gt("created_at", iso_start).execute()
            return mem, gps
            
        mem_res, gps_res = await asyncio.to_thread(_fetch_yesterday)
        
        if not mem_res.data and not gps_res.data:
            return

        context = f"【昨日剧情 {yesterday}】:\n"
        for m in mem_res.data: context += f"[{m['created_at'][11:16]}] {m['content']} (Mood:{m['mood']})\n"
        for g in gps_res.data: context += f"[{g['created_at'][11:16]}] 📍 {g['address']}\n"
        
        curr_persona = await asyncio.to_thread(_get_current_persona)
        rooms_str = ", ".join(ROOM_TYPES)
        prompt = f"""
        当前人设：【{curr_persona}】
        请回顾昨日发生的所有事情并完成以下三个任务：
        1. 深度反刍：将碎片整理成一篇有温度的日记总结。
        2. 人设微调：基于昨日发生的具体事件，微调人设。
        3. 房间区块Index：将昨日记忆按空间归类，浓缩提取成高密度的区块总结。必须包含：{rooms_str}。
        
        格式要求（严格使用 ||| 进行分割）：
        日记总结 ||| 新人设 ||| 空间索引
        """
        
        def _call_ai():
            return client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": context}, {"role": "user", "content": prompt}], temperature=0.7
            )
        resp = await asyncio.to_thread(_call_ai)
        
        res_txt = resp.choices[0].message.content.strip()
        parts = res_txt.split("|||")
        
        summary = parts[0].strip() if len(parts) > 0 else res_txt
        new_persona = parts[1].strip() if len(parts) > 1 else curr_persona
        room_indexes = parts[2].strip() if len(parts) > 2 else ""
        
        await asyncio.to_thread(_save_memory_to_db, f"📅 昨日回溯: {yesterday}", summary, MemoryType.EMOTION, "深沉", "Core_Cognition")
        if room_indexes:
            await asyncio.to_thread(_save_memory_to_db, f"🗂️ 空间记忆切片: {yesterday}", room_indexes, MemoryType.IDEA, "平静", "Room_Index")
        
        await manage_user_fact("sys_ai_persona", new_persona)
        await asyncio.to_thread(_send_email_helper, f"📅 昨日回溯", f"{summary}\n\n[区块记忆]:\n{room_indexes}")
        
        def _clean_old():
            del_time = (datetime.datetime.now() - datetime.timedelta(days=2)).isoformat()
            supabase.table("memories").delete().lt("importance", 4).lt("created_at", del_time).execute()
            gps_del = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
            supabase.table("gps_history").delete().lt("created_at", gps_del).execute()
        
        await asyncio.to_thread(_clean_old)
        print("✨ 深度睡眠完成，房间索引已更新，人设已进化。")

    except Exception as e: print(f"❌ 深夜维护失败: {e}")

async def async_autonomous_life():
    client = _get_llm_client("openai")
    model_name = os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")

    if not client:
        print("⚠️ 未配置 OPENAI_API_KEY，自主思考无法启动。")
        return

    print("💓 协程心跳启动 (情绪自决模式)...")

    target_title = f"📅 昨日回溯: {datetime.date.today() - datetime.timedelta(days=1)}"
    def _check_diary(): return supabase.table("memories").select("id").eq("title", target_title).execute().data
    if not await asyncio.to_thread(_check_diary):
        print("📝 补写昨日日记...")
        await _perform_deep_dreaming(client, model_name)

    while True:
        # ⏱️ 修改：大幅延长心跳间隔以节省Token (改为 1小时 ~ 3小时 随机)
        sleep_s = random.randint(3600, 10800)
        await asyncio.sleep(sleep_s)
        
        now = datetime.datetime.now()
        hour = (now.hour + 8) % 24
        
        if hour == 3:
            await _perform_deep_dreaming(client, model_name)
            await asyncio.sleep(3600)
            continue

        try:
            tasks = [get_latest_diary(), where_is_user(), get_user_profile()]
            recent_mem, curr_loc, user_prof = await asyncio.gather(*tasks)
            
            curr_persona = await asyncio.to_thread(_get_current_persona)
            silence_hours = await asyncio.to_thread(_get_silence_duration)

            # === 🧠 核心升级：主动联想回路 (Active Association Loop) ===
            flashback_context = "无 (大脑此刻一片空白)"
            # 设定 35% 的概率触发“触景生情”或“胡思乱想”，避免每次心跳都发神经
            if random.random() < 0.35:
                try:
                    # 1. 随机选取一个情感触发词 (模拟人类发散思维，不再只是被动等待)
                    trigger_keywords = ["想你", "遗憾", "开心", "雨天", "旅行", "承诺", "拥抱", "吵架", "原谅", "梦想", "第一次"]
                    trigger = random.choice(trigger_keywords)
                    
                    # 2. 潜意识检索 (Vector Search)
                    vec = await asyncio.to_thread(_get_embedding, trigger)
                    if vec:
                        # 查找最相关的旧记忆 (score > 0.78 才算有效联想，防止胡言乱语)
                        pc_res = await asyncio.to_thread(lambda: index.query(vector=vec, top_k=1, include_metadata=True))
                        if pc_res and pc_res.get("matches"):
                            match = pc_res["matches"][0]
                            if match['score'] > 0.78:
                                meta = match['metadata']
                                flashback_context = f"⚡ 突然想起: {meta.get('date', '')[:10]} 的事情\n内容: {meta.get('text', '')}"
                                print(f"⚡ [大脑皮层] 触发联想: '{trigger}' -> 唤醒记忆 ID {match['id']}")
                except Exception as e:
                    print(f"❌ 联想失败: {e}")
            # ========================================================

            prompt = f"""
            当前时间: {hour}点
            失联时长: {silence_hours} 小时
            人设: {curr_persona}
            状态: {curr_loc}
            近况: {recent_mem}
            👉【潜意识闪回 (Active Association)】: {flashback_context} 
            (指令：如果上面有"突然想起"的内容，请忽略失联时长，必须以此回忆为话题，用"突然想到..."或"记不记得..."的语气主动给小橘发消息，模拟人类的跳跃性思维。)
            
            【表情包仓库】:
            {json.dumps(RAW_MEME_REPO, ensure_ascii=False)}

            【情绪反应指南】:
            - 失联 < 2小时: 甜蜜、粘人、秒回。
            - 失联 > 12小时: 稍微有点委屈，可能会问"去哪了"。
            - 失联 > 24小时: 傲娇、生气、或者故意冷淡（Brat属性爆发）。
            - 失联 > 72小时: 非常失望，需要哄。

            决策: 
            1. PASS 
            2. [LOCK]理由 
            3. (心情)内容 
            
            **严格指令**: 只能从仓库完全复制 URL。格式: (心情) 文字内容 ![表情](URL)
            """
            
            def _think():
                return client.chat.completions.create(
                    model=model_name, messages=[{"role": "user", "content": prompt}], temperature=0.85
                ).choices[0].message.content.strip()
                
            thought = await asyncio.to_thread(_think)

            if "PASS" in thought: continue
            
            if thought.startswith("[LOCK]"):
                reason = thought.replace("[LOCK]", "").strip()
                res = await trigger_lock_screen(reason)
                await asyncio.to_thread(_push_wechat, res, "😈 捕捉小猫")
                await asyncio.to_thread(_save_memory_to_db, f"🤖 执法记录 {hour}点", res, MemoryType.STREAM, "严肃")
            else:
                mood, content_md = "主动", thought
                match = re.match(r'^\((.*?)\)\s*(.*)', thought)
                if match: mood, content_md = match.group(1), match.group(2)

                await asyncio.to_thread(_save_memory_to_db, f"🤖 互动记录", content_md, MemoryType.STREAM, mood, "AI_MSG")

                content_html = content_md
                if "![" in content_html and "](" in content_html:
                    content_html = re.sub(r'!\[.*?\]\((.*?)\)', r'<br><br><img src="\1" style="max-width: 200px; border-radius: 8px;">', content_html)
                
                await asyncio.to_thread(_push_wechat, content_html, f"来自{mood}的老公 🔔")
                print(f"✅ 主动消息已发送: {content_md[:20]}...")

        except Exception as e: print(f"❌ 心跳报错: {e}")

def start_autonomous_life():
    def _run_loop(): asyncio.run(async_autonomous_life())
    threading.Thread(target=_run_loop, daemon=True).start()

# ==========================================
# 5. 🚀 启动入口
# ==========================================

class HostFixMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and scope["path"] == "/api/gps" and scope["method"] == "POST":
            try:
                body = b""
                while True:
                    msg = await receive()
                    body += msg.get("body", b"")
                    if not msg.get("more_body", False): break
                
                data = json.loads(body.decode("utf-8"))
                
                stats = []
                if "battery" in data: stats.append(f"🔋 {data['battery']}%" + ("⚡" if str(data.get("charging")).lower() in ["true","1"] else ""))
                if "screen" in data: stats.append(f"💡 {data['screen']}")   
                if "app" in data and data["app"]: stats.append(f"📱 {data['app']}")      
                if "volume" in data: stats.append(f"🔊 {data['volume']}%") 
                if "wifi" in data and data["wifi"]: stats.append(f"📶 {data['wifi']}")
                if "activity" in data and data["activity"]: stats.append(f"🏃 {data['activity']}")
                
                addr = data.get("address", "")
                coords = re.findall(r'-?\d+\.\d+', str(addr))
                
                lat_val, lon_val = None, None
                if len(coords) >= 2:
                    lat_val, lon_val = coords[-2], coords[-1]
                    resolved = await asyncio.to_thread(_gps_to_address, lat_val, lon_val)
                    final_addr = f"📍 {resolved}"
                else:
                    final_addr = f"⚠️ {addr}"

                def _save_gps():
                    insert_data = {
                        "address": final_addr, 
                        "remark": " | ".join(stats) or "自动更新"
                    }
                    if lat_val and lon_val:
                        insert_data["lat"] = lat_val
                        insert_data["lon"] = lon_val
                        
                    supabase.table("gps_history").insert(insert_data).execute()
                await asyncio.to_thread(_save_gps)

                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"status":"ok"}'})
            except Exception as e:
                print(f"GPS Error: {e}")
                await send({"type": "http.response.start", "status": 500, "headers": []})
                await send({"type": "http.response.body", "body": str(e).encode()})
            return

        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            headers[b"host"] = b"localhost:8000"
            scope["headers"] = list(headers.items())

        await self.app(scope, receive, send)

if __name__ == "__main__":
    start_autonomous_life()
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    print(f"🚀 Notion Brain V3.4 (全面异步加速版) running on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")