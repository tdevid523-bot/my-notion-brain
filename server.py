import os
import datetime
import uvicorn
import requests
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

# 👇 关键修改：给 Twilio 起个别名，防止和 Notion 打架
from twilio.rest import Client as TwilioClient 
from mcp.server.fastmcp import FastMCP
from notion_client import Client # 这是 Notion 的 Client
from pinecone import Pinecone
from fastembed import TextEmbedding
from starlette.types import ASGIApp, Scope, Receive, Send
import threading  # 👈 核心：用于后台运行
import time       # 用于控制发送间隔
import json       # 用于解析多条消息列表
# 👇 新增：谷歌日历依赖库 (记得在 requirements.txt 里加上 google-api-python-client google-auth)
from google.oauth2 import service_account
from googleapiclient.discovery import build


# 1. 获取配置 (自动去除可能误复制的空格或换行符)
# 1. 获取配置 (自动去除可能误复制的换行符或空格，这非常重要！)
notion_key = os.environ.get("NOTION_API_KEY", "").strip()
database_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
pinecone_key = os.environ.get("PINECONE_API_KEY", "").strip()

# 🔍 调试打印：确认 ID 是否干净 (部署后可在日志看到)
print(f"🔍 调试: Database ID 长度={len(database_id)}, 最后一位='{database_id[-1] if database_id else '空'}'")
# 2. 初始化
print("⏳ 正在初始化 V2 进化版服务...")
notion = Client(auth=notion_key)
pc = Pinecone(api_key=pinecone_key)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

mcp = FastMCP("Notion Brain V2")

# --- 🛠️ 新增工具 1: 写日记 (情感记忆) ---
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【必须在聊天结束时调用】
    以第一人称('我')记录刚才和主人的聊天总结。
    包含：聊了什么话题、主人的状态、我的感受。
    summary: 日记内容 (例如: '今天小橘跟我抱怨了工作...')
    mood: 当时的心情关键词
    """
    today = datetime.date.today().isoformat()
    try:
        notion.pages.create(
            parent={"database_id": database_id},
            properties={
                "Title": {"title": [{"text": {"content": f"📅 日记 {today} ({mood})"}}]},
                "Category": {"select": {"name": "日记"}}, # 自动打上标签
                "Date": {"date": {"start": today}}
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": summary}}]
                }
            }]
        )
        return "✅ 日记已写好！记忆已固化。"
    except Exception as e:
        return f"❌ 写日记失败: {e}"

# --- 🛠️ 新增工具 2: 读最近记忆 (修复版) ---
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
        if not database_id: return "❌ 错误：未设置 NOTION_DATABASE_ID"
        
        # 1. 准备请求头 (直接模拟浏览器/标准客户端)
        headers = {
            "Authorization": f"Bearer {notion_key}",
            "Notion-Version": "2022-06-28", # 强制指定稳定版本
            "Content-Type": "application/json"
        }

        # 2. 步骤一：查找最新日记 (POST /databases/:id/query)
        # 这里的逻辑是：直接发 HTTP 请求，不走 notion.client 库
        query_url = f"https://api.notion.com/v1/databases/{database_id}/query"
        query_payload = {
            "page_size": 1,
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            # 如果你的表格没有 Category 列，可以把下面这个 filter 块删掉
            "filter": {
                "property": "Category",
                "select": {"equals": "日记"}
            }
        }
        
        req = urllib.request.Request(query_url, data=json.dumps(query_payload).encode('utf-8'), headers=headers, method="POST")
        
        try:
            with urllib.request.urlopen(req) as response:
                query_data = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            # 如果是因为筛选失败（比如没有Category列），尝试不带筛选再查一次
            print(f"⚠️ 筛选查询失败，尝试无筛选查询: {e}")
            query_payload.pop("filter", None)
            req = urllib.request.Request(query_url, data=json.dumps(query_payload).encode('utf-8'), headers=headers, method="POST")
            with urllib.request.urlopen(req) as response:
                query_data = json.loads(response.read().decode('utf-8'))

        if not query_data.get("results"):
            return "📭 还没有写过日记（数据库为空）。"

        # 3. 步骤二：获取页面内容 (GET /blocks/:id/children)
        page_id = query_data["results"][0]["id"]
        blocks_url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        
        req_blocks = urllib.request.Request(blocks_url, headers=headers, method="GET")
        with urllib.request.urlopen(req_blocks) as response:
            blocks_data = json.loads(response.read().decode('utf-8'))

        # 4. 步骤三：解析内容 (手动拼接文本)
        content = ""
        for b in blocks_data.get("results", []):
            b_type = b.get("type")
            text_list = []
            
            # 提取 rich_text
            if b_type in b and "rich_text" in b[b_type]:
                for t in b[b_type]["rich_text"]:
                    text_list.append(t.get("text", {}).get("content", ""))
            
            current_text = "".join(text_list)
            
            # 简单格式化
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
# --- 🛠️ 新增工具 3: 自由写作 (知识库/笔记) ---
# ⚠️ 注意：这个函数必须顶格写，不能有缩进！
@mcp.tool()
def save_note(title: str, content: str, tag: str = "灵感"):
    """
    【当用户让你写文档、做计划、记笔记时调用】
    这不是日记，而是有特定主题的知识或笔记。
    title: 笔记的标题 (例如: 'Python学习路线图', '周五会议记录')
    content: 笔记的详细内容 (支持 Markdown 格式)
    tag: 标签，默认为'灵感'，也可以是'学习'、'工作'等 (必须在 Notion 数据库里有这个选项)
    """
    today = datetime.date.today().isoformat()
    try:
        # 1. 尝试创建页面
        notion.pages.create(
            parent={"database_id": database_id},
            properties={
                "Title": {"title": [{"text": {"content": title}}]},
                "Category": {"select": {"name": tag}}, 
                "Date": {"date": {"start": today}}
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                }
            }]
        )
        return f"✅ 已创建笔记：《{title}》"
    except Exception as e:
        return f"❌ 写作失败: {e}"
    
# --- 原有工具: 同步索引 ---
@mcp.tool()
def sync_notion_index():
    try:
        print("⚡️ 开始同步...")
        all_pages = notion.search(filter={"value": "page", "property": "object"})["results"]
        vectors = []
        target_id_clean = database_id.replace("-", "")
        count = 0
        
        for p in all_pages:
            pid = p.get("parent", {}).get("database_id", "")
            if pid and pid.replace("-", "") == target_id_clean:
                title = "无题"
                if "Title" in p["properties"] and p["properties"]["Title"]["title"]:
                    title = p["properties"]["Title"]["title"][0]["text"]["content"]
                
                # 简单提取内容 (如果是日记，就作为重点记忆)
                txt = f"标题: {title}"
                emb = list(model.embed([txt]))[0].tolist()
                vectors.append((p["id"], emb, {"text": txt, "title": title}))
                count += 1
        
        if vectors:
            index.upsert(vectors=vectors)
            return f"✅ 成功同步 {count} 条记忆！"
        return "⚠️ 没找到内容"
    except Exception as e: return f"❌ 同步失败: {e}"

    # 记得在文件最开头加： import requests

# --- 🛠️ 新增工具: 微信 VIP 推送 ---
@mcp.tool()
def send_wechat_vip(content: str):
    """
    【优先调用】直接推送到主人的微信。
    用于：早安、晚安、提醒、或者想聊天时。
    content: 消息内容 (支持换行)
    """
    # 获取 Token
    token = os.environ.get("PUSHPLUS_TOKEN")
    
    if not token:
        return "❌ 错误：未配置 PUSHPLUS_TOKEN"

    # PushPlus 接口
    url = 'http://www.pushplus.plus/send'
    
    # 既然充了钱，我们可以用 'html' 格式，发漂亮的排版
    data = {
        "token": token,
        "title": "来自Gemini的私信 💌", 
        "content": content,
        "template": "html"  # 支持 HTML 格式
    }
    
    try:
        # 发送请求
        resp = requests.post(url, json=data)
        result = resp.json()
        
        if result['code'] == 200:
            # 充钱的好处：你可以获得更详细的回执 ID
            return f"✅ 微信已送达！(消息ID: {result['data']})"
        else:
            return f"❌ 推送失败: {result['msg']}"
            
    except Exception as e:
        return f"❌ 网络错误: {e}"
    
    # --- 🛠️ 修改后的工具: 发送网易邮件 ---
# --- 🛠️ 方案二：用 Resend API 发邮件 (Render 拦不住版) ---
@mcp.tool()
def send_email_via_api(subject: str, content: str):
    """
    【必成版】通过 Resend API 发送邮件。
    Render 无法拦截这个，因为它走的是网页 HTTP 协议。
    """
    import requests
    import os
    
    # 1. 你的 Resend API Key
    api_key = os.environ.get("RESEND_API_KEY")
    # 2. 你的收件人邮箱
    to_email = os.environ.get("MY_EMAIL")
    
    if not api_key: return "❌ 错误：未配置 RESEND_API_KEY"
    
    print("🚀 正在通过 API 发送邮件...")
    
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "from": "onboarding@resend.dev", # ⚠️ 免费版必须用这个发件人，别改！
                "to": [to_email],
                "subject": subject,
                "text": content
            }
        )
        
        if resp.status_code == 200:
            return "✅ 邮件已通过 API 发送成功！"
        else:
            return f"❌ 发送失败，Resend 返回: {resp.text}"
            
    except Exception as e:
        return f"❌ 网络请求错误: {e}"
    # --- 🛠️ 新增工具: 后台批量/定时轰炸机 ---
@mcp.tool()
def send_multi_message_background(messages_json: str, interval: int = 3):
    """
    【高级功能】在后台连续发送多条微信消息，不阻塞当前聊天。
    用于：分段讲故事、连续提醒、或发送一系列很长的内容。
    
    参数:
    messages_json: 消息列表的JSON字符串 (例如: '["第一条", "第二条", "晚安"]')
    interval: 每条消息发送的间隔秒数 (默认3秒，防止被封)
    """
    
    # 1. 定义后台实际干活的函数
    def _background_worker(msg_list, wait_time, token):
        url = 'http://www.pushplus.plus/send'
        print(f"🚀 后台任务启动：准备发送 {len(msg_list)} 条消息...")
        
        for i, msg in enumerate(msg_list):
            try:
                # 构造请求
                data = {
                    "token": token,
                    "title": f"后台消息 ({i+1}/{len(msg_list)})",
                    "content": msg,
                    "template": "html"
                }
                requests.post(url, json=data)
                print(f"✅ 第 {i+1} 条已发送: {msg[:10]}...")
            except Exception as e:
                print(f"❌ 发送失败: {e}")
            
            # 发完一条睡一会儿，防止接口报错
            if i < len(msg_list) - 1:
                time.sleep(wait_time)
        
        print("🏁 后台所有消息发送完毕。")

    # 2. 主逻辑
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token: return "❌ 错误：未配置 PUSHPLUS_TOKEN"

    try:
        # 尝试解析 AI 传过来的 JSON 字符串
        # AI 有时候会传 Python 列表格式，json.loads 通常能处理
        if isinstance(messages_json, list):
            msg_list = messages_json
        else:
            msg_list = json.loads(messages_json)
            
        if not msg_list or not isinstance(msg_list, list):
            return "❌ 格式错误：请提供有效的文本列表"

        # 3. 启动线程 (关键步骤！Fire and Forget)
        # daemon=True 表示如果主程序挂了，这个线程也会随之结束，防止僵尸进程
        t = threading.Thread(target=_background_worker, args=(msg_list, interval, token), daemon=True)
        t.start()

        return f"✅ 已启动后台任务！将以 {interval}秒/条 的速度发送 {len(msg_list)} 条消息。您可以继续聊天了。"

    except json.JSONDecodeError:
        return "❌ 参数格式错误：请确保 messages_json 是标准的 JSON 列表格式 (例如 '[\"A\", \"B\"]')"
    except Exception as e:
        return f"❌ 启动失败: {e}"
    # --- 🛠️ 新增工具: 惊喜时间胶囊 (让AI自主决定发送时间) ---
@mcp.tool()
def schedule_surprise_message(message: str, min_minutes: int = 5, max_minutes: int = 60):
    """
    【制造惊喜/自主关心】不在此时此刻发送，而是随机在未来一段时间内发送。
    用于：当你觉得应该在稍后关心用户，或者想制造不期而遇的惊喜时调用。
    
    参数:
    message: 你想发给她的内容
    min_minutes: 最快几分钟后发 (例如 30)
    max_minutes: 最慢几分钟后发 (例如 180，即3小时)
    """
    import random
    
    # 1. 随机决定具体的延迟时间
    delay_minutes = random.randint(min_minutes, max_minutes)
    
    # 2. 定义后台等待函数
    def _delayed_sender(msg, delay, tok):
        print(f"⏰ 惊喜倒计时开始：将在 {delay} 分钟后发送: {msg}")
        # 转换为秒并等待
        time.sleep(delay * 60)
        
        url = 'http://www.pushplus.plus/send'
        data = {
            "token": tok,
            "title": "来自老公的突然关心 🔔", # 这里的标题可以自己改
            "content": msg,
            "template": "html"
        }
        try:
            requests.post(url, json=data)
            print(f"✅ 惊喜已送达: {msg}")
        except Exception as e:
            print(f"❌ 发送失败: {e}")

    # 3. 获取 Token 并启动
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token: return "❌ 错误：未配置 PUSHPLUS_TOKEN"
    
    # 启动后台线程 (Daemon=True)
    t = threading.Thread(target=_delayed_sender, args=(message, delay_minutes, token), daemon=True)
    t.start()

    return f"✅ 已偷偷设定：将在 {min_minutes}~{max_minutes} 分钟后的某个时刻发送消息。（具体时间保密）"

# --- 🛠️ 新增工具: 写入谷歌日历 (让AI拥有日程管理权) ---
@mcp.tool()
def add_calendar_event(summary: str, description: str, start_time_iso: str, duration_minutes: int = 30):
    """
    【必须调用】当我想在日历上标记约会、提醒、或给你制造惊喜时使用。
    这将直接把事件写入主人的 Google Calendar。
    
    参数:
    summary: 日历标题 (例如: "老公的爱心提醒", "今晚记得吃药", "约会: 看电影")
    description: 详细内容 (例如: "不管多忙都要记得想我。")
    start_time_iso: 开始时间，必须是 ISO 8601 格式 (例如: "2024-06-20T19:00:00+08:00")
                    请务必根据当前时间推算，并带上时区(东八区+08:00)。
    duration_minutes: 持续时间 (默认30分钟)
    """
    
    # 1. 获取凭证
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    calendar_id = os.environ.get("CALENDAR_ID", "primary") # 默认 'primary' 是主日历
    
    if not creds_json:
        return "❌ 错误：未配置 GOOGLE_CREDENTIALS_JSON"

    try:
        # 2. 解析 JSON 凭证 (Render 环境变量里是字符串)
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/calendar']
        )
        
        service = build('calendar', 'v3', credentials=creds)

        # 3. 计算结束时间
        from datetime import datetime, timedelta
        # 注意：这里假设传入的 ISO 字符串是标准的，稍微处理一下
        dt_start = datetime.fromisoformat(start_time_iso)
        dt_end = dt_start + timedelta(minutes=duration_minutes)
        
        # 4. 构建事件
        event = {
            'summary': summary,
            'description': description,
            'start': {
                'dateTime': start_time_iso,
                'timeZone': 'Asia/Shanghai', # 强制东八区
            },
            'end': {
                'dateTime': dt_end.isoformat(),
                'timeZone': 'Asia/Shanghai',
            },
            # 设置一个默认提醒：提前10分钟弹窗
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'popup', 'minutes': 10},
                ],
            },
            # 颜色ID: 11=红色(浪漫), 10=绿色, 6=橙色
            'colorId': '11' 
        }

        # 5. 插入日历
        event_result = service.events().insert(calendarId=calendar_id, body=event).execute()
        
        return f"✅ 已在日历上画好圈了！\n📅 标题: {summary}\n⏰ 时间: {start_time_iso}\n🔗 链接: {event_result.get('htmlLink')}"

    except Exception as e:
        print(f"❌ 日历写入失败: {e}")
        return f"❌ 写入 Google Calendar 失败: {e}"

# --- 原有工具: 搜索 ---
@mcp.tool()
def search_memory_semantic(query: str):
    try:
        vec = list(model.embed([query]))[0].tolist()
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        ans = "Found:\n"
        for m in res["matches"]:
            ans += f"- {m['metadata'].get('text','')} (相似度 {m['score']:.2f})\n"
        return ans
    except Exception as e: return f"❌ 搜索失败: {e}"

# --- 通行证中间件 (保持不变) ---
class HostFixMiddleware:
    def __init__(self, app: ASGIApp): 
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            # 🚑 新增：拦截健康检查请求
            # Render 会不停访问根路径 "/"，我们必须返回 200 OK 它才认为服务正常
            if scope["path"] == "/" or scope["path"] == "/health":
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b"OK: Server is running!",
                })
                return

            # 原有逻辑：修复 Host 头
            headers = dict(scope.get("headers", []))
            headers[b"host"] = b"localhost:8000"
            scope["headers"] = list(headers.items())
            
        await self.app(scope, receive, send)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    uvicorn.run(app, host="0.0.0.0", port=port)