SHELL := /bin/sh

# Load .env if present (variables defined there take precedence over defaults below)
-include .env
export

NAMESPACE        ?= vllm
KUBECONFIG       ?= $(HOME)/.kube/config_k3s
# Expand leading ~ to $(HOME) in case .env uses ~/... notation
KUBECONFIG       := $(patsubst ~/%,$(HOME)/%,$(KUBECONFIG))
export KUBECONFIG

HELM_CACHE_HOME  ?= $(CURDIR)/.helm/cache
HELM_CONFIG_HOME ?= $(CURDIR)/.helm/config
HELM_DATA_HOME   ?= $(CURDIR)/.helm/data
export HELM_CACHE_HOME
export HELM_CONFIG_HOME
export HELM_DATA_HOME

LLM_ROUTER_URL   ?= http://llm-router.$(EXTERN_DOMAIN)
LLM_GATEWAY_URL  ?= http://llm-router.$(EXTERN_DOMAIN)
MODELS_URL       ?= $(LLM_GATEWAY_URL)/v1/models?include=node
COMPLETIONS_URL  ?= $(LLM_GATEWAY_URL)/v1/completions
SLEEP_LEVEL      ?= 1
TEST_PROMPT      ?= Was ist 4 + 3?
TEST_TEMPERATURE ?= 0.7
TEST_MAX_TOKENS  ?= 220
HELM_TIMEOUT     ?= 35m

# CPU companion service names (derived from release name + values.yaml)
EMBEDDINGS_SVC   ?= vllm-baai-bge-large-en-v15-cpu
WHISPER_SVC      ?= vllm-whisper-cpu-service
# Sample audio file used by make test-whisper (override with TEST_AUDIO=/path/to/file.mp3)
TEST_AUDIO       ?= scripts/test-audio.mp3

CURL    ?= curl
JQ      ?= jq
HELM    ?= helm
PYTHON  ?= python3
KUBECTL ?= kubectl

IMAGE_REGISTRY   ?= ghcr.io/thomaswetzler
IMAGE_NAME       ?= sleep-proxy
IMAGE_TAG        ?= latest

# PVC check — keep in sync with helm/models/values.yaml loader.models
PVC_NAME    ?= vllm-model-cache
CHECK_IMG   ?= alpine:3.19
MODEL_DIRS  ?= gemma-3-4b-it llama-3.1-8b-instruct \
               qwen2.5-coder-7b-instruct qwen2.5-14b-instruct

.PHONY: help status engines-status models toggle-model test test-embedding test-whisper \
        deps model-download check-models \
        deploy deploy-vllm deploy-sleep-proxy deploy-litellm deploy-ops-ui deploy-playground \
        undeploy undeploy-vllm undeploy-sleep-proxy undeploy-litellm undeploy-ops-ui undeploy-playground \
        build push

help:
	@printf '%s\n' \
		'' \
		'  vLLM Sleep Proxy — Makefile targets' \
		'  ──────────────────────────────────────────────────────' \
		'  make model-download     Download model weights onto shared PVC (needs HUGGING_FACE_TOKEN)' \
		'  make check-models      Check model files on shared PVC for completeness' \
		'  make deploy             Deploy sleep-proxy + vllm stack + litellm' \
		'  make deploy-sleep-proxy Deploy only the sleep-proxy service' \
		'  make deploy-vllm        Deploy only the vllm stack (router + engine pods)' \
		'  make deploy-litellm     Deploy LiteLLM unified proxy (requires LITELLM_MASTER_KEY)' \
		'  make deploy-ops-ui      Deploy ops-ui monitoring dashboard' \
		'  make deploy-playground  Deploy vLLM Playground browser UI' \
		'  make undeploy           Remove all releases from the cluster' \
		'  make status             Show engine sleep state + available models' \
		'  make engines-status     Show sleep status of all engines via router API' \
		'  make models             List models from gateway incl. node and sleep state' \
		'  make toggle-model       Interactive: choose a model and flip its sleep state' \
		'  make test               Interactive: choose a model and run a completion test' \
		'  make test-embedding     Test TEI embedding service via kubectl port-forward' \
		'  make test-whisper       Test Whisper transcription service via kubectl port-forward' \
		'  make deps               Update Helm chart dependencies' \
		'  make build              Build sleep-proxy Docker image' \
		'  make push               Push sleep-proxy Docker image to registry' \
		''

# ── Status ───────────────────────────────────────────────────────────────────

status: engines-status models

engines-status:
	@for id in $$($(CURL) -s "$(LLM_ROUTER_URL)/engines" | $(JQ) -r '.[].engine_id'); do \
		printf '== %s ==\n' "$$id"; \
		$(CURL) -s "$(LLM_ROUTER_URL)/is_sleeping?id=$$id" | $(JQ); \
	done

models:
	@KUBECONFIG="$(KUBECONFIG)" $(PYTHON) ./scripts/models_status.py \
		--models-url "$(MODELS_URL)" \
		--router-url "$(LLM_ROUTER_URL)" \
		--namespace "$(NAMESPACE)" \
		--kubectl "$(KUBECTL)"

toggle-model:
	@KUBECONFIG="$(KUBECONFIG)" $(PYTHON) ./scripts/models_status.py \
		--toggle \
		--sleep-level "$(SLEEP_LEVEL)" \
		--models-url "$(MODELS_URL)" \
		--router-url "$(LLM_ROUTER_URL)" \
		--namespace "$(NAMESPACE)" \
		--kubectl "$(KUBECTL)"

test:
	@set -eu; \
	response_file=$$(mktemp); models_file=$$(mktemp); completion_file=$$(mktemp); \
	trap 'rm -f "$$response_file" "$$models_file" "$$completion_file"' EXIT HUP INT TERM; \
	$(CURL) -fsS "$(MODELS_URL)" > "$$response_file"; \
	$(JQ) -r '.data[].id' "$$response_file" 2>/dev/null > "$$models_file" || \
		$(JQ) -r '.[]' "$$response_file" > "$$models_file"; \
	count=$$(wc -l < "$$models_file" | tr -d '[:space:]'); \
	[ "$$count" -gt 0 ] || { printf 'No models found.\n' >&2; exit 1; }; \
	printf 'Available models:\n'; nl -w1 -s') ' "$$models_file"; \
	printf 'Select model number: '; read -r sel; \
	model=$$(sed -n "$${sel}p" "$$models_file"); \
	[ -n "$$model" ] || { printf 'Invalid selection.\n' >&2; exit 1; }; \
	printf 'Testing: %s\n' "$$model"; \
	payload=$$($(JQ) -nc \
		--arg model "$$model" \
		--arg prompt "$(TEST_PROMPT)" \
		--argjson temperature "$(TEST_TEMPERATURE)" \
		--argjson max_tokens "$(TEST_MAX_TOKENS)" \
		'{model:$$model,prompt:$$prompt,temperature:$$temperature,max_tokens:$$max_tokens}'); \
	http_code=$$($(CURL) -sS -o "$$completion_file" -w '%{http_code}' "$(COMPLETIONS_URL)" \
		-H 'Content-Type: application/json' -d "$$payload"); \
	case "$$http_code" in 2??) $(JQ) < "$$completion_file" ;; \
		*) printf 'HTTP %s\n' "$$http_code" >&2; $(JQ) < "$$completion_file" >&2; exit 1 ;; esac

test-embedding:
	@printf 'Port-forwarding %s:3000 → localhost:13000 ...\n' "$(EMBEDDINGS_SVC)"; \
	$(KUBECTL) -n "$(NAMESPACE)" port-forward svc/$(EMBEDDINGS_SVC) 13000:3000 >/dev/null 2>&1 & \
	pf_pid=$$!; \
	trap 'kill $$pf_pid 2>/dev/null || true' EXIT HUP INT TERM; \
	sleep 2; \
	printf 'Sending embedding request ...\n'; \
	result=$$($(CURL) -sS http://localhost:13000/embeddings \
		-H "Content-Type: application/json" \
		-d '{"input": "Hello world", "model": "baai-bge-large-en-v1.5"}' \
		| $(JQ) '.data[0].embedding | length'); \
	printf 'Embedding dimension: %s (expected: 1024)\n' "$$result"

test-whisper:
	@test -f "$(TEST_AUDIO)" || { \
		printf 'Audio file not found: %s\nOverride with: make test-whisper TEST_AUDIO=/path/to/file.mp3\n' \
		"$(TEST_AUDIO)" >&2; exit 1; }; \
	printf 'Port-forwarding %s:80 → localhost:18080 ...\n' "$(WHISPER_SVC)"; \
	pkill -f "port-forward.*18080" 2>/dev/null || true; sleep 1; \
	$(KUBECTL) -n "$(NAMESPACE)" port-forward svc/$(WHISPER_SVC) 18080:80 >/dev/null 2>&1 & \
	pf_pid=$$!; \
	trap 'kill $$pf_pid 2>/dev/null || true' EXIT HUP INT TERM; \
	for i in 1 2 3 4 5; do \
		sleep 1; \
		$(CURL) -sf --max-time 2 http://localhost:18080/health >/dev/null 2>&1 && break; \
		printf 'Waiting for port-forward... (%s/5)\n' "$$i"; \
	done; \
	printf 'Sending transcription request (%s) ...\n' "$(TEST_AUDIO)"; \
	$(CURL) -sS --max-time 120 http://localhost:18080/v1/audio/transcriptions \
		-F "file=@$(TEST_AUDIO)" \
		-F "model=Systran/faster-whisper-large-v3" \
		| $(JQ) '.text'

# ── Helm dependencies ─────────────────────────────────────────────────────────

deps:
	@for chart in helm/vllm; do \
		if grep -q '^dependencies:' "$$chart/Chart.yaml" 2>/dev/null; then \
			printf 'Updating dependencies for %s\n' "$$chart"; \
			$(HELM) dependency update "$$chart"; \
		fi; \
	done

# ── Model download ────────────────────────────────────────────────────────────

model-download:
	@test -n "$$HUGGING_FACE_TOKEN" || { \
		printf 'HUGGING_FACE_TOKEN is not set. Export it first:\n  export HUGGING_FACE_TOKEN=hf_...\n' >&2; exit 1; }
	$(HELM) upgrade --install vllm-models ./helm/models \
		-n "$(NAMESPACE)" --create-namespace \
		--wait --wait-for-jobs --timeout "$(HELM_TIMEOUT)" \
		--set-string huggingfaceCredentials.token="$$HUGGING_FACE_TOKEN"

# ── PVC health check ──────────────────────────────────────────────────────────

check-models:
	@NAMESPACE="$(NAMESPACE)" KUBECTL="$(KUBECTL)" \
	 PVC_NAME="$(PVC_NAME)" CHECK_IMG="$(CHECK_IMG)" \
	 MODEL_DIRS="$(MODEL_DIRS)" \
	 $(PYTHON) ./scripts/check_models.py

# ── Deploy ────────────────────────────────────────────────────────────────────

deploy: deploy-sleep-proxy deploy-vllm deploy-litellm deploy-ops-ui deploy-playground

deploy-sleep-proxy:
	$(HELM) upgrade --install sleep-proxy ./helm/sleep-proxy \
		-n "$(NAMESPACE)" --create-namespace --timeout "$(HELM_TIMEOUT)"

deploy-vllm: deps
	$(HELM) upgrade --install vllm ./helm/vllm \
		-f ./helm/vllm/values.yaml \
		-n "$(NAMESPACE)" --create-namespace \
		--set vllm-stack.routerSpec.ingress.hosts[0].host="llm-router.$(EXTERN_DOMAIN)" \
		--timeout "$(HELM_TIMEOUT)"

deploy-litellm:
	@test -n "$$LITELLM_MASTER_KEY" || { \
		printf 'LITELLM_MASTER_KEY is not set. Set it in .env:\n  LITELLM_MASTER_KEY=sk-...\n' >&2; exit 1; }
	@test -n "$(EXTERN_DOMAIN)" || { \
		printf 'EXTERN_DOMAIN is not set. Set it in .env:\n  EXTERN_DOMAIN=k3s.yourdomain.io\n' >&2; exit 1; }
	$(HELM) upgrade --install lite-helm ./helm/litellm \
		-f ./helm/litellm/values.yaml \
		-n "$(NAMESPACE)" --create-namespace \
		--set secret.data.masterKey="$$LITELLM_MASTER_KEY" \
		--set ingress.hosts[0].host="litellm.$(EXTERN_DOMAIN)" \
		--set ingress.tls[0].hosts[0]="litellm.$(EXTERN_DOMAIN)" \
		--timeout "$(HELM_TIMEOUT)"

deploy-ops-ui:
	@test -n "$(EXTERN_DOMAIN)" || { \
		printf 'EXTERN_DOMAIN is not set. Set it in .env:\n  EXTERN_DOMAIN=k3s.yourdomain.io\n' >&2; exit 1; }
	$(HELM) upgrade --install ops-ui ./helm/ops-ui \
		-f ./helm/ops-ui/values.yaml \
		-n "$(NAMESPACE)" --create-namespace \
		--set ingress.hosts[0].host="ops-ui.$(EXTERN_DOMAIN)" \
		--set ingress.tls[0].hosts[0]="ops-ui.$(EXTERN_DOMAIN)" \
		--timeout "$(HELM_TIMEOUT)"

deploy-playground:
	@test -n "$(EXTERN_DOMAIN)" || { \
		printf 'EXTERN_DOMAIN is not set. Set it in .env:\n  EXTERN_DOMAIN=k3s.yourdomain.io\n' >&2; exit 1; }
	$(HELM) upgrade --install playground ./helm/playground \
		-f ./helm/playground/values.yaml \
		-n "$(NAMESPACE)" --create-namespace \
		--set ingress.hosts[0].host="vllm-playground.$(EXTERN_DOMAIN)" \
		--set ingress.tls[0].hosts[0]="vllm-playground.$(EXTERN_DOMAIN)" \
		--timeout "$(HELM_TIMEOUT)"

# ── Undeploy ──────────────────────────────────────────────────────────────────

undeploy: undeploy-litellm undeploy-vllm undeploy-sleep-proxy undeploy-ops-ui undeploy-playground

undeploy-vllm:
	$(HELM) uninstall vllm -n "$(NAMESPACE)"

undeploy-sleep-proxy:
	$(HELM) uninstall sleep-proxy -n "$(NAMESPACE)"

undeploy-litellm:
	$(HELM) uninstall lite-helm -n "$(NAMESPACE)"

undeploy-ops-ui:
	$(HELM) uninstall ops-ui -n "$(NAMESPACE)"

undeploy-playground:
	$(HELM) uninstall playground -n "$(NAMESPACE)"

# ── Build & push sleep-proxy image ───────────────────────────────────────────

build:
	docker build --platform linux/amd64 -t $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) ./src/sleep-proxy

push: build
	docker push $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
