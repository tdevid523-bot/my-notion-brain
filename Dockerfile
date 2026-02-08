# 换成标准版 Python (虽然大一点，但自带所有编译工具，不会报错)
FROM python:3.9

# 设置工作目录
WORKDIR /app

# 1. 先把文件都拷进去
COPY . .

# 2. 升级 pip (防止安装工具太老)
RUN pip install --upgrade pip

# 3. 【关键】先单独安装 CPU 版 PyTorch
# 这一步是为了防止它去下载 800MB 的显卡版驱动
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# 4. 最后安装剩下的依赖
# 增加 timeout 防止网络卡顿导致报错
RUN pip install --no-cache-dir -r requirements.txt --default-timeout=100

# 启动
CMD ["python", "server.py"]