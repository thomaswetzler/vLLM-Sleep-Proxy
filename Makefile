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
LLM_GATEWAY_URL  ?= https://litellm.$(EXTERN_DOMAIN)
MODELS_URL       ?= $(LLM_GATEWAY_URL)/v1/models?include=node
COMPLETIONS_URL  ?= $(LLM_GATEWAY_URL)/v1/chat/completions
EMBEDDINGS_URL   ?= $(LLM_GATEWAY_URL)/v1/embeddings
TRANSCRIPTIONS_URL ?= $(LLM_GATEWAY_URL)/v1/audio/transcriptions
SLEEP_LEVEL      ?= 1
TEST_PROMPT      ?= Was ist 4 + 3?
TEST_EMBEDDING_INPUT ?= Hello world
TEST_TEMPERATURE ?= 0.7
TEST_MAX_TOKENS  ?= 220
TEST_MODEL       ?=
TEST_VISION_PROMPT ?= What animals are shown in the image? Reply with one short sentence.
TEST_VISION_IMAGE_URL ?= https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/cats.png
TEST_VL_MODEL ?= local/qwen2.5-vl-7b-instruct
TEST_VL_SVC ?= llama-qwen25-vl-7b-instruct-lab01-service
TEST_VL_LOCAL_PORT ?= 18081
TEST_VL_PDF ?= scripts/ESt1A_E10_2025_4.1.1.0_20250925.pdf
TEST_VL_OUTPUT ?= $(patsubst %.pdf,%.md,$(TEST_VL_PDF))
TEST_VL_TEXT_PROMPT ?= What is 2 * 5? Reply with just the result.
TEST_VL_TEXT_MAX_TOKENS ?= 32
TEST_VL_PROMPT ?= Convert this PDF form page into clean Markdown. Preserve headings, section numbers, tables, checkboxes, and empty input fields using Markdown-friendly placeholders like [ ] and __________. Output only Markdown.
TEST_VL_MAX_TOKENS ?= 600
TEST_AWAKE_TIMEOUT ?= 20
TEST_SLEEP_TIMEOUT ?= 90
TEST_SLEEP_POLL_INTERVAL ?= 2
GPU_TEST_LOCAL_PORT ?= 18000
HELM_TIMEOUT     ?= 35m
UNDEPLOY_TIMEOUT ?= 2m
BOOTSTRAP_WAIT_TIMEOUT ?= 90m
INGRESS_CERT_WAIT_SECONDS ?= 900
INGRESS_CERT_MAX_RETRIES ?= 3
CORE_RELEASE     ?= vllm
BOOTSTRAP_RELEASE ?= vllm-bootstrap
BOOTSTRAP_RUN_ID ?= $(shell date +%Y%m%d%H%M%S)
BOOTSTRAP_JOB_NAME ?= $(BOOTSTRAP_RELEASE)-auto-sleep-$(BOOTSTRAP_RUN_ID)
INGRESS_CERTS ?= litellm-tls ops-ui-tls vllm-playground-tls
STACK_CORE_EXTRA_VALUES ?=
STACK_CORE_EXTRA_HELM_ARGS ?=
MODEL_LOADER_EXTRA_VALUES ?=
MODEL_LOADER_EXTRA_HELM_ARGS ?=
MODEL_LOADER_RELEASE ?= vllm-models
MODEL_DIRS_EXTRA ?=

# CPU companion service names (derived from release name + values.yaml)
EMBEDDINGS_SVC   ?= cpu-baai-bge-large-en-v15
WHISPER_SVC      ?= cpu-whisper-service
# Sample audio file used by make test-whisper (override with TEST_AUDIO=/path/to/file.mp3)
TEST_AUDIO       ?= scripts/test-audio.mp3
EMBEDDING_MODEL  ?= BAAI/bge-large-en-v1.5
WHISPER_MODEL    ?= whisper-large-v3

CURL    ?= curl
JQ      ?= jq
HELM    ?= helm
PYTHON  ?= python3
KUBECTL ?= kubectl
ROUTER_SVC ?= vllm-router-service
ROUTER_SVC_PORT ?= 80

IMAGE_REGISTRY   ?= ghcr.io/thomaswetzler
IMAGE_NAME       ?= sleep-proxy
IMAGE_TAG        ?= latest
LLAMA_CPP_ENGINE_IMAGE_NAME ?= llama-cpp-engine

# PVC check — keep in sync with helm/models/values.yaml loader.models
PVC_NAME    ?= vllm-model-cache
CHECK_IMG   ?= alpine:3.19
MODEL_DIRS  ?= baai-bge-large-en-v1.5 gemma-4-12b-it qwen3-14b-fp8 \
               qwen2.5-coder-7b-instruct deepseek-r1-distill-qwen-7b

.PHONY: help status engines-status models toggle-model test test-portforward test-protforward test-litellm test-litellm-all test-embedding test-embedding-litellm test-whisper test-whisper-litellm test-vl test-vl-litellm \
        deps model-download check-models \
        deploy deploy-core deploy-bootstrap deploy-vllm deploy-sleep-proxy deploy-litellm deploy-ops-ui deploy-playground \
        undeploy undeploy-core undeploy-bootstrap undeploy-vllm undeploy-sleep-proxy undeploy-litellm undeploy-ops-ui undeploy-playground \
        build push build-llama-cpp-engine push-llama-cpp-engine

help:
	@printf '%s\n' \
		'' \
		'  vLLM Sleep Proxy — Makefile targets' \
		'  ──────────────────────────────────────────────────────' \
		'  make model-download     Download model weights onto shared PVC (needs HUGGING_FACE_TOKEN)' \
		'  make check-models      Check model files on shared PVC for completeness' \
		'  make deploy             Deploy runtime umbrella + bootstrap umbrella' \
		'  make deploy-core        Deploy runtime umbrella chart (router, proxy, gateway, UIs)' \
		'  make deploy-bootstrap   Run bootstrap umbrella chart and wait for auto-sleep completion' \
		'  make deploy-vllm        Legacy alias for make deploy-core' \
		'  make deploy-sleep-proxy Direct chart deploy: only the sleep-proxy service' \
		'  make deploy-litellm     Direct chart deploy: only LiteLLM unified proxy' \
		'  make deploy-ops-ui      Direct chart deploy: only ops-ui monitoring dashboard' \
		'  make deploy-playground  Direct chart deploy: only vLLM Playground browser UI' \
		'  make undeploy           Remove all releases from the cluster' \
		'  make status             Show engine sleep state + available models' \
		'  make engines-status     Show sleep status of all engines via router API' \
		'  make models             List models from gateway incl. node and sleep state' \
		'  make toggle-model       Interactive: choose a model and flip its sleep state' \
		'  make test-portforward   Interactive: choose a model and test it via direct kubectl port-forward' \
		'  make test-protforward   Alias for make test-portforward' \
		'  make test-litellm      Interactive by default; use TEST_MODEL=<name> or TEST_MODEL=all for automated LiteLLM tests' \
		'  make test-litellm-all  Run the automated LiteLLM smoke test across all configured local/CPU models' \
		'  make test-embedding     Test TEI embedding service via kubectl port-forward' \
		'  make test-embedding-litellm Test embeddings via LiteLLM ingress' \
		'  make test-whisper       Test Whisper transcription service via kubectl port-forward' \
		'  make test-whisper-litellm Test Whisper transcription via LiteLLM ingress' \
		'  make test-vl            Convert a local PDF form into Markdown via the VLM model' \
		'  make test-vl-litellm    Convert a local PDF form into Markdown via LiteLLM + the VLM model' \
		'  make deps               Update Helm chart dependencies' \
		'  make build              Build sleep-proxy Docker image' \
		'  make push               Push sleep-proxy Docker image to registry' \
		'  make build-llama-cpp-engine Build llama-cpp-engine Docker image' \
		'  make push-llama-cpp-engine  Push llama-cpp-engine Docker image to registry' \
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
	@printf 'make test is deprecated; use make test-portforward\n' >&2
	@$(MAKE) --no-print-directory test-portforward

test-protforward: test-portforward

test-portforward:
	@set -eu; \
	test -n "$$LITELLM_MASTER_KEY" || { \
		printf 'LITELLM_MASTER_KEY is not set. Set it in .env:\n  LITELLM_MASTER_KEY=sk-...\n' >&2; exit 1; }; \
	response_file=$$(mktemp); models_file=$$(mktemp); completion_file=$$(mktemp); \
	trap 'rm -f "$$response_file" "$$models_file" "$$completion_file"' EXIT HUP INT TERM; \
	$(CURL) -fsS -H "Authorization: Bearer $$LITELLM_MASTER_KEY" "$(MODELS_URL)" > "$$response_file"; \
	$(JQ) -r '.data[].id' "$$response_file" 2>/dev/null > "$$models_file" || \
		$(JQ) -r '.[]' "$$response_file" > "$$models_file"; \
	count=$$(wc -l < "$$models_file" | tr -d '[:space:]'); \
	[ "$$count" -gt 0 ] || { printf 'No models found.\n' >&2; exit 1; }; \
	printf 'Available models:\n'; nl -w1 -s') ' "$$models_file"; \
	printf 'Select model number: '; read -r sel; \
	model=$$(sed -n "$${sel}p" "$$models_file"); \
	[ -n "$$model" ] || { printf 'Invalid selection.\n' >&2; exit 1; }; \
	printf 'Testing via direct port-forward: %s\n' "$$model"; \
	case "$$model" in \
		"$(EMBEDDING_MODEL)") \
			$(MAKE) --no-print-directory test-embedding TEST_EMBEDDING_INPUT="$(TEST_EMBEDDING_INPUT)" ;; \
		"$(WHISPER_MODEL)") \
			$(MAKE) --no-print-directory test-whisper TEST_AUDIO="$(TEST_AUDIO)" ;; \
		*) \
			pod_name=$$(KUBECONFIG="$(KUBECONFIG)" $(PYTHON) ./scripts/models_status.py \
				--namespace "$(NAMESPACE)" \
				--kubectl "$(KUBECTL)" \
				--pod-for-model "$$model"); \
			[ -n "$$pod_name" ] || { printf 'No pod found for model: %s\n' "$$model" >&2; exit 1; }; \
			printf 'Port-forwarding pod/%s:8000 -> localhost:%s ...\n' "$$pod_name" "$(GPU_TEST_LOCAL_PORT)"; \
			$(KUBECTL) -n "$(NAMESPACE)" port-forward "pod/$$pod_name" "$(GPU_TEST_LOCAL_PORT):8000" >/dev/null 2>&1 & \
			pf_pid=$$!; \
			trap 'kill $$pf_pid 2>/dev/null || true; rm -f "$$response_file" "$$models_file" "$$completion_file"' EXIT HUP INT TERM; \
			for i in 1 2 3 4 5; do \
				sleep 1; \
				$(CURL) -sf --max-time 2 "http://localhost:$(GPU_TEST_LOCAL_PORT)/health" >/dev/null 2>&1 && break; \
				printf 'Waiting for GPU model port-forward... (%s/5)\n' "$$i"; \
			done; \
			payload=$$($(JQ) -nc \
				--arg model "$$model" \
				--arg prompt "$(TEST_PROMPT)" \
				--argjson temperature "$(TEST_TEMPERATURE)" \
				--argjson max_tokens "$(TEST_MAX_TOKENS)" \
				'{model:$$model,messages:[{role:"user",content:$$prompt}],temperature:$$temperature,max_tokens:$$max_tokens}'); \
			http_code=$$($(CURL) -sS -o "$$completion_file" -w '%{http_code}' "http://localhost:$(GPU_TEST_LOCAL_PORT)/v1/chat/completions" \
				-H 'Content-Type: application/json' -d "$$payload"); \
			case "$$http_code" in 2??) $(JQ) < "$$completion_file" ;; \
				*) printf 'HTTP %s\n' "$$http_code" >&2; $(JQ) < "$$completion_file" >&2; exit 1 ;; esac ;; \
	esac

test-litellm:
	@set -eu; \
	test -n "$$LITELLM_MASTER_KEY" || { \
		printf 'LITELLM_MASTER_KEY is not set. Set it in .env:\n  LITELLM_MASTER_KEY=sk-...\n' >&2; exit 1; }; \
	if [ -n "$(TEST_MODEL)" ]; then \
		$(PYTHON) ./scripts/test_litellm_models.py \
			--models-url "$(MODELS_URL)" \
			--completions-url "$(COMPLETIONS_URL)" \
			--embeddings-url "$(EMBEDDINGS_URL)" \
			--transcriptions-url "$(TRANSCRIPTIONS_URL)" \
			--api-key "$$LITELLM_MASTER_KEY" \
			--namespace "$(NAMESPACE)" \
			--kubectl "$(KUBECTL)" \
			--helm "$(HELM)" \
			--core-release "$(CORE_RELEASE)" \
			--router-service "$(ROUTER_SVC)" \
			--router-port "$(ROUTER_SVC_PORT)" \
			--test-audio "$(TEST_AUDIO)" \
			--test-model "$(TEST_MODEL)" \
			--text-prompt "$(TEST_PROMPT)" \
			--vision-prompt "$(TEST_VISION_PROMPT)" \
			--vision-image-url "$(TEST_VISION_IMAGE_URL)" \
			--max-tokens "$(TEST_MAX_TOKENS)" \
			--sleep-level "$(SLEEP_LEVEL)" \
			--awake-timeout "$(TEST_AWAKE_TIMEOUT)" \
			--sleep-timeout "$(TEST_SLEEP_TIMEOUT)" \
			--sleep-poll-interval "$(TEST_SLEEP_POLL_INTERVAL)"; \
		exit $$?; \
	fi; \
	response_file=$$(mktemp); models_file=$$(mktemp); completion_file=$$(mktemp); \
	trap 'rm -f "$$response_file" "$$models_file" "$$completion_file"' EXIT HUP INT TERM; \
	$(CURL) -fsS -H "Authorization: Bearer $$LITELLM_MASTER_KEY" "$(MODELS_URL)" > "$$response_file"; \
	$(JQ) -r '.data[].id' "$$response_file" 2>/dev/null > "$$models_file" || \
		$(JQ) -r '.[]' "$$response_file" > "$$models_file"; \
	count=$$(wc -l < "$$models_file" | tr -d '[:space:]'); \
	[ "$$count" -gt 0 ] || { printf 'No models found.\n' >&2; exit 1; }; \
	printf 'Available models:\n'; nl -w1 -s') ' "$$models_file"; \
	printf 'Select model number: '; read -r sel; \
	model=$$(sed -n "$${sel}p" "$$models_file"); \
	[ -n "$$model" ] || { printf 'Invalid selection.\n' >&2; exit 1; }; \
	printf 'Testing via LiteLLM ingress: %s\n' "$$model"; \
	$(PYTHON) ./scripts/test_litellm_models.py \
		--models-url "$(MODELS_URL)" \
		--completions-url "$(COMPLETIONS_URL)" \
		--embeddings-url "$(EMBEDDINGS_URL)" \
		--transcriptions-url "$(TRANSCRIPTIONS_URL)" \
		--api-key "$$LITELLM_MASTER_KEY" \
		--namespace "$(NAMESPACE)" \
		--kubectl "$(KUBECTL)" \
		--helm "$(HELM)" \
		--core-release "$(CORE_RELEASE)" \
		--router-service "$(ROUTER_SVC)" \
		--router-port "$(ROUTER_SVC_PORT)" \
		--test-audio "$(TEST_AUDIO)" \
		--test-model "$$model" \
		--text-prompt "$(TEST_PROMPT)" \
		--vision-prompt "$(TEST_VISION_PROMPT)" \
		--vision-image-url "$(TEST_VISION_IMAGE_URL)" \
		--max-tokens "$(TEST_MAX_TOKENS)" \
		--sleep-level "$(SLEEP_LEVEL)" \
		--awake-timeout "$(TEST_AWAKE_TIMEOUT)" \
		--sleep-timeout "$(TEST_SLEEP_TIMEOUT)" \
		--sleep-poll-interval "$(TEST_SLEEP_POLL_INTERVAL)"

test-litellm-all:
	@$(MAKE) --no-print-directory test-litellm TEST_MODEL=all

test-embedding-litellm:
	@set -eu; \
	test -n "$$LITELLM_MASTER_KEY" || { \
		printf 'LITELLM_MASTER_KEY is not set. Set it in .env:\n  LITELLM_MASTER_KEY=sk-...\n' >&2; exit 1; }; \
	printf 'Sending embedding request via LiteLLM ingress ...\n'; \
	result=$$($(CURL) -sS "$(EMBEDDINGS_URL)" \
		-H "Content-Type: application/json" \
		-H "Authorization: Bearer $$LITELLM_MASTER_KEY" \
		-d '{"input": "$(TEST_EMBEDDING_INPUT)", "model": "$(EMBEDDING_MODEL)"}' \
		| $(JQ) '.data[0].embedding | length'); \
	printf 'Embedding dimension via LiteLLM: %s (expected: 1024)\n' "$$result"

test-embedding:
	@printf 'Port-forwarding %s:3000 → localhost:13000 ...\n' "$(EMBEDDINGS_SVC)"; \
	$(KUBECTL) -n "$(NAMESPACE)" port-forward svc/$(EMBEDDINGS_SVC) 13000:3000 >/dev/null 2>&1 & \
	pf_pid=$$!; \
	trap 'kill $$pf_pid 2>/dev/null || true' EXIT HUP INT TERM; \
	sleep 2; \
	printf 'Sending embedding request ...\n'; \
	result=$$($(CURL) -sS http://localhost:13000/embeddings \
		-H "Content-Type: application/json" \
		-d '{"input": "$(TEST_EMBEDDING_INPUT)", "model": "baai-bge-large-en-v1.5"}' \
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

test-whisper-litellm:
	@set -eu; \
	test -n "$$LITELLM_MASTER_KEY" || { \
		printf 'LITELLM_MASTER_KEY is not set. Set it in .env:\n  LITELLM_MASTER_KEY=sk-...\n' >&2; exit 1; }; \
	test -f "$(TEST_AUDIO)" || { \
		printf 'Audio file not found: %s\nOverride with: make test-whisper-litellm TEST_AUDIO=/path/to/file.mp3\n' \
		"$(TEST_AUDIO)" >&2; exit 1; }; \
	printf 'Sending transcription request via LiteLLM ingress (%s) ...\n' "$(TEST_AUDIO)"; \
	$(CURL) -sS --max-time 120 "$(TRANSCRIPTIONS_URL)" \
		-H "Authorization: Bearer $$LITELLM_MASTER_KEY" \
		-F "file=@$(TEST_AUDIO)" \
		-F "model=$(WHISPER_MODEL)" \
		| $(JQ) '.text'

test-vl:
	@set -eu; \
	test -f "$(TEST_VL_PDF)" || { \
		printf 'PDF file not found: %s\nOverride with: make test-vl TEST_VL_PDF=/path/to/file.pdf\n' \
		"$(TEST_VL_PDF)" >&2; exit 1; }; \
	printf 'Port-forwarding %s:8080 → localhost:%s ...\n' "$(TEST_VL_SVC)" "$(TEST_VL_LOCAL_PORT)"; \
	pkill -f "port-forward.*$(TEST_VL_LOCAL_PORT)" 2>/dev/null || true; sleep 1; \
	$(KUBECTL) -n "$(NAMESPACE)" port-forward svc/$(TEST_VL_SVC) $(TEST_VL_LOCAL_PORT):8080 >/dev/null 2>&1 & \
	pf_pid=$$!; \
	trap 'kill $$pf_pid 2>/dev/null || true' EXIT HUP INT TERM; \
	for i in 1 2 3 4 5; do \
		sleep 1; \
		$(CURL) -sf --max-time 2 http://localhost:$(TEST_VL_LOCAL_PORT)/health >/dev/null 2>&1 && break; \
		printf 'Waiting for port-forward... (%s/5)\n' "$$i"; \
	done; \
	printf 'Converting %s to Markdown via %s ...\n' "$(TEST_VL_PDF)" "$(TEST_VL_MODEL)"; \
	$(PYTHON) ./scripts/test_vl_pdf.py \
		--base-url "http://localhost:$(TEST_VL_LOCAL_PORT)" \
		--model "$(TEST_VL_MODEL)" \
		--pdf-path "$(TEST_VL_PDF)" \
		--output-path "$(TEST_VL_OUTPUT)" \
		--text-prompt "$(TEST_VL_TEXT_PROMPT)" \
		--text-max-tokens "$(TEST_VL_TEXT_MAX_TOKENS)" \
		--prompt "$(TEST_VL_PROMPT)" \
		--max-tokens "$(TEST_VL_MAX_TOKENS)"

test-vl-litellm:
	@set -eu; \
	test -n "$$LITELLM_MASTER_KEY" || { \
		printf 'LITELLM_MASTER_KEY is not set. Set it in .env:\n  LITELLM_MASTER_KEY=sk-...\n' >&2; exit 1; }; \
	test -f "$(TEST_VL_PDF)" || { \
		printf 'PDF file not found: %s\nOverride with: make test-vl-litellm TEST_VL_PDF=/path/to/file.pdf\n' \
		"$(TEST_VL_PDF)" >&2; exit 1; }; \
	printf 'Converting %s to Markdown via LiteLLM model %s ...\n' "$(TEST_VL_PDF)" "$(TEST_VL_MODEL)"; \
	$(PYTHON) ./scripts/test_vl_pdf.py \
		--completions-url "$(COMPLETIONS_URL)" \
		--api-key "$$LITELLM_MASTER_KEY" \
		--model "$(TEST_VL_MODEL)" \
		--pdf-path "$(TEST_VL_PDF)" \
		--output-path "$(TEST_VL_OUTPUT)" \
		--text-prompt "$(TEST_VL_TEXT_PROMPT)" \
		--text-max-tokens "$(TEST_VL_TEXT_MAX_TOKENS)" \
		--prompt "$(TEST_VL_PROMPT)" \
		--max-tokens "$(TEST_VL_MAX_TOKENS)"

# ── Helm dependencies ─────────────────────────────────────────────────────────

deps:
	@for chart in helm/vllm helm/stack-core helm/stack-bootstrap; do \
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
		-f ./helm/models/values.yaml \
		$(foreach values_file,$(MODEL_LOADER_EXTRA_VALUES),-f $(values_file)) \
		-n "$(NAMESPACE)" --create-namespace \
		--wait --wait-for-jobs --timeout "$(HELM_TIMEOUT)" \
		$(MODEL_LOADER_EXTRA_HELM_ARGS) \
		--set-string huggingfaceCredentials.token="$$HUGGING_FACE_TOKEN"

# ── PVC health check ──────────────────────────────────────────────────────────

check-models:
	@model_dirs="$(MODEL_DIRS)"; \
	if $(HELM) status "$(MODEL_LOADER_RELEASE)" -n "$(NAMESPACE)" >/dev/null 2>&1; then \
		release_dirs="$$($(HELM) get values "$(MODEL_LOADER_RELEASE)" -n "$(NAMESPACE)" -o json | $(JQ) -r '[ (.loader.models // [])[], (.loader.extraModels // [])[] ] | map(select(.enabled != false) | .path) | .[]' | tr '\n' ' ')"; \
		if [ -n "$$release_dirs" ]; then \
			model_dirs="$$release_dirs"; \
		fi; \
	fi; \
	NAMESPACE="$(NAMESPACE)" KUBECTL="$(KUBECTL)" \
	PVC_NAME="$(PVC_NAME)" CHECK_IMG="$(CHECK_IMG)" \
	MODEL_DIRS="$$model_dirs $(MODEL_DIRS_EXTRA)" \
	$(PYTHON) ./scripts/check_models.py

# ── Deploy ────────────────────────────────────────────────────────────────────

deploy: deploy-core deploy-bootstrap

deploy-core: deps
	@test -n "$$LITELLM_MASTER_KEY" || { \
		printf 'LITELLM_MASTER_KEY is not set. Set it in .env:\n  LITELLM_MASTER_KEY=sk-...\n' >&2; exit 1; }
	@test -n "$(EXTERN_DOMAIN)" || { \
		printf 'EXTERN_DOMAIN is not set. Set it in .env:\n  EXTERN_DOMAIN=k3s.yourdomain.io\n' >&2; exit 1; }
	$(HELM) upgrade --install "$(CORE_RELEASE)" ./helm/stack-core \
		-f ./helm/stack-core/values.yaml \
		$(foreach values_file,$(STACK_CORE_EXTRA_VALUES),-f $(values_file)) \
		-n "$(NAMESPACE)" --create-namespace \
		$(STACK_CORE_EXTRA_HELM_ARGS) \
		--set-string vllm.vllm-stack.routerSpec.ingress.hosts[0].host="llm-router.$(EXTERN_DOMAIN)" \
		--set-string vllm.vllm-stack.routerSpec.ingress.hosts[0].paths[0].path="/" \
		--set-string vllm.vllm-stack.routerSpec.ingress.hosts[0].paths[0].pathType="Prefix" \
		--set-string litellm.secret.data.masterKey="$$LITELLM_MASTER_KEY" \
		--set-string litellm.ingress.hosts[0].host="litellm.$(EXTERN_DOMAIN)" \
		--set-string litellm.ingress.hosts[0].paths[0].path="/" \
		--set-string litellm.ingress.hosts[0].paths[0].pathType="Prefix" \
		--set-string litellm.ingress.tls[0].secretName="litellm-tls" \
		--set-string litellm.ingress.tls[0].hosts[0]="litellm.$(EXTERN_DOMAIN)" \
		--set-string ops-ui.ingress.hosts[0].host="ops-ui.$(EXTERN_DOMAIN)" \
		--set-string ops-ui.ingress.hosts[0].paths[0].path="/" \
		--set-string ops-ui.ingress.hosts[0].paths[0].pathType="Prefix" \
		--set-string ops-ui.ingress.tls[0].secretName="ops-ui-tls" \
		--set-string ops-ui.ingress.tls[0].hosts[0]="ops-ui.$(EXTERN_DOMAIN)" \
		--set-string playground.ingress.hosts[0].host="vllm-playground.$(EXTERN_DOMAIN)" \
		--set-string playground.ingress.hosts[0].paths[0].path="/" \
		--set-string playground.ingress.hosts[0].paths[0].pathType="Prefix" \
		--set-string playground.ingress.tls[0].secretName="vllm-playground-tls" \
		--set-string playground.ingress.tls[0].hosts[0]="vllm-playground.$(EXTERN_DOMAIN)" \
		--timeout "$(HELM_TIMEOUT)"
	@KUBECTL_BIN="$(KUBECTL)" ./scripts/wait_for_ingress_certs.sh \
		"$(NAMESPACE)" "$(INGRESS_CERT_WAIT_SECONDS)" "$(INGRESS_CERT_MAX_RETRIES)" \
		$(INGRESS_CERTS)

deploy-bootstrap: deps
	$(HELM) upgrade --install "$(BOOTSTRAP_RELEASE)" ./helm/stack-bootstrap \
		-f ./helm/stack-bootstrap/values.yaml \
		-n "$(NAMESPACE)" --create-namespace \
		--set-string vllm-bootstrap.autoSleepHook.runId="$(BOOTSTRAP_RUN_ID)" \
		--timeout "$(HELM_TIMEOUT)"
	@printf 'Waiting for bootstrap job %s ...\n' "$(BOOTSTRAP_JOB_NAME)"; \
	$(KUBECTL) wait -n "$(NAMESPACE)" --for=create job/$(BOOTSTRAP_JOB_NAME) --timeout=120s; \
	if ! $(KUBECTL) wait -n "$(NAMESPACE)" --for=condition=complete job/$(BOOTSTRAP_JOB_NAME) --timeout="$(BOOTSTRAP_WAIT_TIMEOUT)"; then \
		printf '\nBootstrap job did not complete successfully.\n' >&2; \
		$(KUBECTL) describe job "$(BOOTSTRAP_JOB_NAME)" -n "$(NAMESPACE)" >&2 || true; \
		printf '\nBootstrap logs:\n' >&2; \
		$(KUBECTL) logs job/$(BOOTSTRAP_JOB_NAME) -n "$(NAMESPACE)" --all-containers=true >&2 || true; \
		exit 1; \
	fi

deploy-vllm: deploy-core

deploy-sleep-proxy:
	$(HELM) upgrade --install sleep-proxy ./helm/sleep-proxy \
		-n "$(NAMESPACE)" --create-namespace --timeout "$(HELM_TIMEOUT)"

deploy-litellm:
	@test -n "$$LITELLM_MASTER_KEY" || { \
		printf 'LITELLM_MASTER_KEY is not set. Set it in .env:\n  LITELLM_MASTER_KEY=sk-...\n' >&2; exit 1; }
	@test -n "$(EXTERN_DOMAIN)" || { \
		printf 'EXTERN_DOMAIN is not set. Set it in .env:\n  EXTERN_DOMAIN=k3s.yourdomain.io\n' >&2; exit 1; }
	$(HELM) upgrade --install lite-helm ./helm/litellm \
		-f ./helm/litellm/values.yaml \
		-n "$(NAMESPACE)" --create-namespace \
		--set-string secret.data.masterKey="$$LITELLM_MASTER_KEY" \
		--set-string ingress.hosts[0].host="litellm.$(EXTERN_DOMAIN)" \
		--set-string ingress.hosts[0].paths[0].path="/" \
		--set-string ingress.hosts[0].paths[0].pathType="Prefix" \
		--set-string ingress.tls[0].secretName="litellm-tls" \
		--set-string ingress.tls[0].hosts[0]="litellm.$(EXTERN_DOMAIN)" \
		--timeout "$(HELM_TIMEOUT)"
	@KUBECTL_BIN="$(KUBECTL)" ./scripts/wait_for_ingress_certs.sh \
		"$(NAMESPACE)" "$(INGRESS_CERT_WAIT_SECONDS)" "$(INGRESS_CERT_MAX_RETRIES)" \
		litellm-tls

deploy-ops-ui:
	@test -n "$(EXTERN_DOMAIN)" || { \
		printf 'EXTERN_DOMAIN is not set. Set it in .env:\n  EXTERN_DOMAIN=k3s.yourdomain.io\n' >&2; exit 1; }
	$(HELM) upgrade --install ops-ui ./helm/ops-ui \
		-f ./helm/ops-ui/values.yaml \
		-n "$(NAMESPACE)" --create-namespace \
		--set-string ingress.hosts[0].host="ops-ui.$(EXTERN_DOMAIN)" \
		--set-string ingress.hosts[0].paths[0].path="/" \
		--set-string ingress.hosts[0].paths[0].pathType="Prefix" \
		--set-string ingress.tls[0].secretName="ops-ui-tls" \
		--set-string ingress.tls[0].hosts[0]="ops-ui.$(EXTERN_DOMAIN)" \
		--timeout "$(HELM_TIMEOUT)"
	@KUBECTL_BIN="$(KUBECTL)" ./scripts/wait_for_ingress_certs.sh \
		"$(NAMESPACE)" "$(INGRESS_CERT_WAIT_SECONDS)" "$(INGRESS_CERT_MAX_RETRIES)" \
		ops-ui-tls

deploy-playground:
	@test -n "$(EXTERN_DOMAIN)" || { \
		printf 'EXTERN_DOMAIN is not set. Set it in .env:\n  EXTERN_DOMAIN=k3s.yourdomain.io\n' >&2; exit 1; }
	$(HELM) upgrade --install playground ./helm/playground \
		-f ./helm/playground/values.yaml \
		-n "$(NAMESPACE)" --create-namespace \
		--set-string ingress.hosts[0].host="vllm-playground.$(EXTERN_DOMAIN)" \
		--set-string ingress.hosts[0].paths[0].path="/" \
		--set-string ingress.hosts[0].paths[0].pathType="Prefix" \
		--set-string ingress.tls[0].secretName="vllm-playground-tls" \
		--set-string ingress.tls[0].hosts[0]="vllm-playground.$(EXTERN_DOMAIN)" \
		--timeout "$(HELM_TIMEOUT)"
	@KUBECTL_BIN="$(KUBECTL)" ./scripts/wait_for_ingress_certs.sh \
		"$(NAMESPACE)" "$(INGRESS_CERT_WAIT_SECONDS)" "$(INGRESS_CERT_MAX_RETRIES)" \
		vllm-playground-tls

# ── Undeploy ──────────────────────────────────────────────────────────────────

undeploy: undeploy-bootstrap undeploy-core

undeploy-core:
	@set -eu; \
	if ! $(HELM) uninstall --ignore-not-found --wait --timeout "$(UNDEPLOY_TIMEOUT)" "$(CORE_RELEASE)" -n "$(NAMESPACE)"; then \
		printf 'Helm uninstall for %s timed out; checking for orphaned pods ...\n' "$(CORE_RELEASE)" >&2; \
	fi; \
	orphan_pods=$$($(KUBECTL) -n "$(NAMESPACE)" get pods -l app.kubernetes.io/instance="$(CORE_RELEASE)" -o name 2>/dev/null || true); \
	if [ -n "$$orphan_pods" ]; then \
		printf 'Force deleting orphaned core pods:\n%s\n' "$$orphan_pods"; \
		$(KUBECTL) -n "$(NAMESPACE)" delete $$orphan_pods --force --grace-period=0 --ignore-not-found >/dev/null || true; \
	fi

undeploy-bootstrap:
	@set -eu; \
	if ! $(HELM) uninstall --ignore-not-found --wait --timeout "$(UNDEPLOY_TIMEOUT)" "$(BOOTSTRAP_RELEASE)" -n "$(NAMESPACE)"; then \
		printf 'Helm uninstall for %s timed out; checking for orphaned bootstrap jobs ...\n' "$(BOOTSTRAP_RELEASE)" >&2; \
	fi; \
	orphan_jobs=$$($(KUBECTL) -n "$(NAMESPACE)" get jobs -l app.kubernetes.io/instance="$(BOOTSTRAP_RELEASE)" -o name 2>/dev/null || true); \
	if [ -n "$$orphan_jobs" ]; then \
		printf 'Deleting orphaned bootstrap jobs:\n%s\n' "$$orphan_jobs"; \
		$(KUBECTL) -n "$(NAMESPACE)" delete $$orphan_jobs --ignore-not-found >/dev/null || true; \
	fi; \
	orphan_pods=$$($(KUBECTL) -n "$(NAMESPACE)" get pods -l app.kubernetes.io/instance="$(BOOTSTRAP_RELEASE)" -o name 2>/dev/null || true); \
	if [ -n "$$orphan_pods" ]; then \
		printf 'Force deleting orphaned bootstrap pods:\n%s\n' "$$orphan_pods"; \
		$(KUBECTL) -n "$(NAMESPACE)" delete $$orphan_pods --force --grace-period=0 --ignore-not-found >/dev/null || true; \
	fi

undeploy-vllm:
	$(HELM) uninstall --ignore-not-found "$(CORE_RELEASE)" -n "$(NAMESPACE)"

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

build-llama-cpp-engine:
	docker build --platform linux/amd64 -t $(IMAGE_REGISTRY)/$(LLAMA_CPP_ENGINE_IMAGE_NAME):$(IMAGE_TAG) ./src/llama-cpp-engine

push-llama-cpp-engine: build-llama-cpp-engine
	docker push $(IMAGE_REGISTRY)/$(LLAMA_CPP_ENGINE_IMAGE_NAME):$(IMAGE_TAG)
