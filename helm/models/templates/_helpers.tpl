{{- define "model-loader.fullname" -}}
{{- printf "%s-%s" .Release.Name "models" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "model-loader.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}
