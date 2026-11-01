FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
WORKDIR /code
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock /code/
RUN uv sync --frozen --no-dev
COPY ./app /code/app
ENV PATH="/code/.venv/bin:$PATH"
CMD ["fastapi", "run", "app/main.py", "--port", "8000"]
