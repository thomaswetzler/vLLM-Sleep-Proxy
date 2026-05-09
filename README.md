# vLLM Sleep Proxy Stack

Dieses Repo deployt einen lokalen LLM-Stack auf Kubernetes mit drei getrennten
Lebenszyklen:

- `vllm-models`: laedt Modellgewichte in den gemeinsamen PVC-Cache
- `vllm`: Runtime-Stack als Umbrella-Release
- `vllm-bootstrap`: laeuft den langen Warmup-/Auto-Sleep-Zyklus getrennt von Helm

Der wichtige Architekturwechsel ist:

- Der lange Auto-Sleep-Lauf ist kein `post-install`-Hook mehr.
- `make deploy` deployed erst den Runtime-Stack und wartet danach separat auf
  den Bootstrap-Job.
- Dadurch bleibt Helm nicht mehr in `pending-install` haengen, nur weil das
  serielle GPU-Warmup lange dauert.

## Releases

Die aktuellen Release-Namen sind bewusst stabil gehalten:

- `vllm-models` -> Chart `helm/models`
- `vllm` -> Umbrella-Chart `helm/stack-core`
- `vllm-bootstrap` -> Umbrella-Chart `helm/stack-bootstrap`

Die beiden neuen Umbrella-Charts enthalten:

- `helm/stack-core`
  - `sleep-proxy`
  - `vllm`
  - `litellm`
  - `ops-ui`
  - `playground`
- `helm/stack-bootstrap`
  - `vllm-bootstrap`

## Lokale Installation

Voraussetzungen:

- Zugriff auf den Cluster, z. B.:
  - `export KUBECONFIG=$HOME/.kube/config_k3s`
- `.env` mit mindestens:
  - `EXTERN_DOMAIN=...`
  - `LITELLM_MASTER_KEY=...`
- Hugging Face Token fuer den Model-Download:
  - `export HUGGING_FACE_TOKEN=hf_...`

Empfohlene Reihenfolge:

```bash
make model-download
make check-models
make deploy
```

Das bedeutet im Detail:

1. `make model-download`
   - deployt `vllm-models`
   - laedt die Gewichte nach `vllm-model-cache`
   - erstellt dabei das Secret `huggingface-credentials`

2. `make check-models`
   - prueft die erwarteten Modellverzeichnisse im PVC

3. `make deploy`
   - deployt zuerst `vllm` aus `helm/stack-core`
   - wartet danach auf die externen TLS-Zertifikate fuer `litellm`, `ops-ui`
     und `vllm-playground`
   - startet fehlgeschlagene cert-manager-Issuance-Laeufe bei Bedarf neu
   - deployt danach `vllm-bootstrap` aus `helm/stack-bootstrap`
   - wartet anschliessend per `kubectl wait` auf den Bootstrap-Job

Nuetzliche Teilziele:

```bash
make deploy-core
make deploy-bootstrap
make undeploy-core
make undeploy-bootstrap
```

## Warum Zwei Releases?

Die GPU-Modelle muessen pro Node seriell geladen werden. Diese Serialisierung
passiert weiterhin in den Init-Containern ueber den Dateilock unter
`/data/locks/gpu-loading-<node>`.

Der Unterschied ist nur die Steuerung:

- Frueher: Helm wartete selbst auf einen `post-install`-Hook-Job
- Heute: Helm deployed nur den Runtime-Stack, danach wartet `make` separat auf
  einen normalen Kubernetes-Job

Damit bleibt der Runtime-Release sauber auf `deployed`, auch wenn das Warmup
lange dauert oder die CLI-Session unterbrochen wird.

## Wichtige Charts

- Runtime-Umbrella: [helm/stack-core/Chart.yaml](helm/stack-core/Chart.yaml)
- Bootstrap-Umbrella: [helm/stack-bootstrap/Chart.yaml](helm/stack-bootstrap/Chart.yaml)
- Bootstrap-Job: [helm/vllm-bootstrap/templates/auto-sleep-job.yaml](helm/vllm-bootstrap/templates/auto-sleep-job.yaml)
- Runtime-Deploylogik: [Makefile](Makefile)

## ArgoCD Zielbild

Fuer ArgoCD ist die saubere Zielstruktur:

1. Application `vllm`
   - Pfad: `helm/stack-core`
   - Release-Name: `vllm`
   - Sync-Wave: `0`

2. Application `vllm-bootstrap`
   - Pfad: `helm/stack-bootstrap`
   - Release-Name: `vllm-bootstrap`
   - Sync-Wave: `1`

3. `vllm-models` vorerst separat oder manuell
   - Pfad: `helm/models`
   - Heute noch kein idealer ArgoCD-Kandidat, weil der Download-Job dort noch
     als Helm-Hook implementiert ist und sehr lange laufen kann

### Empfohlene Reihenfolge in ArgoCD

- optional `vllm-models` mit Wave `-1`
- `vllm` mit Wave `0`
- `vllm-bootstrap` mit Wave `1`

### Wichtiger Hinweis fuer `vllm-bootstrap`

Der Bootstrap-Job braucht pro Lauf eine neue Kennung:

- Value: `vllm-bootstrap.autoSleepHook.runId`

Lokal setzt `make deploy` diese Kennung automatisch als Timestamp.

In ArgoCD muss dieser Wert ebenfalls geaendert werden, wenn der Bootstrap-Job
neu erstellt werden soll. Ohne neue `runId` wird kein neuer Job erzeugt.

Praktisch bedeutet das:

- Entweder `runId` bei einem gewollten Re-Deploy in Git aktualisieren
- oder spaeter einen kleinen Generator/CI-Schritt davor setzen

## ArgoCD Werte

Fuer ArgoCD ist eine kleine Environment-Values-Datei sinnvoller als viele
einzelne Parameter-Overrides, weil die Ingress-Hosts Listen mit `paths`
enthalten.

Beispiel fuer `stack-core`:

```yaml
vllm:
  vllm-stack:
    routerSpec:
      ingress:
        hosts:
          - host: llm-router.example.com
            paths:
              - path: /
                pathType: Prefix

litellm:
  secret:
    data:
      masterKey: "change-me"
  ingress:
    hosts:
      - host: litellm.example.com
        paths:
          - path: /
            pathType: Prefix
    tls:
      - secretName: litellm-tls
        hosts:
          - litellm.example.com

ops-ui:
  ingress:
    hosts:
      - host: ops-ui.example.com
        paths:
          - path: /
            pathType: Prefix
    tls:
      - secretName: ops-ui-tls
        hosts:
          - ops-ui.example.com

playground:
  ingress:
    hosts:
      - host: vllm-playground.example.com
        paths:
          - path: /
            pathType: Prefix
    tls:
      - secretName: vllm-playground-tls
        hosts:
          - vllm-playground.example.com
```

Beispiel fuer `stack-bootstrap`:

```yaml
vllm-bootstrap:
  coreReleaseName: vllm
  autoSleepHook:
    runId: "20260509120000"
```

## Aktueller Stand

Die Runtime-Services referenzieren weiterhin die bekannten internen DNS-Namen:

- `sleep-proxy-service`
- `vllm-router-service`
- `lite-helm-litellm`

Das wurde absichtlich so gelassen, damit bestehende interne Referenzen in
`litellm`, `ops-ui` und `playground` stabil bleiben, obwohl der Deploy jetzt
ueber Umbrella-Charts laeuft.
