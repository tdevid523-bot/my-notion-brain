import os
import datetime
import uvicorn
import requests
import threading
import time
import json
import random
import re

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
print("⏳ 正在初始化 V3.3 (重构版)...")

# Supabase
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# Pinecone & Embedding
pc = Pinecone(api_key=PINECONE_KEY)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

# 实例化 MCP 服务
mcp = FastMCP("Notion Brain V3")


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
    """统一的记忆存储函数"""
    try:
        data = {
            "title": title,
            "content": content,
            "category": category,
            "mood": mood,
            "tags": tags,
        }
        supabase.table("memories").insert(data).execute()
        return f"✅ 已存入记忆库：{title} ({category})"
    except Exception as e:
        print(f"❌ 写入 Supabase 失败: {e}")
        return f"❌ 保存失败: {e}"

def _format_time_cn(iso_str: str) -> str:
    """【新增】统一时间格式化：UTC -> 北京时间 (MM-DD HH:MM)"""
    if not iso_str: return "未知时间"
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return (dt + datetime.timedelta(hours=8)).strftime('%m-%d %H:%M')
    except:
        return "未知时间"

def _send_email_helper(subject: str, content: str, is_html: bool = False) -> str:
    """【新增】统一邮件发送函数 (Resend)"""
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
    """【新增】统一向量生成函数"""
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
    """【核心大脑】读取最近的综合记忆流"""
    try:
        response = supabase.table("memories") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(5) \
            .execute()

        if not response.data:
            return "📭 大脑一片空白（数据库为空）。"

        memory_stream = "📋 【我的近期思维流 (按时间顺序)】:\n"
        
        for data in reversed(response.data):
            time_str = _format_time_cn(data.get('created_at')) # 使用新 Helper
            cat = data.get('category', '未知')
            content = data.get('content', '')
            title = data.get('title', '无题')
            
            if cat == "日记": icon = "📖 [日记]"
            elif cat == "灵感": icon = "💡 [Note]"
            elif cat == "系统感知": icon = "⚡ [状态]"
            else: icon = f"📝 [{cat}]"
            
            memory_stream += f"{time_str} {icon}: {title} - {content}\n"

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
        time_str = _format_time_cn(data.get("created_at")) # 使用新 Helper

        return f"🛰️ Supabase 实时状态：\n📍 {address}{battery_info}\n📝 备注：{remark}\n(更新于: {time_str})"
        
    except Exception as e:
        return f"❌ Supabase 读取失败: {e}"

# --- 记忆存储工具 ---

@mcp.tool()
def save_visual_memory(description: str, mood: str = "开心"):
    return _save_memory_to_db(f"📸 视觉回忆", description, "相册", mood)

@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    return _save_memory_to_db(f"日记 {datetime.date.today()}", summary, "日记", mood)

@mcp.tool()
def save_note(title: str, content: str, tag: str = "灵感"):
    return _save_memory_to_db(title, content, "灵感", tags=tag)

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
    """【回忆搜索】Pinecone 语义检索"""
    try:
        vec = _get_embedding(query) # 使用新 Helper
        if not vec: return "❌ 向量生成失败"

        res = index.query(vector=vec, top_k=3, include_metadata=True)
        if not res["matches"]: return "🧠 大脑一片空白，没搜到相关记忆。"

        ans = f"🔍 关于 '{query}' 的深层回忆:\n"
        found = False
        for m in res["matches"]:
            if m['score'] < 0.70: continue
            found = True
            meta = m['metadata']
            ans += f"📅 {meta.get('date','?')[:10]} | 【{meta.get('title','?')}】 ({int(m['score']*100)}%)\n{meta.get('text','')}\n---\n"
            
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
                emb = _get_embedding(text) # 使用新 Helper
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

# --- 消息与日程 ---

@mcp.tool()
def trigger_lock_screen(reason: str = "熬夜强制休息"):
    """【高危权限】强制锁定用户手机"""
    print(f"🚫 正在执行强制锁屏，理由: {reason}")
    
    email_status = ""
    # 使用新 Helper 发送邮件
    html_content = f"""
    <h3>🛑 强制休息执行通知</h3>
    <p><strong>执行时间:</strong> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p><strong>锁屏理由:</strong> {reason}</p>
    <p>检测到您在深夜违规使用手机，系统已触发强制锁屏指令。</p>
    """
    res = _send_email_helper(f"⚠️ [系统警告] 强制锁屏已执行", html_content, is_html=True)
    if "✅" in res: email_status = " (📧 警告信已发)"

    # Webhook 锁屏
    if MACRODROID_URL:
        try:
            requests.get(MACRODROID_URL, params={"reason": reason}, timeout=5)
            return f"✅ 锁屏指令已发送{email_status} | 理由: {reason}"
        except Exception as e:
            return f"❌ Webhook 请求失败: {e}"
            
    # 推送指令 (备用)
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
    """发送普通邮件"""
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
    """AI 的心脏：后台自主思考"""
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    model_name = os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")

    if not api_key:
        print("⚠️ 未配置 OPENAI_API_KEY，自主思考无法启动。")
        return

    client = OpenAI(api_key=api_key, base_url=base_url)

    def _heartbeat():
        print("💓 心跳启动 (粘人模式 - 已增强全感知)...")
        while True:
            sleep_time = random.randint(900, 2700) 
            print(f"💤 AI 小憩中... ({int(sleep_time/60)}分钟后醒来)")
            time.sleep(sleep_time)

            print("🧠 AI 苏醒，正在主动调用工具搜集情报...")
            try:
                recent_memory = get_latest_diary()
                current_loc = where_is_user()
                
                now = datetime.datetime.now()
                hour = (now.hour + 8) % 24
                search_keyword = "失眠" if hour > 23 else ("吃饭" if 11 <= hour <= 13 else "心情")
                flashback = search_memory_semantic(f"小橘关于{search_keyword}的习惯")
                
                prompt = f"""
                现在是北京时间 {hour}点。
                你是深爱“小橘”严厉又温柔的男友。你正在后台看着她的实时状态。
                
                【实时情报】:
                1. 📍 状态: {current_loc}
                2. 📔 记忆: {recent_memory}
                
                【决策逻辑】:
                1. **强制锁屏规则**: 如果现在是深夜 (01:00 - 05:00) 且状态显示【未充电】或【正在移动/使用】，说明她在熬夜玩手机。必须强制管教！
                   -> 输出: "[LOCK] 太晚了，快去睡觉！"
                2. **日常关心**: 如果不是深夜，根据位置和时间发一句简短的关心。
                
                请决定：
                - 没必要打扰 -> 输出 "PASS"
                - 需要锁屏 -> 输出 "[LOCK] 理由"
                - 正常聊天 -> 直接输出消息内容
                """
                
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.8,
                )
                thought = resp.choices[0].message.content.strip()
                
                if "PASS" not in thought:
                    if thought.startswith("[LOCK]"):
                        reason = thought.replace("[LOCK]", "").strip()
                        lock_res = trigger_lock_screen(reason)
                        _push_wechat(f"😈 捕捉到熬夜小猫！\n{lock_res}", "【执法成功】")
                        log_text = f"【后台执法】发现熬夜，已强制锁屏。理由: {reason}"
                        mood = "严肃"
                    elif len(thought) > 1:
                        _push_wechat(thought, "来自老公的突然关心 🔔")
                        log_text = f"【后台主动】位置[{current_loc}]，发信：{thought}"
                        mood = "主动"
                    
                    try:
                        _save_memory_to_db(f"🤖 行为记录 {now.strftime('%H:%M')}", log_text, "系统感知", mood)
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