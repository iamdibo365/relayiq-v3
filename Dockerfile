# For the AWS step later (the EKS kit in voice-sandwich-eks can host this image).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir "uv>=0.8,<1"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
 && .venv/bin/playwright install --with-deps chromium
COPY src ./src
ENV PATH="/app/.venv/bin:${PATH}" PYTHONPATH=/app/src
EXPOSE 8000
CMD ["uvicorn", "relayiq.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
