{{/*
Expand the chart name.
*/}}
{{- define "llama-cpp-engine.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "llama-cpp-engine.labels" -}}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version }}
app.kubernetes.io/name: {{ include "llama-cpp-engine.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/component: serving-engine
app.kubernetes.io/runtime: llama-cpp
app.kubernetes.io/part-of: vllm-sleep-proxy-stack
{{- end -}}

{{/*
Per-engine selector labels.
*/}}
{{- define "llama-cpp-engine.selectorLabels" -}}
app.kubernetes.io/name: {{ include "llama-cpp-engine.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: serving-engine
app.kubernetes.io/runtime: llama-cpp
llama-cpp-engine/name: {{ .engine.name }}
{{- end -}}

{{/*
Deployment name.
*/}}
{{- define "llama-cpp-engine.deploymentName" -}}
{{- printf "%s-deployment" .engine.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Service name.
*/}}
{{- define "llama-cpp-engine.serviceName" -}}
{{- printf "%s-llama-cpp-service" .engine.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
