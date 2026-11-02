FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.8.2 /uv /usr/local/bin/uv
WORKDIR /opt/awm
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY examples ./examples
RUN uv sync --locked --no-dev --no-editable \
    && useradd --uid 10001 --create-home awm \
    && mkdir -p /data && chown awm:awm /data
ENV PATH="/opt/awm/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER awm
EXPOSE 8000
CMD ["python", "-m", "app.container"]
