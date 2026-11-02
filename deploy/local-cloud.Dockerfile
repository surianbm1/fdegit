FROM python:3.13-slim
WORKDIR /srv
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2
COPY app/local_cloud.py app/local_cloud_ui.html ./app/
RUN useradd --uid 10001 --create-home publisher && mkdir /data && chown publisher /data
USER publisher
CMD ["python", "-m", "app.local_cloud"]
