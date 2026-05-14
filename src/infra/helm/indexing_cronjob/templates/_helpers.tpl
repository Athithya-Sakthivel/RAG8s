{{/*
Selector labels used by both the CronJob pods and the NetworkPolicy
*/}}
{{- define "indexing-cronjob.selectorLabels" -}}
{{- toYaml .Values.cronjob.labels }}
{{- end }}