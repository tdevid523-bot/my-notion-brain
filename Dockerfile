FROM python:3.9-slim

WORKDIR /app

COPY . .

# 只需要安装基本工具，不需要那些复杂的编译环境了
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "server.py"]