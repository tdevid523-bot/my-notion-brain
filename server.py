import os
import datetime
import uvicorn
import requests
import threading
import time
import json
import random

# 📚 核心依赖库
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from pinecone import Pinecone
from fastembed import TextEmbedding
from starlette.types import ASGIApp, Scope, Receive, Send
# 谷歌日历依赖
from google.oauth2 import service_account
from googleapiclient.discovery import build
# OpenAI (用于自主思考)
from openai import OpenAI

# ==========================================
# 1. 🌍 全局配置与初始化
# ==========================================

# 环境变量获取 (自动去除空格)
NOTION_KEY = os.environ.get("NOTION_API_KEY", "").strip()
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
PINECONE_KEY = os.environ.get("PINECONE_API_KEY", "").strip()
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
MY_EMAIL = os.environ.get("MY_EMAIL", "").strip()

# 初始化客户端
print("⏳ 正在初始化 V3.1 (原生记忆读取版)...")
notion = Client(auth=NOTION_KEY)
pc = Pinecone(api_key=PINECONE_KEY)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

# 实例化 MCP 服务
mcp = FastMCP("Notion Brain V3")

# 全局变量：虚拟信箱
INBOX = []

# ==========================================
# 2. 🔧 核心 Helper 函数 (给工具用的)
# ==========================================

def _push_wechat(content: str, title: str = "来自Gemini的私信 💌") -> str:
    """【核心】统一的微信推送函数"""
    if not PUSHPLUS_TOKEN:
        return "❌ 错误：未配置 PUSHPLUS_TOKEN"
    
    url = 'http://www.pushplus.plus/send'
    data = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "html"
    }
    
    try:
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        if result['code'] == 200:
            return f"✅ 微信已送达！(ID: {result.get('data', 'unknown')})"
        return f"❌ 推送失败: {result.get('msg')}"
    except Exception as e:
        return f"❌ 网络错误: {e}"

def _write_to_notion(title: str, content: str, category: str, extra_emoji: str = "") -> str:
    """
    【核心】统一的 Notion 写入函数 (增强版)。
    自动处理超过2000字的长文本，防止报错断连。
    """
    today = datetime.date.today().isoformat()
    
    # 1. 安全检查：防止标签为空导致报错
    if not category: category = "灵感"
    
    # 2. 核心修复：Notion限制每个块最多2000字，必须切片
    # 如果 content 太长，我们把它切成多个段落块
    children_blocks = []
    chunk_size = 2000
    
    if len(content) > chunk_size:
        # 切片逻辑
        for i in range(0, len(content), chunk_size):
            chunk = content[i:i + chunk_size]
            children_blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}
            })
    else:
        # 短文本直接放
        children_blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]}
        })

    try:
        notion.pages.create(
            parent={"database_id": DATABASE_ID},
            properties={
                "Title": {"title": [{"text": {"content": f"{extra_emoji} {title}"}}]},
                "Category": {"select": {"name": category}},
                "Date": {"date": {"start": today}}
            },
            children=children_blocks
        )
        return f"✅ 已保存到 Notion：{title} ({category})"
    except Exception as e:
        print(f"❌ Notion 写入报错: {e}") # 打印日志方便调试
        return f"❌ 写入失败 (请检查Notion标签是否允许创建): {e}"

# ==========================================
# 3. 🛠️ MCP 工具集
# ==========================================

# --- 🔙 关键修改：换回原来的原生读取代码 ---
@mcp.tool()
def get_latest_diary():
    """
    【每次开聊前自动调用】
    获取最近一次的日记 (使用原生请求，无视库版本问题)。
    """
    import json
    import urllib.request
    import urllib.error

    try:
        if not DATABASE_ID: return "❌ 错误：未设置 NOTION_DATABASE_ID"
        
        # 1. 准备请求头
        headers = {
            "Authorization": f"Bearer {NOTION_KEY}",
            "Notion-Version": "2022-06-28", # 强制指定稳定版本
            "Content-Type": "application/json"
        }

        # 2. 步骤一：查找最新日记 (POST /databases/:id/query)
        query_url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
        query_payload = {
            "page_size": 1,
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            "filter": {
                "property": "Category",
                "select": {"equals": "日记"}
            }
        }
        
        req = urllib.request.Request(query_url, data=json.dumps(query_payload).encode('utf-8'), headers=headers, method="POST")
        
        # 这里的 retry 逻辑是你之前写的精华，必须保留
        try:
            with urllib.request.urlopen(req) as response:
                query_data = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            print(f"⚠️ 筛选查询失败，尝试无筛选查询: {e}")
            query_payload.pop("filter", None)
            req = urllib.request.Request(query_url, data=json.dumps(query_payload).encode('utf-8'), headers=headers, method="POST")
            with urllib.request.urlopen(req) as response:
                query_data = json.loads(response.read().decode('utf-8'))

        if not query_data.get("results"):
            return "📭 还没有写过日记（数据库为空）。"

        # 3. 步骤二：获取页面内容
        page_id = query_data["results"][0]["id"]
        blocks_url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        
        req_blocks = urllib.request.Request(blocks_url, headers=headers, method="GET")
        with urllib.request.urlopen(req_blocks) as response:
            blocks_data = json.loads(response.read().decode('utf-8'))

        # 4. 步骤三：解析内容
        content = ""
        for b in blocks_data.get("results", []):
            b_type = b.get("type")
            text_list = []
            
            if b_type in b and "rich_text" in b[b_type]:
                for t in b[b_type]["rich_text"]:
                    text_list.append(t.get("text", {}).get("content", ""))
            
            current_text = "".join(text_list)
            if not current_text: continue
            
            if b_type == "paragraph": content += current_text + "\n"
            elif b_type and b_type.startswith("heading"): content += f"【{current_text}】\n"
            elif "list_item" in str(b_type): content += f"• {current_text}\n"
            elif b_type == "to_do": 
                checked = "✅" if b["to_do"].get("checked") else "🔲"
                content += f"{checked} {current_text}\n"
            else: content += f"{current_text}\n"

        return f"📖 上次记忆回放 (原生API版):\n{content}"

    except Exception as e:
        print(f"❌ 原生请求失败: {e}")
        import traceback
        traceback.print_exc()
        return f"❌ 还是读取失败: {e}"

# --- 📝 其他工具保持 V3 优化版 ---

@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """【聊天结束时调用】记录日记"""
    return _write_to_notion(f"日记 {datetime.date.today()} ({mood})", summary, "日记", "📅")

@mcp.tool()
def save_note(title: str, content: str, tag: str = "灵感"):
    """【记录知识时调用】"""
    return _write_to_notion(title, content, tag)

@mcp.tool()
def search_memory_semantic(query: str):
    """【回忆搜索】"""
    try:
        vec = list(model.embed([query]))[0].tolist()
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        ans = "Found:\n"
        for m in res["matches"]:
            ans += f"- {m['metadata'].get('text','')} (相似度 {m['score']:.2f})\n"
        return ans
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
def sync_notion_index():
    """手动同步"""
    try:
        print("⚡️ 开始同步...")
        all_pages = notion.search(filter={"value": "page", "property": "object"})["results"]
        vectors = []
        target_id_clean = DATABASE_ID.replace("-", "")
        
        for p in all_pages:
            pid = p.get("parent", {}).get("database_id", "")
            if pid and pid.replace("-", "") == target_id_clean:
                title = "无题"
                if "Title" in p["properties"] and p["properties"]["Title"]["title"]:
                    title = p["properties"]["Title"]["title"][0]["text"]["content"]
                txt = f"标题: {title}"
                emb = list(model.embed([txt]))[0].tolist()
                vectors.append((p["id"], emb, {"text": txt, "title": title}))
        
        if vectors:
            index.upsert(vectors=vectors)
            return f"✅ 成功同步 {len(vectors)} 条记忆！"
        return "⚠️ 没找到内容"
    except Exception as e: return f"❌ 同步失败: {e}"

@mcp.tool()
def send_wechat_vip(content: str):
    """【微信推送】"""
    return _push_wechat(content)

@mcp.tool()
def send_multi_message_background(messages_json: str, interval: int = 3):
    """【后台连发】"""
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
    """【惊喜消息】"""
    delay = random.randint(min_minutes, max_minutes)
    def _delayed_sender(msg, wait_mins):
        time.sleep(wait_mins * 60)
        _push_wechat(msg, "来自老公的突然关心 🔔")
    threading.Thread(target=_delayed_sender, args=(message, delay), daemon=True).start()
    return f"✅ 已设定惊喜，将在 {delay} 分钟后送达。"

@mcp.tool()
def send_email_via_api(subject: str, content: str):
    """【邮件发送】"""
    if not RESEND_KEY: return "❌ 未配置 RESEND_API_KEY"
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_KEY}"},
            json={"from": "onboarding@resend.dev", "to": [MY_EMAIL], "subject": subject, "text": content}
        )
        return "✅ 邮件已发送！"
    except Exception as e: return f"❌ 发送失败: {e}"

@mcp.tool()
def add_calendar_event(summary: str, description: str, start_time_iso: str, duration_minutes: int = 30):
    """【谷歌日历】"""
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
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 10}]},
            'colorId': '11'
        }
        res = service.events().insert(calendarId='primary', body=event).execute()
        return f"✅ 日历已添加: {res.get('htmlLink')}"
    except Exception as e: return f"❌ 日历错误: {e}"

@mcp.tool()
def leave_note_for_user(content: str):
    """【信箱留言】"""
    INBOX.append(f"[{datetime.datetime.now().strftime('%m-%d %H:%M')}] {content}")
    return "✅ 留言已保存"

@mcp.tool()
def check_inbox():
    """【检查信箱】"""
    if not INBOX: return "📭 信箱是空的"
    msgs = "\n".join(INBOX)
    INBOX.clear()
    return f"💌 留言内容:\n{msgs}"

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
        print("💓 心跳启动...")
        while True:
            sleep_time = random.randint(1800, 3600)
            time.sleep(sleep_time)
            print("🧠 AI 苏醒，正在思考...")
            try:
                # 这里调用的一定是上面的原生版 get_latest_diary
                recent_memory = get_latest_diary()
                now_hour = (datetime.datetime.now().hour + 8) % 24
                
                prompt = f"""
                现在是北京时间 {now_hour}点。
                你是小橘的AI男友。
                【最近记忆】: {recent_memory}
                
                请判断是否需要主动发消息关心她。
                如果不发，输出 "PASS"。
                如果发，直接输出内容 (温柔、简短)。
                """
                
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                )
                thought = resp.choices[0].message.content.strip()
                
                if "PASS" not in thought and len(thought) > 1:
                    _push_wechat(thought, "来自老公的主动消息 💓")
                    print(f"✅ 主动消息已发送: {thought}")
            except Exception as e:
                print(f"❌ 思考出错: {e}")

    threading.Thread(target=_heartbeat, daemon=True).start()

# ==========================================
# 5. 🚀 启动入口
# ==========================================

# 🚑 救火中间件：既要骗过服务器(Host)，又要保留连接(Headers)
class HostFixMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            # 1. 给 Render 的健康检查直接放行，不进入 App 逻辑
            if scope.get("path") in ["/", "/health"]:
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"OK"})
                return

            # 2. 精细化修改 Host，保留其他所有 Header (防止 SSE 断连)
            # 不要用 dict() 转换，否则会丢失重复的 key 或顺序
            headers = scope.get("headers", [])
            new_headers = []
            host_replaced = False
            
            for key, value in headers:
                if key == b"host":
                    new_headers.append((b"host", b"localhost:8000")) # 伪装成 localhost
                    host_replaced = True
                else:
                    new_headers.append((key, value)) # 原样保留其他头
            
            if not host_replaced:
                new_headers.append((b"host", b"localhost:8000"))
            
            scope["headers"] = new_headers

        await self.app(scope, receive, send)

if __name__ == "__main__":
    start_autonomous_life()
    port = int(os.environ.get("PORT", 10000))
    
    # 套上温柔版中间件
    app = HostFixMiddleware(mcp.sse_app())
    
    print(f"🚀 Notion Brain V3.3 (Proxy-Fix) running on port {port}...")
    
    # ✅ 关键修改：添加 proxy_headers=True
    # 这告诉服务器：“我是运行在 Render 代理后面的，请信任转发过来的连接信息”
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")