# Backend/worker image with CUDA 11.8 + cuDNN 8 runtime, Python 3.8, venv, GDAL, and osmium-tool

FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu20.04

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH \
    DEBIAN_FRONTEND=noninteractive \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    curl \
    ca-certificates \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    python3.8 \
    python3.8-dev \
    python3.8-venv \
    python3.8-distutils \
    build-essential \
    git \
    gdal-bin \
    libgdal-dev \
    python3-gdal \
    libgeos-dev \
    libproj-dev \
    osmium-tool \
    graphviz \
    libgraphviz-dev \
    postgresql-client \
    libgomp1 \
    wget \
    && apt-get remove -y python3.8-distutils || true \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sS https://bootstrap.pypa.io/pip/3.8/get-pip.py | python3.8

RUN ln -sf /usr/bin/python3.8 /usr/bin/python && \
    ln -sf /usr/bin/python3.8 /usr/bin/python3

RUN python -m pip install --upgrade pip
RUN python -m pip install --upgrade packaging setuptools wheel

WORKDIR /app

# Copy requirements and entrypoint script
COPY requirements.txt ./
COPY docker-entrypoint.sh ./

# Create a virtual environment and install runtime dependencies.
# Keep a bootstrap copy under /opt so the ./backend:/app bind mount does not
# hide it; the entrypoint seeds /app/.venv from this copy on first run.
RUN python -m venv .venv && \
    .venv/bin/pip install --upgrade "pip<24.1" setuptools wheel && \
    .venv/bin/pip install -r requirements.txt && \
    .venv/bin/pip install "nvidia-cuda-cupti-cu11==11.7.101" && \
    .venv/bin/pip install \
      pyg-lib torch-scatter torch-sparse torch-cluster \
      -f https://data.pyg.org/whl/torch-2.0.1+cu118.html && \
    cp -a .venv /opt/venv_bootstrap

# Make entrypoint script executable
RUN chmod +x docker-entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["bash"]
