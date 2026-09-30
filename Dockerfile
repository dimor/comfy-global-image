FROM filebrowser/filebrowser:v2.63.23 AS filebrowser
FROM nvidia/cuda:13.0.0-runtime-ubuntu24.04

ARG COMFY_VERSION=v0.38.0
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH PIP_NO_CACHE_DIR=1 \
    COMFY_VERSION=${COMFY_VERSION} HF_HOME=/workspace/.cache/huggingface \
    TORCH_HOME=/workspace/.cache/torch
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv git ffmpeg curl ca-certificates libgl1 libglib2.0-0 tini \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv && pip install --upgrade pip
RUN pip install torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cu130
RUN git clone --depth 1 --branch ${COMFY_VERSION} https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI
RUN pip freeze > /opt/torch-constraints.txt && \
    pip install -c /opt/torch-constraints.txt -r /opt/ComfyUI/requirements.txt \
    -r /opt/ComfyUI/manager_requirements.txt jupyterlab && \
    pip check && pip freeze > /opt/image-packages.txt
COPY --from=filebrowser /bin/filebrowser /usr/local/bin/filebrowser
COPY start.py /opt/start.py
RUN python -m py_compile /opt/start.py && \
    cd /opt/ComfyUI && python main.py --cpu --quick-test-for-ci
EXPOSE 8188 8888 8080
WORKDIR /opt/ComfyUI
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/opt/start.py"]
