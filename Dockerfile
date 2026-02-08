# 使用 3.10 轻量版 (基础占用小，省出内存给软件用)
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 1. 【核心修复】安装编译工具
# 有些软件(如 sentence-transformers)需要 gcc 才能安装，没有就会报错 Exit 1
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# 2. 升级 pip (防止工具太老报错)
RUN pip install --upgrade pip setuptools wheel

# 拷贝文件
COPY . .

# 3. 【分步安装法】防止内存撑爆
# 第一步：只装 CPU 版 Torch (最占内存的先装)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# 第二步：只装 向量模型 和 Pinecone (中等大小)
RUN pip install --no-cache-dir sentence-transformers pinecone

# 第三步：装剩下的 (requirements.txt 里如果重复了会自动跳过，不用担心)
RUN pip install --no-cache-dir -r requirements.txt

# 启动
CMD ["python", "server.py"]