ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-aiohttp py3-yaml

COPY app.py /app.py
COPY index.html /index.html
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
