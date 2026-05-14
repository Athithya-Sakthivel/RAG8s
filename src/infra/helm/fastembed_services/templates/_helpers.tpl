{{/* vim: set filetype=mustache: */}}

{{/*
Expand the name of the chart.
*/}}
{{- define "fastembed.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this.
*/}}
{{- define "fastembed.fullname" -}}
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
Common labels
*/}}
{{- define "fastembed.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{ include "fastembed.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "fastembed.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/instance: {{ .instance | default .name }}
{{- end }}

{{/*
Service account name for a given service
*/}}
{{- define "fastembed.serviceAccountName" -}}
{{- if eq .service "dense" -}}rag-dense-model
{{- else if eq .service "sparse" -}}rag-sparse-model
{{- else if eq .service "reranker" -}}rag-reranker-model
{{- else -}}{{ .service }}-sa
{{- end -}}
{{- end -}}

{{/*
Service name for a given service
*/}}
{{- define "fastembed.serviceName" -}}
{{ .service }}-svc
{{- end -}}

{{/*
Deployment name
*/}}
{{- define "fastembed.deploymentName" -}}
{{ .service }}-deployment
{{- end -}}

{{/*
Role name
*/}}
{{- define "fastembed.roleName" -}}
{{ .service }}-role
{{- end -}}

{{/*
RoleBinding name
*/}}
{{- define "fastembed.roleBindingName" -}}
{{ .service }}-rb
{{- end -}}

{{/*
NetworkPolicy name
*/}}
{{- define "fastembed.networkPolicyName" -}}
{{ .service }}
{{- end -}}

{{/*
Pod labels for a service
*/}}
{{- define "fastembed.podLabels" -}}
{{ include "fastembed.selectorLabels" (dict "name" .name "component" .component "instance" .instance) }}
fastembed.io/service-account: {{ include "fastembed.serviceAccountName" (dict "service" .name) }}
{{- end }}