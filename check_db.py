import os
from dotenv import load_dotenv
from notion_client import Client

# 加载密码
load_dotenv()
notion = Client(auth=os.getenv("NOTION_API_KEY"))

print("🔍 正在暴力搜索所有权限内容...")

try:
    # 关键修改：不加任何 filter 参数，直接搜索所有内容
    response = notion.search()
    results = response.get("results")
    
    # 我们自己在代码里筛选出数据库
    databases = [item for item in results if item["object"] == "database"]

    if not databases:
        print("\n❌ 机器人说：我还是没看到数据库！")
        print("请确认两点：")
        print("1. 你刚才截图里的那个页面，是不是就是我们要找的数据库？")
        print("2. 尝试在左侧侧边栏，直接右键点击该数据库 -> Copy Link，把那个链接发给我看看。")
    else:
        print(f"\n✅ 成功！机器人找到了 {len(databases)} 个数据库：\n")
        for db in databases:
            # 获取标题
            title_list = db.get("title", [])
            if title_list:
                title = title_list[0].get("plain_text", "无标题")
            else:
                title = "无标题"
            
            print(f"📂 数据库名称: 【{title}】")
            print(f"🔑 ID: {db['id']}") 
            print("------------------------------------------------")
            print("👉 请复制上面这个 ID，填进 .env 文件的 NOTION_DATABASE_ID 里！")

except Exception as e:
    print(f"❌ 运行出错: {e}")