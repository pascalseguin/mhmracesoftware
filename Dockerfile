FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" jinja2 requests itsdangerous python-multipart
COPY . .
ENV MHM_HOST=0.0.0.0
ENV MHM_PORT=8000
EXPOSE 8000
CMD ["python", "run_server.py"]
