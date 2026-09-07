# Cross-encoder reranker service (bge-reranker-v2-m3). CPU by default; if you run with an
# a GPU, swap the torch install below for a CUDA wheel and run with the nvidia runtime
# (server.py currently selects mps-or-cpu, so add a cuda branch there too). The model lazy-loads and
# unloads after RERANK_IDLE_SEC, so it only holds ~3-4 GB while actively serving.
FROM python:3.12-slim

# Pin what goes into an image someone else will run. Unconstrained installs mean two builds of
# the same Dockerfile can ship different code, and "it worked last month" stops being checkable.
RUN pip install --no-cache-dir \
      torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir \
      transformers==4.57.1 sentencepiece==0.2.1 protobuf==6.33.0 safetensors==0.6.2

WORKDIR /app
COPY server.py /app/server.py

ENV RERANK_MODEL=BAAI/bge-reranker-v2-m3 PORT=8091 RERANK_IDLE_SEC=600
EXPOSE 8091
CMD ["python", "server.py"]
