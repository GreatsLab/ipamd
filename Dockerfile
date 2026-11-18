FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY ipamd ./ipamd
RUN pip install --no-cache-dir . && useradd -r -u 10001 ipamd && mkdir /data && chown ipamd /data
USER ipamd
ENV IPAMD_DB=/data/ipamd.sqlite3
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["ipamd", "--host", "0.0.0.0", "--port", "8000"]
