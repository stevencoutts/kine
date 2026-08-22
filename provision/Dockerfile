FROM python:3.12-alpine
RUN pip install --no-cache-dir httpx pyyaml
WORKDIR /app
COPY . /app
ENTRYPOINT ["python", "-u", "provision.py"]
