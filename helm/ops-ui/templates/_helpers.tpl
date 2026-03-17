{{- define "ops-ui.name" -}}
{{- default .Chart.Name .Values.service.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ops-ui.fullname" -}}
{{- include "ops-ui.name" . -}}
{{- end -}}

{{- define "ops-ui.labels" -}}
app.kubernetes.io/name: {{ include "ops-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}

{{- define "ops-ui.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ops-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
