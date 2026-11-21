FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY pytz-2026.6-py2.py3-none-any.whl /tmp/
RUN pip install --no-index --no-deps --disable-pip-version-check /tmp/pytz-2026.6-py2.py3-none-any.whl
ADD pinned-source.tar.gz /project/
RUN python -m compileall -q /project/sqlglot /project/tests
COPY selector.py semantic.py replay.py impact-index.json.gz semantic-index.json.gz source-reads.json.gz isolated.json.gz module-profile.json.gz /app/
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
