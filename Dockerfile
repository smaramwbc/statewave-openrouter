# Single-file app: copy what the wheel needs, install, drop privileges.
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE statewave_openrouter.py ./
RUN pip install --no-cache-dir .

USER nobody
ENV PORT=8080
EXPOSE 8080
# Shell form so PORT can be overridden by the platform (Cloud Run, Fly, ...).
CMD uvicorn statewave_openrouter:app --host 0.0.0.0 --port ${PORT}
