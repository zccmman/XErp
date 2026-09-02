FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY kernel ./kernel
COPY migrations ./migrations
COPY mcp-server ./mcp-server
COPY scripts ./scripts
COPY deploy ./deploy
COPY alembic.ini ./

# 依赖清单以 pyproject.toml [project.dependencies] 为真源，此处需与之保持一致
RUN pip install --no-cache-dir \
    "sqlalchemy>=2.0" \
    "alembic>=1.13" \
    "fastmcp>=2.0" \
    "psycopg[binary]>=3.1" \
    "fastapi>=0.141" \
    "uvicorn>=0.52" \
    "casbin>=1.43" \
    "casbin_sqlalchemy_adapter>=1.4" \
    "cryptography>=49.0" \
    "lark-oapi>=1.7"

# 默认以 HTTP MCP 服务启动；init 一次性任务由 compose 覆盖 command
CMD ["python", "deploy/run_http.py"]
