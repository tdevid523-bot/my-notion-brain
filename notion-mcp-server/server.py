import os
import datetime
from dotenv import load_dotenv
import uvicorn
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from starlette.types import ASGIApp, Scope, Receive, Send
import chromadb

# 1. 加载配置
load_dotenv()
notion_key = os.getenv("NOTION_API_KEY")
database_id = os.getenv("NOTION_DATABASE_ID")

# 2. 初始化
notion = Client(auth=notion_key)
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_or_create_collection(name="notion_memory")
mcp = FastMCP("Notion Vector Brain")

# --- 工具 1: 写入 (保持不变) ---
@mcp.tool()
def write_notion_page(title: str, content: str, category: str = "日常", date: str = None):
    if not date: date = datetime.date.today().isoformat()
    print(f"⚡️ [写入] {title}")
    try:
        notion.pages.create(
            parent={"database_id": database_id},
            properties={
                "Title": {"title": [{"text": {"content": title}}]},
                "Category": {"select": {"name": category}},
                "Date": {"date": {"start": date}}
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                }
            }]
        )
        return "✅ 写入成功！(记得运行 sync_notion_index 同步)"
    except Exception as e:
        return f"❌ 写入失败: {e}"

# --- 工具 2: 精确读取 (保持不变) ---
@mcp.tool()
def read_notion_exact(category: str = None, date: str = None):
    print(f"⚡️ [精确查阅]")
    return "请使用语义搜索。"

# --- 工具 3: 同步索引 (已修复：只认 ID，不认类型) ---
@mcp.tool()
def sync_notion_index():
    print("⚡️ [开始同步] 正在下载记忆...")
    target_id_clean = database_id.replace("-", "")
    
    try:
        # 搜索所有页面
        all_pages = notion.search(filter={"value": "page", "property": "object"})["results"]
        
        ids = []
        documents = []
        metadatas = []
        count = 0
        
        for page in all_pages:
            parent = page.get("parent", {})
            # 获取父亲 ID (不管它叫 database_id 还是 data_source_id，只要有 ID 就行)
            p_id = parent.get("database_id") or parent.get("data_source_id")
            
            # --- 核心修改：只比对 ID ---
            if p_id and p_id.replace("-", "") == target_id_clean:
                try:
                    props = page["properties"]
                    # 获取标题
                    t_obj = props.get("Title", {}).get("title", [])
                    title = t_obj[0]["text"]["content"] if t_obj else "无标题"
                    
                    # 获取分类
                    c_obj = props.get("Category", {}).get("select")
                    category = c_obj["name"] if c_obj else "未分类"
                    
                    # 获取日期
                    d_obj = props.get("Date", {}).get("date")
                    date = d_obj["start"] if d_obj else "未知"
                    
                    # 读取正文
                    page_id = page["id"]
                    blocks = notion.blocks.children.list(block_id=page_id)
                    content = ""
                    for b in blocks["results"]:
                        if "paragraph" in b and b["paragraph"]["rich_text"]:
                            for t in b["paragraph"]["rich_text"]:
                                content += t["text"]["content"]

                    full_text = f"标题:{title}\n分类:{category}\n日期:{date}\n内容:{content}"
                    
                    ids.append(page_id)
                    documents.append(full_text)
                    metadatas.append({"category": category, "date": date, "title": title})
                    count += 1
                    print(f"   ✅ 已索引: {title}")
                except Exception as e:
                    print(f"   ⚠️ 跳过页面 (格式不对): {e}")
            
        if ids:
            collection.add(ids=ids, documents=documents, metadatas=metadatas)
            msg = f"✅ 同步完成！共索引了 {count} 条记忆。"
        else:
            msg = "⚠️ 同步了 0 条。请检查 Notion 数据库里是不是真的有内容？"
            
        print(msg)
        return msg
    except Exception as e:
        return f"❌ 同步失败: {e}"

# --- 工具 4: 语义搜索 (保持不变) ---
@mcp.tool()
def search_memory_semantic(query: str, n_results: int = 3):
    print(f"⚡️ [大脑思考]: {query}")
    try:
        results = collection.query(query_texts=[query], n_results=n_results)
        if not results['documents'][0]: return "🧠 没找到相关记忆。"
        
        answer = "Found:\n"
        for i, doc in enumerate(results['documents'][0]):
            answer += f"---\n{doc}\n"
        return answer
    except Exception as e:
        return f"❌ Error: {e}"

# --- 中间件 ---
class HostFixMiddleware:
    def __init__(self, app: ASGIApp): self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            headers[b"host"] = b"localhost:8000"
            scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

if __name__ == "__main__":
    print("🚀 【云端版】服务器启动中...")
    
    # 这一行是关键：自动获取云端的端口，如果没有就用 8000
    port = int(os.environ.get("PORT", 8000))
    
    raw_app = mcp.sse_app()
    # 云端通常有自动的 HTTPS，所以我们把 HostFixMiddleware 保留着防止验证错误
    final_app = HostFixMiddleware(raw_app)
    
    # 注意 host 必须是 0.0.0.0，端口变成变量
    uvicorn.run(final_app, host="0.0.0.0", port=port, http="h11")