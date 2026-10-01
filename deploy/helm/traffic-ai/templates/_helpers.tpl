{{/*
Base chart name, honoring nameOverride.
*/}}
{{- define "traffic-ai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Honors fullnameOverride, then falls back to
<release>-<chart> unless the release name already contains the chart name.
*/}}
{{- define "traffic-ai.fullname" -}}
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
Worker and UI resource names — one Deployment/Service each, suffixed off the
shared fullname so both sides of compose.yaml's topology are addressable.
*/}}
{{- define "traffic-ai.worker.fullname" -}}
{{- printf "%s-worker" (include "traffic-ai.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "traffic-ai.ui.fullname" -}}
{{- printf "%s-ui" (include "traffic-ai.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
The migration hook Job. A fixed name per release on purpose: Helm's
`before-hook-creation` delete policy finds the previous attempt by name.
*/}}
{{- define "traffic-ai.migrate.fullname" -}}
{{- printf "%s-migrate" (include "traffic-ai.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Chart name and version, for the helm.sh/chart label.
*/}}
{{- define "traffic-ai.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common recommended labels, applied to every object this chart renders.
*/}}
{{- define "traffic-ai.labels" -}}
helm.sh/chart: {{ include "traffic-ai.chart" . }}
{{ include "traffic-ai.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels. Deliberately minimal and stable across upgrades — anything
that can change release-to-release (version, component) must never be part
of a selector, or an upgrade orphans the old pods instead of replacing them.
*/}}
{{- define "traffic-ai.selectorLabels" -}}
app.kubernetes.io/name: {{ include "traffic-ai.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Per-component selector labels — same stable base plus the one label that is
allowed to vary (component), since worker and UI are separate Deployments
that must never share a selector.
*/}}
{{- define "traffic-ai.worker.selectorLabels" -}}
{{ include "traffic-ai.selectorLabels" . }}
app.kubernetes.io/component: worker
{{- end -}}

{{- define "traffic-ai.ui.selectorLabels" -}}
{{ include "traffic-ai.selectorLabels" . }}
app.kubernetes.io/component: ui
{{- end -}}

{{/*
ServiceAccount name: honor an explicit override, else derive from the
fullname only when the chart is asked to create one.
*/}}
{{- define "traffic-ai.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "traffic-ai.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Name of the Secret the worker reads TRAFFIC_AI_API_TOKEN / DATABASE_URL
from: an operator-supplied existingSecret takes precedence over the Secret
this chart can generate from values (see values.yaml `auth` block and
templates/secret.yaml).
*/}}
{{- define "traffic-ai.secretName" -}}
{{- default (include "traffic-ai.fullname" .) .Values.auth.existingSecret -}}
{{- end -}}

{{- define "traffic-ai.secretTokenKey" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecretTokenKey -}}
{{- else -}}
api-token
{{- end -}}
{{- end -}}

{{- define "traffic-ai.secretDatabaseUrlKey" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecretDatabaseUrlKey -}}
{{- else -}}
database-url
{{- end -}}
{{- end -}}

{{/*
Fully-qualified image references. An empty tag falls back to the chart's
appVersion, so `helm upgrade` with a new chart picks up the matching image.
*/}}
{{- define "traffic-ai.workerImage" -}}
{{- $tag := default .Chart.AppVersion .Values.image.worker.tag -}}
{{- if .Values.image.registry -}}
{{- printf "%s/%s:%s" (trimSuffix "/" .Values.image.registry) .Values.image.worker.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.worker.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "traffic-ai.uiImage" -}}
{{- $tag := default .Chart.AppVersion .Values.image.ui.tag -}}
{{- if .Values.image.registry -}}
{{- printf "%s/%s:%s" (trimSuffix "/" .Values.image.registry) .Values.image.ui.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.ui.repository $tag -}}
{{- end -}}
{{- end -}}
