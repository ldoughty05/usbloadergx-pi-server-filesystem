# wiipi: makes a Raspberry Pi look like a USB drive to a Wii and installs games onto it remotely.
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-aiohttp \
      wit dosfstools util-linux mount kmod \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY wiipi/ /app/wiipi/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8765
CMD ["python3", "-m", "wiipi"]
