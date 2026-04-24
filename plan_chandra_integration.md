# Plan: Integration von Chandra OCR 2 in den vLLM-Sleep-Proxy

Dieser Plan beschreibt die Architektur und die Umsetzungsschritte, um das Modell \`datalab-to/chandra-ocr-2\` auf einer Consumer-GPU (NVIDIA RTX 4060 Ti, 16 GB VRAM) innerhalb des bestehenden k3s-Clusters nutzbar zu machen.

Da moderne vLLM-Versionen (ab v0.17.0, welche für Chandra zwingend erforderlich sind) Consumer-Karten oft nicht mehr stabil unterstützen (PagedAttention / NCCL Fehler) und das Modell mit ca. 11-14 GB VRAM den Speicher für den vLLM KV-Cache stark limitiert, wird eine **robuste HuggingFace-FastAPI Brücke** gebaut.

## Zielarchitektur

Anstatt das offizielle \`lmcache/vllm-openai\` Image zu verwenden, erstellen wir einen leichtgewichtigen Python-Pod (\`chandra-engine\`), der die HuggingFace-Transformers-Bibliothek nutzt. 
Dieser Pod emuliert die API-Endpunkte eines vLLM-Routers, sodass er sich nahtlos in den bestehenden \`sleep-proxy\` einklinkt.

### Die "Fake vLLM" Endpunkte der chandra-engine:
1. **\`/health\` & \`/v1/models\`**: Zur Erkennung durch den \`vllm-deployment-router\`.
2. **\`/is_sleeping\`**: Meldet den aktuellen Zustand (wach/schlafend).
3. **\`/wake_up\`**: Lädt das Modell \`datalab-to/chandra-ocr-2\` in den VRAM der RTX 4060 Ti.
4. **\`/sleep\`**: Führt \`del model\` und \`torch.cuda.empty_cache()\` aus, um die 11 GB VRAM sofort für andere Modelle (z.B. Llama 3.1) freizugeben.
5. **\`/v1/chat/completions\`**: Nimmt OpenAI-kompatible Requests (inklusive Base64-Bildern/PDFs) von LiteLLM an, verarbeitet sie via \`chandra.model.hf.generate_hf\` und gibt strukturiertes JSON/Markdown zurück.

---

## Umsetzungsschritte (Sobald im Execute-Modus)

### 1. Python-Brücke erstellen (\`src/chandra-engine/\`)
* Verzeichnis \`src/chandra-engine/app\` anlegen.
* \`requirements.txt\` erstellen (inkl. \`fastapi\`, \`uvicorn\`, \`chandra-ocr[hf]\`, \`torch\`, \`transformers\`).
* \`main.py\` implementieren:
  * FastAPI-App aufsetzen.
  * Dummy-Routen für \`/sleep\`, \`/wake_up\` und \`/is_sleeping\` implementieren.
  * Speicherverwaltung (\`torch.cuda.empty_cache()\`) in die \`/sleep\` Route einbauen.
  * Die OpenAI Chat-Completion API (\`/v1/chat/completions\`) rudimentär nachbauen, um Bild-Daten an \`chandra-ocr\` zu übergeben.

### 2. Docker-Image bauen
* \`Dockerfile\` in \`src/chandra-engine/\` anlegen.
* Im \`Makefile\` ein neues Build-Target \`build-chandra\` und \`push-chandra\` hinzufügen.
* Image für \`linux/amd64\` bauen und in die GitHub Container Registry (\`ghcr.io\`) pushen.

### 3. Modell Download (Shared PVC)
* Im \`Makefile\` die Variable \`MODEL_DIRS\` um \`datalab-to/chandra-ocr-2\` erweitern.
* \`make model-download\` ausführen, damit das Modell im Cluster-PVC gecacht wird.

### 4. Helm-Chart erweitern (\`helm/vllm/values.yaml\`)
* Einen neuen Block unter \`modelSpec\` auf Node \`lab01\` (RTX 4060 Ti) hinzufügen:
  ```yaml
      - name: "chandra-ocr-2-lab01-gpu"
        repository: "ghcr.io/thomaswetzler/chandra-engine" # Unser Custom Image
        tag: "latest"
        modelURL: "/data/models/chandra-ocr-2"
        replicaCount: 1
        nodeName: "lab01"
        resources:
          requests:
            cpu: "2"
            memory: "4Gi"
          limits:
            cpu: "8"
            memory: "16Gi"
  ```
* Da das Custom-Image keine \`vllmConfig\` Parameter versteht, müssen wir sicherstellen, dass die Helm-Templates (z.B. \`deployment.yaml\`) diesen Pod korrekt ausrollen, auch wenn er eine abweichende Start-Kommandozeile hat (ggf. Template-Anpassung nötig).

### 5. LiteLLM Routing (\`helm/litellm/values.yaml\`)
* Das Modell im Gateway registrieren:
  ```yaml
    - enabled: true
      alias: local/chandra-ocr-2
      upstreamModel: local/chandra-ocr-2
      apiBase: http://sleep-proxy-service.vllm.svc.cluster.local:8080/v1
      apiKey: not-used
      modelInfo:
        lane: vision
  ```

### 6. Deployment & Test
* \`make deploy-vllm\` und \`make deploy-litellm\` ausführen.
* Via \`kubectl logs\` prüfen, ob die \`chandra-engine\` startet und sich beim Router meldet.
* Inference-Test mit einem PDF/Bild über LiteLLM durchführen und den automatischen \`wake_up\` / \`sleep\` Zyklus überwachen.
