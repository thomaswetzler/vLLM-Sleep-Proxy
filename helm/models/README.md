# model-loader chart

Dieses Chart lädt Hugging-Face-Modelle in ein gemeinsames PVC und legt
zusätzlich die gemeinsam genutzten vLLM-Chat-Templates auf einem separaten PVC
ab.

## Install

```bash
helm upgrade --install vllm-models ./helm/models \
  -n vllm --create-namespace --wait --wait-for-jobs
```

Mit HuggingFace Token (empfohlen, für private Modelle und schnellere Downloads):

```bash
helm upgrade --install vllm-models ./helm/models \
  -n vllm --create-namespace --wait --wait-for-jobs \
  --set-string huggingfaceCredentials.token="$HUGGING_FACE_TOKEN"
```

Standardmäßig werden zwei PVCs verwendet:

* ``vllm-model-cache`` (80Gi) fuer Modelle unter ``/data/models``
* ``vllm-templates-pvc`` (256Mi) fuer Chat-Templates unter ``/templates``

Die Modelle landen unter:

| Modell | Pfad | Use Case | Node |
|--------|------|----------|------|
| BAAI/bge-large-en-v1.5 | `/data/models/baai-bge-large-en-v1.5` | Embeddings EN (CPU) | CPU |
| gaunernst/gemma-3-4b-it-int4-awq | `/data/models/gemma-3-4b-it` | Kleiner Allrounder | lab02 |
| hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4 | `/data/models/llama-3.1-8b-instruct` | Default-Lane | lab01 |
| Qwen/Qwen2.5-Coder-7B-Instruct-AWQ | `/data/models/qwen2.5-coder-7b-instruct` | Coding-Lane | lab02 |
| Qwen/Qwen2.5-14B-Instruct-AWQ | `/data/models/qwen2.5-14b-instruct` | Qualitaetsmodus | lab01 |
| deepseek-ai/DeepSeek-R1-Distill-Qwen-7B | `/data/models/deepseek-r1-distill-qwen-7b` | Optionale Reasoning-Lane | vorbereitet, standardmaessig deaktiviert |

Hinweis: Das Download-Chart wertet jetzt ``enabled: true|false`` pro
Modelleintrag aus. Damit kann das vorbereitete DeepSeek-Modell spaeter durch
einfaches Umstellen auf ``enabled: true`` in den Download aufgenommen werden.

Zusaetzlich wird aktuell folgendes Chat-Template vorbereitet:

| Template | Zielpfad | Zweck |
|----------|----------|-------|
| `tool_chat_template_llama3.1_json.jinja` | `/templates/tool_chat_template_llama3.1_json.jinja` | Standardnahes Tool-Calling fuer die lokale Llama-3.1-Lane |
