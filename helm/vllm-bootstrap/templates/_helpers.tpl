{{/*
Expand the chart name.
*/}}
{{- define "vllm-bootstrap.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified release name.
*/}}
{{- define "vllm-bootstrap.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "vllm-bootstrap.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "vllm-bootstrap.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Sanitized run id used to force a fresh Job object on every deploy.
*/}}
{{- define "vllm-bootstrap.runId" -}}
{{- $raw := required "autoSleepHook.runId must be set (for example via --set-string vllm-bootstrap.autoSleepHook.runId=<timestamp>)." (toString .Values.autoSleepHook.runId) -}}
{{- $sanitized := regexReplaceAll "[^a-z0-9-]+" (lower $raw) "-" -}}
{{- trimAll "-" $sanitized -}}
{{- end -}}
