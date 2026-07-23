# Backend/worker image with CUDA 11.8 + cuDNN 8 runtime, Python 3.8, Poetry, GDAL, and osmium-tool

FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu20.04

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_VIRTUALENVS_CREATE=true \
    POETRY_VIRTUALENVS_IN_PROJECT=false \
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
RUN python -m pip install "poetry==${POETRY_VERSION}"

WORKDIR /app

# Copy backend pyproject.toml and entrypoint script
COPY pyproject.toml ./
COPY poetry.lock ./
COPY docker-entrypoint.sh ./

# Configure Poetry to use the correct Python and install dependencies
RUN poetry config virtualenvs.create true && \
    poetry env use /usr/bin/python3.8 && \
    poetry lock --no-update && \
    poetry install --no-interaction --no-ansi && \
    # Ensure CUDA CUPTI runtime (libcupti.so.11.7) is available inside the Poetry venv
    poetry run python -m pip install "nvidia-cuda-cupti-cu11==11.7.101"

# Make entrypoint script executable
RUN chmod +x docker-entrypoint.sh

# Install PyTorch Geometric extension packages with CUDA 11.8 wheels
# into the Poetry virtualenv so Node2Vec and related ops are available.
# These wheels must match the pinned torch version (2.0.1) from backend/pyproject.toml
RUN poetry run python -m pip install \
      pyg-lib torch-scatter torch-sparse torch-cluster \
      -f https://data.pyg.org/whl/torch-2.0.1+cu118.html

ENV PATH="/root/.local/bin:${PATH}"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["bash"]
