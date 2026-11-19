FROM alpine:3.21

RUN apk add --no-cache python3 py3-aiohttp py3-yaml

COPY app.py /app.py
COPY index.html /index.html

CMD ["python3", "/app.py"]
