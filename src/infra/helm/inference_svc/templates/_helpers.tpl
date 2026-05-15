{{/*
Expand the name of the chart.
*/}}
{{- define "inference-services.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Component selector labels – only the labels needed for selectors.
*/}}
{{- define "inference-services.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .component }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Common labels (merged into pod template only, not selector).
*/}}
{{- define "inference-services.commonLabels" -}}
helm.sh/chart: {{ include "inference-services.name" . }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end }}