{{/*
Expand the name of the chart.
*/}}
{{- define "oia-agentscope.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "oia-agentscope.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create a default fully qualified api name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
*/}}
{{- define "oia-agentscope.api.fullname" -}}
{{ template "oia-agentscope.fullname" . }}-api
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "oia-agentscope.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "oia-agentscope.labels" -}}
helm.sh/chart: {{ include "oia-agentscope.chart" . }}
{{ include "oia-agentscope.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* labels defiend by user*/}}
{{- define "oia-agentscope.ud.labels" -}}
{{- if .Values.labels }}
{{- toYaml .Values.labels }}
{{- end -}}
{{- end -}}

{{/* annotations defiend by user*/}}
{{- define "oia-agentscope.ud.annotations" -}}
{{- if .Values.annotations }}
{{- toYaml .Values.annotations }}
{{- end -}}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "oia-agentscope.selectorLabels" -}}
app.kubernetes.io/name: {{ include "oia-agentscope.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
持久化卷声明名称：
- 未指定 existingClaim 时用 Chart 生成的名字
- 名称长度受 63 字符限制
*/}}
{{- define "oia-agentscope.api.logsPvcName" -}}
{{- printf "%s" (include "oia-agentscope.api.fullname" . | trunc 58) }}
{{- end -}}

{{- define "oia-agentscope.api.sessionFilesPvcName" -}}
{{- printf "%s-session-files" (include "oia-agentscope.api.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "oia-agentscope.api.externalSkillsPvcName" -}}
{{- printf "%s-external-skills" (include "oia-agentscope.api.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end -}}
