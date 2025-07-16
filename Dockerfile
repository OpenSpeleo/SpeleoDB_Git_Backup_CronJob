FROM python:latest

RUN apt update && apt install -y --no-install-recommends \
    git \
    curl \
    wget \
    unzip \
    vim \
    nano \
    zsh \
    tmux \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /workspace

ADD . /workspace/

CMD ["/root/.local/bin/uv", "--quiet", "run", "main.py"]
