FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QT_QPA_PLATFORM=offscreen \
    PYTHONPATH=/usr/lib/python3/dist-packages \
    PATH=/opt/calibre:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-libtorrent \
        ca-certificates \
        wget \
        xz-utils \
        unzip \
        libegl1 \
        libopengl0 \
        libxcb-cursor0 \
        libfreetype6 \
        libfontconfig1 \
        libxkbcommon0 \
        libglx0 \
    && rm -rf /var/lib/apt/lists/*

RUN wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh \
        | sh /dev/stdin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common.py downloader.py converter.py ./