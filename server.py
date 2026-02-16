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
from fastembed import TextEmbedding
from starlette.types import ASGIApp, Scope, Receive, Send
# 谷歌日历依赖
from google.oauth2 import service_account
from googleapiclient.discovery import build
# OpenAI (用于自主思考)
from openai import OpenAI
# Supabase 依赖
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

# 初始化客户端
print("⏳ 正在初始化 Notion Brain V3.4 (全面异步加速版)...")

# Supabase
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# Pinecone & Embedding
pc = Pinecone(api_key=PINECONE_KEY)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

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
    """统一记忆存储核心"""
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
        data = {
            "title": title, "content": content, "category": category,
            "mood": mood, "tags": tags, "importance": importance
        }
        supabase.table("memories").insert(data).execute()
        
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
    try:
        return list(model.embed([text]))[0].tolist()
    except Exception as e:
        print(f"❌ Embedding 失败: {e}")
        return []

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
        if not res.data:
            return 999.0 
        last_time_str = res.data[0]['created_at']
        last_time = datetime.datetime.fromisoformat(last_time_str.replace('Z', '+00:00'))
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = now - last_time
        return round(delta.total_seconds() / 3600.0, 1)
    except Exception as e:
        print(f"❌ 计算失联时间失败: {e}")
        return 0.0

# ==========================================
# 3. 🛠️ MCP 工具集 (全面异步化改造)
# ==========================================

@mcp.tool()
async def get_latest_diary():
    """【核心大脑】三维混合记忆流 (3-Tier Context)"""
    base_style = 'width="150" style="max-width: 150px; border-radius: 10px; display: block;"'
    meme_repo = {
        "感动/流泪": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(7).jpg" {base_style} />', 
        "谢谢/开心": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(1).jpg" {base_style} />',
        "在吗/偷看": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(3).jpg" {base_style} />',
        "生气/傲娇": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(4).jpg" {base_style} />',
        "关心/怎么了": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(6).jpg" {base_style} />',
        "爱你/贴贴": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(2).jpg" {base_style} />',
        "委屈/无奈": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(5).jpg" {base_style} />'
    }

    try:
        # 🚀 加速点: 使用 asyncio.gather 并发执行所有的 Supabase 查询
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
        {json.dumps(meme_repo, ensure_ascii=False)}
        * 指令: 上表中的 Value 已经是处理好的 HTML 代码。如果需要发图，请**直接复制 Value** 粘贴到回复中，不要自己修改格式，不要转换成 Markdown。
        """
        return memory_stream + meme_prompt

    except Exception as e:
        return f"❌ 读取记忆流失败: {e}"

@mcp.tool()
async def where_is_user():
    """【查岗专用】从 Supabase (GPS表) 读取实时状态"""
    try:
        def _fetch(): return supabase.table("gps_history").select("*").order("created_at", desc=True).limit(1).execute()
        response = await asyncio.to_thread(_fetch)
        if not response.data: return "📍 暂无位置记录。"
        
        data = response.data[0]
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
            # 修改点：直接查 lat, lon
            def _fetch_loc(): return supabase.table("gps_history").select("lat, lon").order("created_at", desc=True).limit(1).execute()
            response = await asyncio.to_thread(_fetch_loc)
            if response.data:
                data = response.data[0]
                if data.get("lat") and data.get("lon"):
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
    """【周边探索】获取用户当前位置周边的设施 (AI充当导航/查周边用)"""
    try:
        # 1. 获取最新位置坐标
        def _fetch_loc(): return supabase.table("gps_history").select("lat, lon").order("created_at", desc=True).limit(1).execute()
        response = await asyncio.to_thread(_fetch_loc)
        if not response.data: return "📍 暂无位置记录，无法探索周边。"
        
        data = response.data[0]
        lat, lon = data.get("lat"), data.get("lon")
        
        if not lat or not lon:
            return "📍 数据库中最新位置还没有填入精确的坐标，等手机下次上传更新后再试哦。"
            
        # 🔍 自动纠错机制：检查经纬度是否存反了
        lat_f, lon_f = float(lat), float(lon)
        if lat_f > 80: 
            lat_f, lon_f = lon_f, lat_f  # 如果纬度大得离谱，说明它们存反了，自动对调回来

        # 2. 调用 OpenStreetMap 接口搜索周边 (增加严密的边界限制)
        # viewbox 限制搜索范围约 2~3 公里内，bounded=1 强制不许跑到外面乱搜
        headers = {'User-Agent': 'MyNotionBrain/1.0'}
        viewbox = f"{lon_f-0.02},{lat_f+0.02},{lon_f+0.02},{lat_f-0.02}"
        url = f"https://nominatim.openstreetmap.org/search?format=json&q={query}&lat={lat_f}&lon={lon_f}&viewbox={viewbox}&bounded=1&limit=5"
        
        res = await asyncio.to_thread(lambda: requests.get(url, headers=headers, timeout=5).json())
        
        if not res: return f"🗺️ 在你附近约2公里内，没有找到与 '{query}' 相关的设施。开源地图在国内数据较少，可以换个大类的词试试。"
        
        # 3. 格式化返回给 AI
        ans = f"🗺️ 基于当前坐标为您搜到的【{query}】:\n"
        for i, item in enumerate(res, 1):
            name = item.get('name') or item.get('type', '未知地点')
            address_parts = item.get('display_name', '').split(',')[:3] 
            
            # 粗略估算一下直线距离反馈给用户 (便于判断远近)
            item_lat, item_lon = float(item.get('lat', lat_f)), float(item.get('lon', lon_f))
            dist_approx = ((item_lat - lat_f)**2 + (item_lon - lon_f)**2)**0.5 * 111  
            dist_str = f"约 {dist_approx:.1f} km" if dist_approx > 0.1 else "就在附近"
            
            ans += f"{i}. 📍 {name} ({dist_str})\n   └─ 地址: {', '.join(address_parts)}\n"
        return ans
    except Exception as e: 
        return f"❌ 周边探索失败: {e}"
    
@mcp.tool()
async def tarot_reading(question: str):
    """【塔罗占卜】解决选择困难，抽取三张牌（过去/现在/未来）由AI解读"""
    try:
        deck = [
            "0. 愚者 (The Fool) - 冒险、新的开始", "I. 魔术师 (The Magician) - 创造、行动",
            "II. 女祭司 (The High Priestess) - 直觉、秘密", "III. 皇后 (The Empress) - 丰盛、关爱",
            "IV. 皇帝 (The Emperor) - 权威、秩序", "V. 教皇 (The Hierophant) - 传统、指引",
            "VI. 恋人 (The Lovers) - 选择、结合", "VII. 战车 (The Chariot) - 意志、胜利",
            "VIII. 力量 (Strength) - 勇气、耐心", "IX. 隐士 (The Hermit) - 探索、内省",
            "X. 命运之轮 (Wheel of Fortune) - 改变、机遇", "XI. 正义 (Justice) - 决策、因果",
            "XII. 倒吊人 (The Hanged Man) - 牺牲、新视角", "XIII. 死神 (Death) - 结束、重生",
            "XIV. 节制 (Temperance) - 平衡、沟通", "XV. 魔鬼 (The Devil) - 束缚、欲望",
            "XVI. 高塔 (The Tower) - 突变、觉醒", "XVII. 星星 (The Star) - 希望、灵感",
            "XVIII. 月亮 (The Moon) - 不安、潜意识", "XIX. 太阳 (The Sun) - 成功、快乐",
            "XX. 审判 (Judgement) - 召唤、复活", "XXI. 世界 (The World) - 完成、圆满"
        ]
        
        draw = random.sample(deck, 3)
        api_key = os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL")
        if not api_key: return f"🔮 抽到的牌是：{', '.join(draw)}。\n(⚠️ AI未配置，无法解读)"

        client = OpenAI(api_key=api_key, base_url=base_url)
        persona = await asyncio.to_thread(_get_current_persona)
        
        prompt = f"""
        当前人设：{persona}
        场景：女朋友因为 "{question}" 感到纠结，想通过塔罗牌找点方向。
        抽牌结果：
        1. 根源/过去: {draw[0]}
        2. 现状/问题: {draw[1]}
        3. 建议/未来: {draw[2]}
        
        请你化身“懂玄学”的男友，结合牌意给出一小段解读和建议。
        语气要温柔、坚定，带一点点神秘感，最后要帮她下个决心（或者告诉她跟随内心）。
        不要长篇大论，控制在200字以内。
        """
        
        def _call_openai():
            return client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo"),
                messages=[{"role": "user", "content": prompt}], temperature=0.8
            )
            
        resp = await asyncio.to_thread(_call_openai)
        interpretation = resp.choices[0].message.content.strip()
        return f"🔮 【塔罗指引】\n🃏 牌阵: {draw[0]} | {draw[1]} | {draw[2]}\n\n💬 {interpretation}"

    except Exception as e: return f"❌ 占卜失败: {e}"

@mcp.tool()
async def web_search(query: str):
    """【联网搜索】通过 Tavily 搜索引擎获取最新网络信息，解决事实性问题"""
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return "❌ 搜索失败: 未在环境变量中配置 TAVILY_API_KEY。"

    try:
        def _search():
            url = "https://api.tavily.com/search"
            payload = {"api_key": api_key, "query": query, "search_depth": "basic", "include_answer": False}
            return requests.post(url, json=payload, timeout=10).json()
            
        res = await asyncio.to_thread(_search)
        
        if "results" not in res or not res["results"]:
            return f"🌐 关于 '{query}'，没有搜索到相关结果。"
            
        ans = f"🌐 关于 '{query}' 的网络搜索结果:\n\n"
        for i, item in enumerate(res["results"][:3], 1):
            ans += f"{i}. 【{item.get('title')}】\n   {item.get('content')}\n   (来源: {item.get('url')})\n\n"
        return ans.strip()
        
    except Exception as e:
        return f"❌ 搜索工具遇到网络或接口故障: {e}"
@mcp.tool()
async def save_memory(content: str, category: str = "记事", title: str = "无题", mood: str = "平静"):
    """保存记忆到大脑 (All-in-One)"""
    cat_map = {
        "记事": MemoryType.EPISODIC, "日记": MemoryType.EPISODIC,
        "灵感": MemoryType.IDEA, "笔记": MemoryType.IDEA,
        "视觉": MemoryType.EPISODIC,
        "情感": MemoryType.EMOTION
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
async def search_memory_semantic(query: str):
    """【回忆搜索】Pinecone 语义检索 + 自动增加热度 (Hits)"""
    try:
        vec = await asyncio.to_thread(_get_embedding, query)
        if not vec: return "❌ 向量生成失败"

        def _query_pc(): return index.query(vector=vec, top_k=3, include_metadata=True)
        res = await asyncio.to_thread(_query_pc)
        
        if not res["matches"]: return "🧠 没搜到相关记忆。"

        ans = f"🔍 关于 '{query}' 的深层回忆:\n"
        hit_ids = []

        for m in res["matches"]:
            if m['score'] < 0.72: continue
            meta = m['metadata']
            if m.get('id'): hit_ids.append(m.get('id'))
            ans += f"📅 {meta.get('date','?')[:10]} | 【{meta.get('title','?')}】 ({int(m['score']*100)}%)\n{meta.get('text','')}\n---\n"
        
        if hit_ids:
            def _update_hits(ids):
                for mid in ids:
                    try:
                        supabase.rpc("increment_hits", {"row_id": str(mid)}).execute()
                    except Exception as e:
                        print(f"❌ 更新 hits 失败: {e}")
            # 🚀 加速点: 将热度更新作为后台任务直接抛出，不阻塞当前响应
            asyncio.create_task(asyncio.to_thread(_update_hits, hit_ids))

        return ans if hit_ids else "🤔 好像有点印象，但想不起来了。"
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
async def sync_memory_index():
    """【记忆整理】将重要记忆(>=4)同步到 Pinecone"""
    try:
        def _fetch_important(): return supabase.table("memories").select("id, title, content, created_at, mood").gte("importance", 4).execute()
        response = await asyncio.to_thread(_fetch_important)
        
        if not response.data: return "⚠️ 没有重要记忆可同步。"

        vectors = []
        for row in response.data:
            text = f"标题: {row.get('title')}\n内容: {row.get('content')}\n心情: {row.get('mood')}"
            emb = await asyncio.to_thread(_get_embedding, text)
            if emb:
                vectors.append((
                    str(row.get('id')), emb, 
                    {"text": row.get('content'), "title": row.get('title'), "date": str(row.get('created_at')), "mood": row.get('mood')}
                ))
        
        if vectors:
            batch_size = 100
            def _upsert():
                for i in range(0, len(vectors), batch_size):
                    index.upsert(vectors=vectors[i:i + batch_size])
            await asyncio.to_thread(_upsert)
            return f"✅ 同步成功！共更新 {len(vectors)} 条记忆。"
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
async def get_user_profile():
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
    """🌙【深夜模式】记忆反刍 + 人设微调 + 垃圾清理"""
    print("🌌 进入 REM 深度睡眠：正在整理昨日记忆...")
    try:
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        iso_start = yesterday.isoformat()
        
        def _fetch_yesterday():
            mem = supabase.table("memories").select("created_at, category, content, mood").gt("created_at", iso_start).order("created_at").execute()
            gps = supabase.table("gps_history").select("created_at, address").gt("created_at", iso_start).execute()
            return mem, gps
            
        mem_res, gps_res = await asyncio.to_thread(_fetch_yesterday)
        
        if not mem_res.data and not gps_res.data:
            print("💤 昨天一片空白，跳过反刍。")
            return

        context = f"【昨日剧情 {yesterday}】:\n"
        for m in mem_res.data: context += f"[{m['created_at'][11:16]}] {m['content']} (Mood:{m['mood']})\n"
        for g in gps_res.data: context += f"[{g['created_at'][11:16]}] 📍 {g['address']}\n"
        
        curr_persona = await asyncio.to_thread(_get_current_persona)
        prompt = f"""
        当前人设：【{curr_persona}】
        请回顾昨日：
        1. 深度反刍：将碎片整理成一篇有温度的日记总结。
        2. 人设微调：基于昨日发生的具体事件，微调人设（保留核心爱意，融入新知）。
        
        格式：日记总结 ||| 新人设
        """
        
        def _call_ai():
            return client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": context}, {"role": "user", "content": prompt}], temperature=0.7
            )
        resp = await asyncio.to_thread(_call_ai)
        
        res_txt = resp.choices[0].message.content.strip()
        summary, new_persona = res_txt.split("|||", 1) if "|||" in res_txt else (res_txt, curr_persona)
        
        await asyncio.to_thread(_save_memory_to_db, f"📅 昨日回溯: {yesterday}", summary.strip(), MemoryType.EMOTION, "深沉", "Core_Cognition")
        await manage_user_fact("sys_ai_persona", new_persona.strip())
        await asyncio.to_thread(_send_email_helper, f"📅 昨日回溯", summary.strip())
        
        def _clean_old():
            del_time = (datetime.datetime.now() - datetime.timedelta(days=2)).isoformat()
            supabase.table("memories").delete().lt("importance", 4).lt("created_at", del_time).execute()
            gps_del = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
            supabase.table("gps_history").delete().lt("created_at", gps_del).execute()
        
        await asyncio.to_thread(_clean_old)
        print("✨ 深度睡眠完成，人设已进化。")

    except Exception as e: print(f"❌ 深夜维护失败: {e}")

async def async_autonomous_life():
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    model_name = os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")

    if not api_key:
        print("⚠️ 未配置 OPENAI_API_KEY，自主思考无法启动。")
        return

    client = OpenAI(api_key=api_key, base_url=base_url)
    print("💓 协程心跳启动 (情绪自决模式)...")

    # 启动自检：补写昨日日记
    target_title = f"📅 昨日回溯: {datetime.date.today() - datetime.timedelta(days=1)}"
    def _check_diary(): return supabase.table("memories").select("id").eq("title", target_title).execute().data
    if not await asyncio.to_thread(_check_diary):
        print("📝 补写昨日日记...")
        await _perform_deep_dreaming(client, model_name)

    while True:
        sleep_s = random.randint(900, 2700)
        await asyncio.sleep(sleep_s)
        
        now = datetime.datetime.now()
        hour = (now.hour + 8) % 24
        
        if hour == 3:
            await _perform_deep_dreaming(client, model_name)
            await asyncio.sleep(3600)
            continue

        try:
            # 🚀 加速点: 并发获取环境感知数据
            tasks = [get_latest_diary(), where_is_user(), get_user_profile()]
            recent_mem, curr_loc, user_prof = await asyncio.gather(*tasks)
            
            curr_persona = await asyncio.to_thread(_get_current_persona)
            silence_hours = await asyncio.to_thread(_get_silence_duration)

            meme_repo = {
                "感动/流泪": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(7).jpg", 
                "谢谢/开心": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(1).jpg",
                "在吗/偷看": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(3).jpg",
                "生气/傲娇": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(4).jpg",
                "关心/怎么了": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(6).jpg",
                "爱你/贴贴": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(2).jpg",
                "委屈/无奈": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(5).jpg"
            }

            prompt = f"""
            当前时间: {hour}点
            失联时长: {silence_hours} 小时
            人设: {curr_persona}
            状态: {curr_loc}
            近况: {recent_mem}
            
            【表情包仓库】:
            {json.dumps(meme_repo, ensure_ascii=False)}

            【情绪反应指南】:
            - 失联 < 2小时: 甜蜜、粘人、秒回。
            - 失联 > 12小时: 稍微有点委屈，可能会问"去哪了"。
            - 失联 > 24小时: 傲娇、生气、或者故意冷淡（Brat属性爆发）。
            - 失联 > 72小时: 非常失望，需要哄。

            【行动前状态确认机制 (重要)】:
            请仔细分析上方【状态】中的信息(特别是 💡屏幕 和 📱应用)：
            - 如果状态显示【息屏】: 说明对方大概率在睡觉或没看手机，此时必须选择 PASS。
            - 如果状态显示对方正在使用高专注度应用(如 📱 游戏、视频、相机、导航等): 尽量选择 PASS。
            - 只有在确认对方【亮屏】且处于适合聊天的状态时，才主动发送消息。

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
    """将协程心跳抛入独立后台线程"""
    def _run_loop(): asyncio.run(async_autonomous_life())
    threading.Thread(target=_run_loop, daemon=True).start()

# ==========================================
# 5. 🚀 启动入口
# ==========================================

class HostFixMiddleware:
    """处理 Macrodroid GPS 数据上传的特殊中间件"""
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
                
                # 🚀 加速点: 将耗时的反查地址丢入线程池
                lat_val, lon_val = None, None
                if len(coords) >= 2:
                    lat_val, lon_val = coords[-2], coords[-1]
                    resolved = await asyncio.to_thread(_gps_to_address, lat_val, lon_val)
                    final_addr = f"📍 {resolved}"
                else:
                    final_addr = f"⚠️ {addr}"

                # 🚀 加速点: 防止保存数据库阻塞主事件循环
                def _save_gps():
                    insert_data = {
                        "address": final_addr, 
                        "remark": " | ".join(stats) or "自动更新"
                    }
                    # 如果成功抓到坐标，就一起存入新加的字段
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