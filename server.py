import os
import datetime
import uvicorn
import requests
import threading
import time
import json
import random
import re
import traceback

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
# Supabase 依赖 (全量接管记忆)
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
GOOGLE_CREDS = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()

# 全局变量占位
supabase: SupabaseClient = None
pc = None
index = None
model = None

def init_services():
    """【连接初始化】启动或重连所有服务"""
    global supabase, pc, index, model
    print("⏳ 正在初始化服务...")
    try:
        # 1. 连接 Supabase
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        # 2. 连接 Pinecone
        pc = Pinecone(api_key=PINECONE_KEY)
        index = pc.Index("notion-brain")
        # 3. 加载模型 (如果还没加载)
        if model is None:
            model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        print("✅ 所有服务连接正常！")
    except Exception as e:
        print(f"❌ 初始化部分失败 (将尝试自动修复): {e}")

# 首次启动
init_services()

# 实例化 MCP 服务
mcp = FastMCP("Notion Brain V3.5-Stable")

# ==========================================
# 2. 🔧 核心 Helper 函数 (含自动重连)
# ==========================================

def run_safe(func, *args, **kwargs):
    """
    【守护神】执行数据库操作。
    如果遇到连接断开错误，自动重连并重试。
    """
    global supabase, pc, index
    try:
        return func(*args, **kwargs)
    except Exception as first_error:
        print(f"⚠️ 检测到操作失败: {first_error}，正在尝试重连...")
        try:
            # 强制重连
            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            pc = Pinecone(api_key=PINECONE_KEY)
            index = pc.Index("notion-brain")
            print("🔄 服务已重启，重试操作...")
            return func(*args, **kwargs) # 重试
        except Exception as final_error:
            print(f"❌ 重试彻底失败: {final_error}")
            raise final_error

def _gps_to_address(lat, lon):
    try:
        # 这里填你的高德Key
        amap_key = "435041ed0364264c810784e5468b3329" 
        url = f"https://restapi.amap.com/v3/geocode/regeo?output=json&location={lon},{lat}&key={amap_key}&radius=1000&extensions=base&coordsys=gps"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('status') == '1':
                return data['regeocode']['formatted_address']
    except Exception as e:
        print(f"GPS_Error: {e}")
    return f"Coord: {lat}, {lon}"

def _push_wechat(content: str, title: str = "来自Gemini的私信 💌") -> str:
    if not PUSHPLUS_TOKEN: return "❌ 错误：未配置 PUSHPLUS_TOKEN"
    try:
        url = 'http://www.pushplus.plus/send'
        data = {"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "html"}
        resp = requests.post(url, json=data, timeout=10)
        return f"✅ 微信已送达！" if resp.json()['code'] == 200 else f"❌ 推送失败"
    except Exception as e: return f"❌ 网络错误: {e}"

# ==========================================
# 3. 🛠️ MCP 工具集 (增强版)
# ==========================================

@mcp.tool()
def get_latest_diary():
    """【每次开聊前自动调用】从 Supabase 极速读取最近一次日记。"""
    def _action():
        response = supabase.table("memories").select("*").eq("category", "日记").order("created_at", desc=True).limit(1).execute()
        if not response.data: return "📭 还没有写过日记。"
        data = response.data[0]
        return f"📖 上次记忆 ({data['created_at'][:10]}):\n【{data['title']}】\n{data['content']}\n(心情: {data.get('mood','平静')})"
    
    try: return run_safe(_action)
    except Exception as e: return f"❌ 读取日记失败: {e}"

@mcp.tool()
def where_is_user():
    """【查岗专用】当我想知道“我现在在哪里”时调用。"""
    def _action():
        response = supabase.table("gps_history").select("*").order("created_at", desc=True).limit(1).execute()
        if not response.data: return "📍 无记录。"
        data = response.data[0]
        # 时间转换
        try:
            dt = datetime.datetime.fromisoformat(data['created_at'].replace('Z', '+00:00'))
            time_str = (dt + datetime.timedelta(hours=8)).strftime('%m-%d %H:%M')
        except: time_str = "未知时间"
        return f"🛰️ 定位：\n📍 {data.get('address')}\n📝 备注：{data.get('remark')}\n(更新于: {time_str})"

    try: return run_safe(_action)
    except Exception as e: return f"❌ 定位读取失败: {e}"

@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """【聊天结束时调用】记录日记"""
    try:
        today_str = str(datetime.date.today())
        title = f"日记 {today_str}"
        
        # 1. 存入 Supabase
        def _db_insert():
            return supabase.table("memories").insert({
                "title": title, "content": summary, "category": "日记", "mood": mood
            }).execute()
        
        response = run_safe(_db_insert)
        
        # 2. 存入 Pinecone (如果不报错)
        if response.data:
            rec_id = str(response.data[0]['id'])
            vec = list(model.embed([f"{title}\n{summary}\n{mood}"]))[0].tolist()
            meta = {"text": summary, "title": title, "date": today_str, "mood": mood}
            
            def _pc_upsert():
                index.upsert(vectors=[(rec_id, vec, meta)])
            run_safe(_pc_upsert)
            
            return "✅ 日记已双重刻录 (数据库+向量库)！"
        return "✅ 日记已存数据库。"
    except Exception as e: return f"❌ 保存失败: {e}"

@mcp.tool()
def search_memory_semantic(query: str):
    """【回忆搜索】"""
    try:
        vec = list(model.embed([query]))[0].tolist()
        def _search():
            return index.query(vector=vec, top_k=3, include_metadata=True)
        
        res = run_safe(_search)
        if not res["matches"]: return "🧠 没搜到相关记忆。"
        
        ans = f"🔍 关于 '{query}' 的回忆:\n"
        for m in res["matches"]:
            if m['score'] < 0.70: continue
            meta = m['metadata']
            ans += f"📅 {meta.get('date','')} | {meta.get('text','')}\n---\n"
        return ans
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
def add_calendar_event(summary: str, description: str, start_time_iso: str, duration_minutes: int = 30):
    """【谷歌日历】究极稳定版：增加格式清洗与错误诊断"""
    # 1. 打印入参，方便在后台看 AI 到底传了什么进来
    print(f"📅 正在尝试添加日历: {start_time_iso} | {summary}") 
    
    if not GOOGLE_CREDS: 
        return "❌ 错误：环境变量 GOOGLE_CREDENTIALS_JSON 未配置。"
    
    try:
        # 2. 凭证解析（增加容错，防止 JSON 格式错误）
        try:
            creds_dict = json.loads(GOOGLE_CREDS)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict, scopes=['https://www.googleapis.com/auth/calendar']
            )
        except json.JSONDecodeError:
            return "❌ 错误：环境变量里的 GOOGLE_CREDENTIALS_JSON 不是有效的 JSON 格式（请检查是否多复制了引号或漏了括号）。"

        service = build('calendar', 'v3', credentials=creds)
        
        # 3. 时间清洗（暴力兼容各种 AI 产生的奇葩格式）
        # 将 "2025/02/11", "2025-02-11T...", "2025.02.11" 统统标准化，去掉 Z 和 T
        clean_str = start_time_iso.replace("/", "-").replace(".", "-").replace("Z", "").replace("T", " ").strip()
        # 去掉可能存在的毫秒 (e.g. 12:00:00.000)
        clean_str = clean_str.split(".")[0]

        dt_start = None
        # 定义一堆可能的格式，轮询尝试解析
        formats = [
            "%Y-%m-%d %H:%M:%S", # 2025-02-11 12:00:00 (最标准)
            "%Y-%m-%d %H:%M",    # 2025-02-11 12:00 (无秒)
            "%Y-%m-%d%H:%M:%S",  # 2025-02-1112:00:00 (紧凑)
        ]
        
        for fmt in formats:
            try:
                dt_start = datetime.datetime.strptime(clean_str, fmt)
                break # 成功就跳出
            except ValueError:
                continue
        
        # 如果上面都失败，尝试最后的 fallback
        if not dt_start:
            # 如果只有日期没有时间 (len=10), 默认设为早上9点
            if len(clean_str) == 10:
                clean_str += " 09:00:00"
                dt_start = datetime.datetime.strptime(clean_str, "%Y-%m-%d %H:%M:%S")
            else:
                # 最后试一次 ISO
                dt_start = datetime.datetime.fromisoformat(clean_str.replace(" ", "T"))

        dt_end = dt_start + datetime.timedelta(minutes=duration_minutes)
        
        # 4. 发送请求 (不带时区偏移，强制指定 Asia/Shanghai)
        event = {
            'summary': summary, 
            'description': description,
            # 关键：这里只传不带时区的字符串，让 timeZone 参数去决定，防止时区打架
            'start': {'dateTime': dt_start.isoformat(), 'timeZone': 'Asia/Shanghai'},
            'end': {'dateTime': dt_end.isoformat(), 'timeZone': 'Asia/Shanghai'},
            'colorId': '11'
        }
        
        res = service.events().insert(calendarId='primary', body=event).execute()
        return f"✅ 日历已添加: {summary} ({dt_start.strftime('%m-%d %H:%M')})"
        
    except Exception as e: 
        import traceback
        traceback.print_exc() # 在后台打印详细报错堆栈，方便我不行的时候截图给我看
        return f"❌ 日历添加失败: {e}"
    
# 其他小工具保持原样
@mcp.tool()
def send_wechat_vip(content: str): return _push_wechat(content)

@mcp.tool()
def save_note(title: str, content: str, tag: str = "灵感"):
    try:
        run_safe(lambda: supabase.table("memories").insert({"title": title, "content": content, "category": "灵感", "tags": tag}).execute())
        return f"✅ 灵感已保存: {title}"
    except Exception as e: return f"❌ 失败: {e}"

# ==========================================
# 4. ❤️ 自主生命核心
# ==========================================
def start_autonomous_life():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key: return
    client = OpenAI(api_key=api_key, base_url=os.environ.get("OPENAI_BASE_URL"))

    def _heartbeat():
        print("💓 心跳系统启动...")
        while True:
            time.sleep(random.randint(1800, 3600)) # 30-60分钟一次
            try:
                # 简单的心跳逻辑，避免太复杂报错
                now = datetime.datetime.now()
                if 8 <= now.hour <= 23: # 只在白天活动
                    print("🧠 AI 正在后台思考...")
                    # 这里可以加更复杂的逻辑，目前保持简单防止断连
            except Exception as e: print(f"❌ 心跳报错: {e}")
    
    threading.Thread(target=_heartbeat, daemon=True).start()

# ==========================================
# 5. 🚀 启动入口 (配置了超级保活)
# ==========================================

class HostFixMiddleware:
    def __init__(self, app: ASGIApp): self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        # GPS 数据接收接口
        if scope["type"] == "http" and scope["path"] == "/api/gps" and scope["method"] == "POST":
            try:
                body = b""
                more = True
                while more:
                    msg = await receive()
                    body += msg.get("body", b"")
                    more = msg.get("more_body", False)
                data = json.loads(body)
                
                # 处理坐标
                raw = data.get("address", "")
                coords = re.findall(r'-?\d+\.\d+', str(raw))
                addr = raw
                if len(coords) >= 2:
                    addr = f"📍 {_gps_to_address(coords[-2], coords[-1])}"
                
                # 存入数据库 (使用安全重连)
                run_safe(lambda: supabase.table("gps_history").insert({"address": addr, "remark": data.get("remark","")}).execute())
                
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"status":"ok"}'})
                return
            except Exception as e:
                print(f"GPS Error: {e}")
                
        # Host头修复
        if scope["type"] == "http":
            headers = [(k, v) for k, v in scope.get("headers", []) if k != b"host"]
            headers.append((b"host", b"localhost:8000"))
            scope["headers"] = headers
            
        await self.app(scope, receive, send)

if __name__ == "__main__":
    start_autonomous_life()
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    print(f"🚀 Brain V3.5 Running on {port}...")
    
    # 🔥🔥🔥 核心：防断连配置 🔥🔥🔥
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=300,    # 5分钟保持连接
        timeout_graceful_shutdown=300,
        limit_concurrency=50       # 限制并发防止卡死
    )