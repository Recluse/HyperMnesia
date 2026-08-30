# Cross-encoder reranker service (bge-reranker-v2-m3). CPU by default; if you run with an
# a GPU, swap the torch install below for a CUDA wheel and run with the nvidia runtime
# (server.py currently selects mps-or-cpu, so add a cuda branch there too). The model lazy-loads and
# unloads after RERANK_IDLE_SEC, so it only holds ~3-4 GB while actively serving.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
      torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir transformers sentencepiece protobuf safetensors

WORKDIR /app
COPY server.py /app/server.py

ENV RERANK_MODEL=BAAI/bge-reranker-v2-m3 PORT=8091 RERANK_IDLE_SEC=600
EXPOSE 8091
CMD ["python", "server.py"]
