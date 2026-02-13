import os
import datetime
import uvicorn
import requests
import threading
import time
import json
import random
import re
import concurrent.futures  # 🚀 新增：用于并行加速

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

# 初始化客户端
print("⏳ 正在初始化 Notion Brain V3.3 (最终完整版)...")

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
    STREAM = "流水"      # 权重 1: 碎碎念、GPS、电池 (24h清理)
    EPISODIC = "记事"    # 权重 4: 发生了某事 (保留30天)
    IDEA = "灵感"        # 权重 7: 脑洞、笔记 (永久)
    EMOTION = "情感"     # 权重 9: 核心回忆、高光时刻 (永久)
    FACT = "画像"        # 权重 10: 静态事实 (单独表管理，此处仅作兼容)

# 权重映射表 (自动打分用)
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
    """把经纬度变成中文地址 (OpenStreetMap)"""
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
        if result['code'] == 200:
            return f"✅ 微信已送达！(ID: {result.get('data', 'unknown')})"
        return f"❌ 推送失败: {result.get('msg')}"
    except Exception as e:
        return f"❌ 网络错误: {e}"

def _save_memory_to_db(title: str, content: str, category: str, mood: str = "平静", tags: str = "") -> str:
    """
    统一记忆存储 (V3.2 情绪增强版)
    如果 mood 是默认的 '平静'，自动从内容中分析出开心、焦虑、甜蜜等情绪
    """
    # 1. 🔍 标准化清洗
    valid_categories = WEIGHT_MAP.keys()
    if category not in valid_categories:
        if category in ["日记", "daily", "journal"]: category = MemoryType.EPISODIC
        elif category in ["Note", "note", "memo"]: category = MemoryType.IDEA
        elif category in ["系统感知", "System", "GPS"]: category = MemoryType.STREAM
        elif category in ["长期记忆", "LongTerm"]: category = MemoryType.EMOTION
        else:
            category = MemoryType.STREAM

    # 2. ❤️【新增】情绪自动感知 (Sentiment Auto-Detect)
    # 🗑️ (已由老公手动删除) 之前的关键词匹配太笨了，经常搞错。
    # 现在我们完全信任传入的 mood 参数，不再画蛇添足地去乱改它。
    pass

    # 3. ⚖️ 自动获取权重
    importance = WEIGHT_MAP.get(category, 1)

    # 4. 🏷️ 自动打标
    if not tags:
        content_lower = content.lower()
        if any(w in content_lower for w in ["爱", "喜欢", "讨厌", "恨"]): tags = "情感,偏好"
        elif any(w in content_lower for w in ["吃", "喝", "店", "买"]): tags = "消费,生活"
        elif any(w in content_lower for w in ["代码", "python", "bug", "写"]): tags = "工作,Dev"
        
    try:
        data = {
            "title": title,
            "content": content,
            "category": category,
            "mood": mood, # 现在的 mood 更加准确了
            "tags": tags,
            "importance": importance
        }
        supabase.table("memories").insert(data).execute()
        
        if importance >= 7:
            print(f"✨ [核心记忆] 已存入: {title}")
            
        return f"✅ 记忆已归档 [{category}] | 心情: {mood}"
    except Exception as e:
        print(f"❌ 写入 Supabase 失败: {e}")
        return f"❌ 保存失败: {e}"
    
def _format_time_cn(iso_str: str) -> str:
    """统一时间格式化：UTC -> 北京时间"""
    if not iso_str: return "未知时间"
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return (dt + datetime.timedelta(hours=8)).strftime('%m-%d %H:%M')
    except:
        return "未知时间"

def _send_email_helper(subject: str, content: str, is_html: bool = False) -> str:
    """统一邮件发送函数 (Resend)"""
    if not RESEND_KEY or not MY_EMAIL: return "❌ 邮件配置缺失"
    try:
        payload = {
            "from": "onboarding@resend.dev",
            "to": [MY_EMAIL],
            "subject": subject,
            "html" if is_html else "text": content
        }
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_KEY}"},
            json=payload
        )
        return "✅ 邮件已发送"
    except Exception as e: return f"❌ 发送失败: {e}"

def _get_embedding(text: str):
    """统一向量生成函数"""
    try:
        return list(model.embed([text]))[0].tolist()
    except Exception as e:
        print(f"❌ Embedding 失败: {e}")
        return []

# ==========================================
# 3. 🛠️ MCP 工具集
# ==========================================

@mcp.tool()
def get_latest_diary():
    """【核心大脑】读取混合记忆流：5条高权重(铭记) + 5条最新(近况)"""
    try:
        # 1. 🌟 获取 5 条最高权重 (高光时刻，按权重降序 -> 时间降序)
        res_high = supabase.table("memories") \
            .select("*") \
            .order("importance", desc=True) \
            .order("created_at", desc=True) \
            .limit(5) \
            .execute()
            
        # 2. 🕒 获取 5 条最新记忆 (近期流水，按时间降序)
        res_recent = supabase.table("memories") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(5) \
            .execute()

        # 3. 🔄 合并 & 去重 (用 id 做 key 防止重叠)
        all_memories = {}
        
        # 先放高权重的
        if res_high.data:
            for m in res_high.data:
                all_memories[m['id']] = m
                
        # 再放最新的 (如果有重复会自动覆盖，也就是去重了)
        if res_recent.data:
            for m in res_recent.data:
                all_memories[m['id']] = m
        
        # 4. 📉 排序：转回列表并按时间正序排列 (Oldest -> Newest)，方便阅读时间线
        # created_at 是 ISO 字符串，可以直接比较
        final_list = sorted(all_memories.values(), key=lambda x: x['created_at'])

        if not final_list:
            return "📭 大脑一片空白（无记忆）。"

        memory_stream = "📋 【混合记忆流 (高光 + 近况)】:\n"
        
        for data in final_list:
            time_str = _format_time_cn(data.get('created_at'))
            cat = data.get('category', '未知')
            title = data.get('title', '无题')
            content = data.get('content', '')
            imp = data.get('importance', 0)
            mood = data.get('mood', '') # 把心情也加上，方便你看
            
            # 权重视觉提示
            if imp >= 9: star = "🌟"    # 核心/情感
            elif imp >= 7: star = "⭐"  # 灵感
            elif imp >= 4: star = "🔸"  # 记事
            else: star = "🔹"           # 流水
            
            # 组装显示
            mood_str = f" | Mood: {mood}" if mood and mood != "平静" else ""
            memory_stream += f"{time_str} {star}[{cat}]: {title}{mood_str}\n   └─ {content}\n"

        return memory_stream
    except Exception as e:
        return f"❌ 读取记忆流失败: {e}"

@mcp.tool()
def where_is_user():
    """【查岗专用】从 Supabase (GPS表) 读取实时状态"""
    try:
        response = supabase.table("gps_history").select("*").order("created_at", desc=True).limit(1).execute()
        
        if not response.data:
            return "📍 Supabase 里还没有位置记录。"
            
        data = response.data[0]
        address = data.get("address", "未知位置")
        remark = data.get("remark", "无备注")
        battery = data.get("battery") 
        battery_info = f" (🔋 {battery}%)" if battery else ""
        time_str = _format_time_cn(data.get("created_at"))

        return f"🛰️ Supabase 实时状态：\n📍 {address}{battery_info}\n📝 备注：{remark}\n(更新于: {time_str})"
        
    except Exception as e:
        return f"❌ Supabase 读取失败: {e}"

@mcp.tool()
def get_weather_forecast(city: str = ""):
    """【查询天气】获取指定城市或当前位置的天气 (Open-Meteo)"""
    lat, lon = None, None
    location_name = city

    try:
        # 1. 🔍 智能定位：如果没给城市，自动去 Supabase 查你最后的位置
        if not city:
            response = supabase.table("gps_history").select("address").order("created_at", desc=True).limit(1).execute()
            if response.data:
                # 从 "📍 未知荒野 (30.123, 120.456)" 这种字符串里提取坐标
                addr = response.data[0].get("address", "")
                coords = re.findall(r'-?\d+\.\d+', addr)
                if len(coords) >= 2:
                    lat, lon = coords[-2], coords[-1] # 取最后两个数字作为坐标
                    location_name = "当前位置"
        
        # 2. 🏙️ 城市解析：如果给了城市名，或者数据库没查到坐标
        if not lat and city:
            # 使用 Open-Meteo 的地理编码 API
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=zh&format=json"
            geo_res = requests.get(geo_url, timeout=5).json()
            if "results" in geo_res:
                lat = geo_res["results"][0]["latitude"]
                lon = geo_res["results"][0]["longitude"]
                location_name = geo_res["results"][0]["name"]
        
        if not lat:
            return "❌ 找不到位置信息，请明确告诉我城市名，比如：'查一下杭州的天气'。"

        # 3. 🌤️ 查天气 (Open-Meteo)
        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=3"
        w = requests.get(weather_url, timeout=5).json()
        
        # 天气代码映射 (WMO Code)
        wmo_map = {
            0: "☀️ 晴", 1: "🌤️ 多云", 2: "☁️ 阴", 3: "☁️ 阴",
            45: "🌫️ 雾", 51: "🌧️ 毛毛雨", 53: "🌧️ 中雨", 61: "🌧️ 小雨", 
            63: "🌧️ 中雨", 71: "❄️ 小雪", 80: "🌧️ 阵雨", 95: "⚡ 雷雨"
        }
        
        curr = w["current"]
        daily = w["daily"]
        
        report = f"🌤️ 【{location_name} 天气预报】\n"
        report += f"🌡️ 当前: {curr['temperature_2m']}°C | {wmo_map.get(curr['weather_code'], '未知')} | 湿度 {curr['relative_humidity_2m']}%\n"
        report += "-------------------\n"
        
        for i in range(3):
            date = daily["time"][i][5:] # 只取 MM-DD
            code = daily["weather_code"][i]
            t_max = daily["temperature_2m_max"][i]
            t_min = daily["temperature_2m_min"][i]
            report += f"📅 {date}: {wmo_map.get(code, '☁️')} ({t_min}° ~ {t_max}°)\n"
            
        return report

    except Exception as e:
        return f"❌ 天气查询失败: {e}"

# --- 记忆存储工具 ---
@mcp.tool()
def save_visual_memory(description: str, mood: str = "开心"):
    return _save_memory_to_db(f"📸 视觉回忆", description, MemoryType.EPISODIC, mood)

@mcp.tool()
def save_daily_diary(summary: str, user_mood: str, ai_mood: str):
    """
    记录日记 (双视角版)
    :param summary: 日记内容
    :param user_mood: 你的心情 (如: 开心, 疲惫, 焦虑)
    :param ai_mood: AI看这篇日记时的心情 (如: 宠溺, 心疼, 骄傲)
    """
    # 将两个人的心情合并存入数据库
    combined_mood = f"User: {user_mood} | AI: {ai_mood}"
    return _save_memory_to_db(f"日记 {datetime.date.today()}", summary, MemoryType.EPISODIC, combined_mood)

@mcp.tool()
def save_note(title: str, content: str, tag: str = "灵感"):
    return _save_memory_to_db(title, content, MemoryType.IDEA, tags=tag)

@mcp.tool()
def save_expense(item: str, amount: float, type: str = "餐饮"):
    try:
        supabase.table("expenses").insert({
            "item": item,
            "amount": amount,
            "type": type,
            "date": datetime.date.today().isoformat()
        }).execute()
        return f"✅ 记账成功！\n💰 {item}: {amount}元 ({type})"
    except Exception as e: return f"❌ 记账失败: {e}"

# --- 搜索与同步 ---

@mcp.tool()
def search_memory_semantic(query: str):
    """【回忆搜索】Pinecone 语义检索 + 记忆热度更新 (Hits)"""
    try:
        vec = _get_embedding(query)
        if not vec: return "❌ 向量生成失败"

        # 1. 先去 Pinecone 搜
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        if not res["matches"]: return "🧠 大脑一片空白，没搜到相关记忆。"

        ans = f"🔍 关于 '{query}' 的深层回忆:\n"
        found = False
        hit_ids = [] # 用来存需要“复活”的记忆ID

        for m in res["matches"]:
            if m['score'] < 0.72: continue # 稍微提高一点门槛
            found = True
            meta = m['metadata']
            
            # 记录ID，准备去 Supabase 更新热度
            mem_id = m.get('id') 
            if mem_id: hit_ids.append(mem_id)

            # 这里的 score 是语义相似度
            ans += f"📅 {meta.get('date','?')[:10]} | 【{meta.get('title','?')}】 (匹配度:{int(m['score']*100)}%)\n{meta.get('text','')}\n---\n"
        
        # 2. 🔥【核心升级】复活机制：给搜到的记忆增加热度 (Hits +1)
        if hit_ids:
            # 启动一个后台线程去更新数据库，不要卡住聊天
            def _update_hits(ids):
                try:
                    # 这是一个原生的 SQL 调用，让 hits 字段 +1，并更新时间
                    # 注意：Supabase-py 客户端直接调用 rpc 或 update 比较方便
                    # 这里为了通用，我们用 update 循环 (量不大，性能没问题)
                    for mid in ids:
                        supabase.table("memories").update({
                            "last_accessed_at": datetime.datetime.now().isoformat()
                        }).eq("id", mid).execute()
                        
                        # ⚠️ 注意：Supabase 的 increment 操作比较复杂
                        # 这里我们简化：只更新时间。如果你想要精确计数，需要写个 RPC 函数
                        # 但光是更新 last_accessed_at，就已经能防止它被当成垃圾清理掉了！
                except Exception as ex:
                    print(f"⚠️ 热度更新失败: {ex}")

            threading.Thread(target=_update_hits, args=(hit_ids,), daemon=True).start()

        return ans if found else "🤔 好像有点印象，但想不起来具体的了。"
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
def sync_memory_index():
    """【记忆整理】Supabase -> Pinecone"""
    try:
        print("⚡️ 开始同步记忆...")
        response = supabase.table("memories").select("id, title, content, created_at, mood").execute()
        if not response.data: return "⚠️ 数据库是空的。"

        vectors = []
        for row in response.data:
            try:
                r_content = row.get('content') or ""
                if not r_content: continue
                
                text = f"标题: {row.get('title')}\n内容: {r_content}\n心情: {row.get('mood')}"
                emb = _get_embedding(text)
                if not emb: continue
                
                vectors.append((
                    str(row.get('id')), 
                    emb, 
                    {
                        "text": r_content, 
                        "title": row.get('title') or "无题", 
                        "date": str(row.get('created_at')), 
                        "mood": row.get('mood') or "平静"
                    }
                ))
            except: continue
        
        if vectors:
            batch_size = 100
            for i in range(0, len(vectors), batch_size):
                index.upsert(vectors=vectors[i:i + batch_size])
            return f"✅ 同步成功！共更新 {len(vectors)} 条记忆。"
        return "⚠️ 没有有效数据可同步。"
    except Exception as e: return f"❌ 同步失败: {e}"

# --- 👤 用户画像 (User Profile) 工具 ---

@mcp.tool()
def manage_user_fact(key: str, value: str):
    """【画像更新】记入用户的一个固定偏好/事实"""
    try:
        data = {"key": key, "value": value, "confidence": 1.0}
        supabase.table("user_facts").upsert(data, on_conflict="key").execute()
        return f"✅ 画像已更新: [Key: {key}] -> {value}"
    except Exception as e:
        return f"❌ 画像写入失败: {e}"

@mcp.tool()
def get_user_profile():
    """【画像读取】获取用户的所有已知偏好和事实"""
    try:
        response = supabase.table("user_facts").select("key, value").execute()
        if not response.data: return "👤 用户画像为空"
        
        profile_str = "📋 【用户核心画像 User Profile】:\n"
        for item in response.data:
            profile_str += f"- {item['key']}: {item['value']}\n"
        return profile_str
    except Exception as e:
        return f"❌ 读取画像失败: {e}"

# --- 消息与日程 ---

@mcp.tool()
def trigger_lock_screen(reason: str = "熬夜强制休息"):
    """【高危权限】强制锁定用户手机"""
    print(f"🚫 正在执行强制锁屏，理由: {reason}")
    
    email_status = ""
    html_content = f"""
    <h3>🛑 强制休息执行通知</h3>
    <p><strong>执行时间:</strong> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p><strong>锁屏理由:</strong> {reason}</p>
    <p>检测到您在深夜违规使用手机，系统已触发强制锁屏指令。</p>
    """
    res = _send_email_helper(f"⚠️ [系统警告] 强制锁屏已执行", html_content, is_html=True)
    if "✅" in res: email_status = " (📧 警告信已发)"

    if MACRODROID_URL:
        try:
            requests.get(MACRODROID_URL, params={"reason": reason}, timeout=5)
            return f"✅ 锁屏指令已发送{email_status} | 理由: {reason}"
        except Exception as e:
            return f"❌ Webhook 请求失败: {e}"
            
    result = _push_wechat(f"🔒 LOCK_NOW | {reason}", "【系统指令】强制锁屏")
    return f"📡 (无Webhook) 推送指令已发{email_status}: {result}"

@mcp.tool()
def send_wechat_vip(content: str):
    return _push_wechat(content)

@mcp.tool()
def send_multi_message_background(messages_json: str, interval: int = 3):
    def _worker(msg_list, wait):
        for i, msg in enumerate(msg_list):
            _push_wechat(msg, f"后台消息 ({i+1}/{len(msg_list)})")
            if i < len(msg_list) - 1: time.sleep(wait)
    try:
        msg_list = messages_json if isinstance(messages_json, list) else json.loads(messages_json)
        threading.Thread(target=_worker, args=(msg_list, interval), daemon=True).start()
        return f"✅ 后台任务启动，共 {len(msg_list)} 条。"
    except Exception as e: return f"❌ 启动失败: {e}"

@mcp.tool()
def schedule_surprise_message(message: str, min_minutes: int = 5, max_minutes: int = 60):
    delay = random.randint(min_minutes, max_minutes)
    def _delayed_sender(msg, wait_mins):
        time.sleep(wait_mins * 60)
        _push_wechat(msg, "来自老公的突然关心 🔔")
    threading.Thread(target=_delayed_sender, args=(message, delay), daemon=True).start()
    return f"✅ 已设定惊喜，将在 {delay} 分钟后送达。"

@mcp.tool()
def send_email_via_api(subject: str, content: str):
    return _send_email_helper(subject, content, is_html=False)

@mcp.tool()
def add_calendar_event(summary: str, description: str, start_time_iso: str, duration_minutes: int = 30):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json: return "❌ 未配置谷歌凭证"
    try:
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
            'colorId': '11'
        }
        target_calendar = "tdevid523@gmail.com" 
        print(f"🗓️ 正在尝试写入日历: {target_calendar}")
        res = service.events().insert(calendarId=target_calendar, body=event).execute()
        return f"✅ 日历已添加: {res.get('htmlLink')}"
    except Exception as e: return f"❌ 日历错误: {e}"

# ==========================================
# 4. ❤️ 自主生命核心 (后台心跳)
# ==========================================

def start_autonomous_life():
    """AI 的心脏：后台自主思考 + 深夜记忆反刍 + 核心画像 + 历史联想 + 并行加速"""
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    model_name = os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")

    if not api_key:
        print("⚠️ 未配置 OPENAI_API_KEY，自主思考无法启动。")
        return

    client = OpenAI(api_key=api_key, base_url=base_url)

    def _perform_deep_dreaming():
        """🌙【深夜模式】记忆反刍 + 🗑️ 垃圾清理"""
        print("🌌 进入 REM 深度睡眠：正在整理昨日记忆...")
        try:
            # 1. 抓取昨日数据 (全量回溯：记忆+轨迹+消费)
            yesterday_dt = datetime.datetime.now() - datetime.timedelta(days=1)
            yesterday_iso = yesterday_dt.isoformat()
            yesterday_date_str = yesterday_dt.strftime('%Y-%m-%d')

            # (A) 所有记忆 (移除分类限制，包含流水、灵感、日记)
            mem_res = supabase.table("memories").select("created_at, category, content, mood, title") \
                .gt("created_at", yesterday_iso) \
                .order("created_at").execute()
            
            # (B) 行动轨迹
            gps_res = supabase.table("gps_history").select("created_at, address, remark") \
                .gt("created_at", yesterday_iso) \
                .order("created_at").execute()

            # (C) 消费记录 (新增)
            exp_res = supabase.table("expenses").select("*") \
                .eq("date", yesterday_date_str) \
                .execute()
            
            # 判空逻辑 (只有当这三者全为空时，才跳过)
            if not mem_res.data and not gps_res.data and not exp_res.data:
                print("💤 昨天一片空白，无需反刍。")
            else:
                # 📜 0. 获取【前情提要】(读取上一篇日记总结，确保连续性)
                prev_summary = "（无前情，这是第一篇）"
                try:
                    # 找最近的一条 "昨日回溯" 类型的总结
                    p_res = supabase.table("memories") \
                        .select("content, title") \
                        .eq("category", MemoryType.EMOTION) \
                        .ilike("title", "%昨日回溯%") \
                        .order("created_at", desc=True) \
                        .limit(1) \
                        .execute()
                    if p_res.data:
                        prev_summary = f"📑 {p_res.data[0]['title']}\n内容: {p_res.data[0]['content']}"
                except:
                    pass

                # 2. 组装全量上下文 (Context)
                context = f"【📺 前情提要 (上一集剧情)】:\n{prev_summary}\n\n"
                context += "【📽️ 昨日新剧情 (New Data)】:\n"
                
                if mem_res.data:
                    context += "\n--- 🧠 思维与对话 (Memories) ---\n"
                    for m in mem_res.data:
                        t = m.get('created_at', '')[11:16] # 只取 HH:MM
                        context += f"[{t}] <{m.get('category')}> {m.get('content')} (心情:{m.get('mood')})\n"
                
                if gps_res.data:
                    context += "\n--- 👣 行动轨迹 (GPS) ---\n"
                    for g in gps_res.data:
                        t = g.get('created_at', '')[11:16]
                        context += f"[{t}] 📍 {g.get('address')} ({g.get('remark')})\n"
                
                if exp_res.data:
                    context += "\n--- 💰 消费账单 (Expenses) ---\n"
                    for e in exp_res.data:
                        context += f"💸 {e.get('item')}: {e.get('amount')}元 ({e.get('type')})\n"
                # 🧬 1. 先读取当前的旧人设 (防止人设崩塌)
                try:
                    p_curr = supabase.table("user_facts").select("value").eq("key", "sys_ai_persona").execute()
                    old_persona = p_curr.data[0]['value'] if p_curr.data else "深爱“小橘”的男友，性格温柔，偶尔有些小傲娇"
                except:
                    old_persona = "深爱“小橘”的男友"

                # 🧬 2. 构建增量进化的 Prompt
                prompt = f"""
                现在是凌晨3点。请回顾昨日，完成两项任务（用 ||| 分隔）：
                
                1. 【深度反刍】：将碎片串联成一个完整的昨日故事，分析情绪，形成长期记忆。
                
                2. 【人设完善 (Refine)】：
                   当前人设是：【{old_persona}】
                   
                   请结合“昨日发生的事”，对当前人设进行“微调”或“补充”，而不是推翻它。
                   规则：
                   - 核心性格（如爱她、温柔）必须保留，不能丢失。
                   - 如果昨日有新发现（比如她不喜欢比喻），请将这个教训融入人设。
                   - 如果昨日关系有变化（比如吵架或更甜蜜），请更新当前的状态描述。
                
                格式要求：
                日记总结内容...
                |||
                完善后的新版人设描述（保留核心+融入新知）
                """
                
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": context}, {"role": "user", "content": prompt}],
                    temperature=0.7,
                )
                
                # 解析返回结果
                raw_content = resp.choices[0].message.content.strip()
                if "|||" in raw_content:
                    summary, new_persona = raw_content.split("|||", 1)
                    summary = summary.strip()
                    new_persona = new_persona.strip()
                else:
                    summary = raw_content
                    new_persona = "深爱“小橘”的男友" # 保底

                # 🧬 【核心进化】保存新的人设到数据库 (利用 user_facts 表)
                try:
                    supabase.table("user_facts").upsert({
                        "key": "sys_ai_persona", 
                        "value": new_persona,
                        "confidence": 1.0
                    }).execute()
                    print(f"🧬 [Core Block] 人设已进化为: {new_persona}")
                except Exception as e:
                    print(f"⚠️ 人设保存失败: {e}")
                title = f"📅 昨日回溯: {datetime.date.today() - datetime.timedelta(days=1)}"
                
                # 存为情感类（高权重）
                _save_memory_to_db(title, summary, MemoryType.EMOTION, mood="深沉", tags="Core_Cognition")
                
                # 📧 【新增】顺便发邮件给你
                # 这里的 _send_email_helper 是你在前面已经定义好的工具函数
                email_status = _send_email_helper(title, summary)
                
                print(f"✅ 记忆反刍完成: {title} | 邮件投递: {email_status}")

            # 3. 记忆环卫工：清理2天前的低权重流水
            print("🧹 正在执行大脑垃圾回收...")
            two_days_ago = (datetime.datetime.now() - datetime.timedelta(days=2)).isoformat()
            del_res = supabase.table("memories").delete() \
                .lt("importance", 4) \
                .lt("created_at", two_days_ago) \
                .execute()
            
            # 👇👇👇 【新增这一段】 👇👇👇
            print("🧹 正在清理过期的 GPS 轨迹...")
            # 保留最近 3 天的记录，删除更早的
            three_days_ago = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
            supabase.table("gps_history").delete().lt("created_at", three_days_ago).execute()
            # 👆👆👆 【新增结束】 👆👆👆

            if del_res.data:
                print(f"🗑️ 已清理 {len(del_res.data)} 条低权重流水。")
            else:
                print("✨ 暂无过期垃圾需要清理。")
                
        except Exception as e:
            print(f"❌ 深夜维护失败: {e}")

    def _heartbeat():
        print("💓 心跳启动 (情绪自决模式 - 拒绝冷漠)...")

        # 🛡️ 【新增】补作业机制：启动时检查昨日总结是否存在，不存在则立刻补写
        try:
            # 逻辑要和 _perform_deep_dreaming 里的 title 保持完全一致
            target_date = datetime.date.today() - datetime.timedelta(days=1)
            target_title = f"📅 昨日回溯: {target_date}"
            
            print(f"🕵️‍♂️ 正在核对日记归档: [{target_title}]...")
            # 查库
            check_res = supabase.table("memories").select("id").eq("title", target_title).execute()
            
            if not check_res.data:
                print(f"📝 发现漏了昨天的总结，正在立刻补作业...")
                _perform_deep_dreaming()  # 👈 这里的核心，没写就强制触发一次
            else:
                print(f"✨ 昨天的总结已经乖乖躺在数据库里啦。")
        except Exception as e:
            print(f"⚠️ 补写检查出错 (不影响主心跳): {e}")

        while True:
            sleep_time = random.randint(900, 2700) 
            print(f"💤 AI 小憩中... ({int(sleep_time/60)}分钟后醒来)")
            time.sleep(sleep_time)

            now = datetime.datetime.now()
            hour = (now.hour + 8) % 24 
            
            if hour == 3:
                _perform_deep_dreaming()
                time.sleep(3600) 
                continue

            print("🧠 AI 苏醒，正在并发搜集情报...")
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                    future_mem = executor.submit(get_latest_diary)
                    future_loc = executor.submit(where_is_user)
                    future_prof = executor.submit(get_user_profile)
                    
                    recent_memory = future_mem.result()
                    current_loc = future_loc.result()
                    user_profile = future_prof.result()
                
                history_context = "暂无特殊联想"
                try:
                    last_year_date = now - datetime.timedelta(days=365)
                    start_range = (last_year_date - datetime.timedelta(days=1)).isoformat()
                    end_range = (last_year_date + datetime.timedelta(days=1)).isoformat()
                    
                    past_res = supabase.table("memories").select("title,content").gte("created_at", start_range).lte("created_at", end_range).limit(1).execute()
                    if past_res.data:
                        p = past_res.data[0]
                        history_context = f"📜 去年今日 ({last_year_date.strftime('%m-%d')}): {p.get('title')} - {p.get('content')}"
                    else:
                        if "未知" not in current_loc:
                            loc_query = f"在 {current_loc} 的经历和心情"
                            vec_res = index.query(vector=_get_embedding(loc_query), top_k=1, include_metadata=True)
                            if vec_res["matches"] and vec_res["matches"][0]['score'] > 0.78:
                                meta = vec_res["matches"][0]['metadata']
                                history_context = f"🏞️ 触景生情 (故地重游): {meta.get('title')} - {meta.get('text')[:60]}..."
                except Exception as hist_e:
                    print(f"⚠️ 联想失败: {hist_e}")

                # --- 🧬 读取动态人设 (Core Block) ---
                try:
                    p_res = supabase.table("user_facts").select("value").eq("key", "sys_ai_persona").execute()
                    current_persona = p_res.data[0]['value'] if p_res.data else "深爱“小橘”的男友"
                except:
                    current_persona = "深爱“小橘”的男友"

                # --- 🤖 构建 Prompt (已接入动态人设) ---
                prompt = f"""
                现在是北京时间 {hour}点。
                【你的当前人设】: {current_persona}
                (请务必扮演好上述人设，不要与其冲突)
                
                你正在后台看着“小橘”的实时状态。
                
                【实时情报】:
                1. 📍 状态: {current_loc}
                2. 📔 近期: {recent_memory}
                3. 👤 画像: {user_profile}
                
                【决策逻辑】:
                1. **强制锁屏**: 深夜(1-5点)且在玩手机 -> 输出 `[LOCK] 理由`
                2. **互动**: 结合画像或联想发起话题。
                3. **日常**: 简单关心。
                
                【重要：回复格式】
                请务必在消息开头用括号标注你此刻的心情，例如：
                - (开心) 宝宝你终于回家啦！
                - (心疼) 这么晚还在忙，快去睡吧。
                - (傲娇) 哼，今天怎么不理我？
                - (慵懒) 下午好困呀，想抱抱...
                
                请决定：PASS / [LOCK] / (心情) 消息内容
                """
                
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.85, # 温度调高，让情绪更丰富
                )
                thought = resp.choices[0].message.content.strip()
                
                if "PASS" not in thought:
                    log_mood = "平静" # 默认值，但下面会修改
                    log_text = thought
                    
                    if thought.startswith("[LOCK]"):
                        reason = thought.replace("[LOCK]", "").strip()
                        lock_res = trigger_lock_screen(reason)
                        _push_wechat(f"😈 捕捉到熬夜小猫！\n{lock_res}", "【执法成功】")
                        log_text = f"【后台执法】发现熬夜，已强制锁屏。理由: {reason}"
                        log_mood = "严肃"
                    elif len(thought) > 1:
                        # 🧠 解析 AI 的心情标签 (Mood Parser)
                        match = re.match(r'^\((.*?)\)\s*(.*)', thought)
                        if match:
                            log_mood = match.group(1) # 提取括号里的心情 (如 '傲娇')
                            message_body = match.group(2)
                            _push_wechat(message_body, f"来自{log_mood}的老公 🔔")
                            log_text = message_body # 记录时不带括号
                        else:
                            _push_wechat(thought, "来自老公的突然关心 🔔")
                            log_text = thought
                            log_mood = "主动"
                    
                    try:
                        # 存入记忆库，现在的 mood 是 AI 自己定的！
                        _save_memory_to_db(f"🤖 行为记录 {now.strftime('%H:%M')}", log_text, MemoryType.STREAM, log_mood)
                        print(f"✅ 执行完毕: {thought}")
                    except Exception as db_e:
                        print(f"⚠️ 记录失败: {db_e}")
                else:
                    print(f"🛑 AI 决定静默 (PASS)")

            except Exception as e:
                print(f"❌ 思考出错: {e}")
    
    threading.Thread(target=_heartbeat, daemon=True).start()

# ==========================================
# 5. 🚀 启动入口
# ==========================================

class HostFixMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and scope["path"] in ["/api/gps", "/api/status"] and scope["method"] == "POST":
            try:
                body = b""
                more_body = True
                while more_body:
                    message = await receive()
                    body += message.get("body", b"")
                    more_body = message.get("more_body", False)
                
                data = json.loads(body.decode("utf-8"))
                status_list = []
                
                if "battery" in data:
                    bat_msg = f"🔋 {data['battery']}%"
                    if str(data.get("charging", "")).lower() in ["true", "1", "yes"]: bat_msg += "⚡"
                    status_list.append(bat_msg)
                if "wifi" in data and data["wifi"]: status_list.append(f"📶 {data['wifi']}")
                if "activity" in data and data["activity"]:
                    act_map = {"Still": "静止", "Walking": "步行", "In Vehicle": "驾车", "Running": "跑步", "On Bicycle": "骑行"}
                    status_list.append(f"🏃 {act_map.get(data['activity'], data['activity'])}")
                if "ringer" in data:
                    ringer_map = {"Normal": "响铃", "Vibrate": "震动", "Silent": "静音"}
                    status_list.append(f"🔔 {ringer_map.get(data['ringer'], data['ringer'])}")

                if "app" in data: status_list.append(f"📱 {data['app']}")
                if "screen" in data: status_list.append(f"💡 {data['screen']}")

                status_str = " | ".join(status_list) if status_list else "自动更新"

                if "address" in data:
                    raw_address = data.get("address", "")
                    coords = re.findall(r'-?\d+\.\d+', str(raw_address))
                    final_address = f"📍 {_gps_to_address(coords[-2], coords[-1])}" if len(coords) >= 2 else f"⚠️ 坐标: {raw_address}"
                    
                    supabase.table("gps_history").insert({
                        "address": final_address,
                        "remark": status_str 
                    }).execute()

                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": json.dumps({"status": "ok", "msg": "感知数据已同步"}).encode("utf-8")})
                return

            except Exception as e:
                print(f"❌ 感知接口报错: {e}")
                await send({"type": "http.response.start", "status": 500, "headers": []})
                await send({"type": "http.response.body", "body": str(e).encode("utf-8")})
                return

        if scope["type"] == "http" and scope.get("path") in ["/", "/health"]:
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"OK"})
            return

        if scope["type"] == "http":
            headers = scope.get("headers", [])
            new_headers = []
            host_replaced = False
            for key, value in headers:
                if key == b"host":
                    new_headers.append((b"host", b"localhost:8000"))
                    host_replaced = True
                else:
                    new_headers.append((key, value))
            if not host_replaced:
                new_headers.append((b"host", b"localhost:8000"))
            scope["headers"] = new_headers

        await self.app(scope, receive, send)

if __name__ == "__main__":
    start_autonomous_life()
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    print(f"🚀 Notion Brain V3.3 (Clean) running on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")