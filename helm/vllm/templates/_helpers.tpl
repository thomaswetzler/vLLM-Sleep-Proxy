{{- define "vllm.embeddings.fullname" -}}
{{- $name := default "embeddings-cpu" .Values.embeddings.name -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vllm.embeddings.pvcName" -}}
{{- printf "%s-pvc" (include "vllm.embeddings.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vllm.embeddings.labels" -}}
app.kubernetes.io/name: {{ default "embeddings-cpu" .Values.embeddings.name }}
app.kubernetes.io/component: embeddings
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
