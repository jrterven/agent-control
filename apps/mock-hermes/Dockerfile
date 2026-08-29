FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10002 mock-hermes \
    && useradd --system --uid 10002 --gid mock-hermes --home-dir /nonexistent --shell /usr/sbin/nologin mock-hermes

WORKDIR /opt/mock-hermes
COPY pyproject.toml ./
COPY src src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

USER 10002:10002
EXPOSE 19119 18642
ENTRYPOINT ["/opt/venv/bin/mock-hermes"]
CMD ["--host", "0.0.0.0", "--dashboard-port", "19119", "--api-port", "18642"]
