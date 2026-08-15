{{- define "socialnetwork.templates.baseDeployment" }}
apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    service: {{ .Values.name }}
  name: {{ .Values.name }}
spec: 
  replicas: {{ .Values.replicas | default .Values.global.replicas }}
  selector:
    matchLabels:
      service: {{ .Values.name }}
  template:
    metadata:
      labels:
        service: {{ .Values.name }}
        app: {{ .Values.name }}
      {{- /*
        Pod annotations, merged from global and per-service values.

        This exists for the Mazu strategy arms. A Mazu sidecar refuses to
        become Ready ("RBE registration not yet confirmed by Key Curator")
        unless the RBE public params -- and, for the TPM strategies, the TPM
        pubkeys -- are mounted into it via the sidecar.istio.io/userVolume and
        sidecar.istio.io/userVolumeMount annotations. The Bookinfo manifests
        in scratch/yaml/bookinfo-const*.yaml carry these inline on every
        deployment; this chart had nowhere to put them, so its pods hung
        forever under Mazu while the gateway (which gets the volumes from
        istio-operator*.yaml) came up fine.

        See scratch/yaml/sn-mazu-annotations*.yaml for the values to pass.
        Harmless when unset.
      */}}
      {{- $annotations := merge (deepCopy (.Values.podAnnotations | default dict)) (.Values.global.podAnnotations | default dict) }}
      {{- if $annotations }}
      annotations:
        {{- range $k, $v := $annotations }}
        {{ $k }}: {{ $v | quote }}
        {{- end }}
      {{- end }}
    spec:
      {{- if .Values.nodeName}}
      nodeName: {{ .Values.nodeName }}
      {{ end }}
      containers:
      {{- with .Values.container }}
      - name: "{{ .name }}"
        image: {{ .dockerRegistry | default $.Values.global.dockerRegistry }}/{{ .image }}:{{ .imageVersion | default $.Values.global.defaultImageVersion }}
        imagePullPolicy: {{ .imagePullPolicy | default $.Values.global.imagePullPolicy }}
        ports:
        {{- range $cport := .ports }}
        - containerPort: {{ $cport.containerPort -}}
        {{ end }} 
        {{- if .env }}
        env:
        {{- range $e := .env}}
        - name: {{ $e.name }}
          value: "{{ (tpl ($e.value | toString) $) }}"
        {{ end -}}
        {{ end -}}
        {{- if .command}}
        command: 
        - {{ .command }}
        {{- end -}}
        {{- if .args}}
        args:
        {{- range $arg := .args}}
        - {{ $arg }}
        {{- end -}}
        {{- end }}
        {{- if hasKey . "resources" }}  
        resources:
          {{ toYaml .resources | nindent 10 | trim }}
        {{- else if hasKey $.Values.global "resources" }}           
        resources:
          {{ toYaml $.Values.global.resources | nindent 10 | trim }}
        {{- end }}  
        {{- if $.Values.configMaps }}        
        volumeMounts: 
        {{- range $configMap := $.Values.configMaps }}
        - name: {{ $.Values.name }}-config
          mountPath: {{ $configMap.mountPath }}
          subPath: {{ $configMap.name }}
        {{- end }}
        {{- end }}
      {{- end -}}
      {{- if $.Values.configMaps }}
      volumes:
      - name: {{ $.Values.name }}-config
        configMap:
          name: {{ $.Values.name }}
      {{- end }}
      {{- if hasKey .Values "topologySpreadConstraints" }}
      topologySpreadConstraints:
        {{ tpl .Values.topologySpreadConstraints . | nindent 6 | trim }}
      {{- else if hasKey $.Values.global  "topologySpreadConstraints" }}
      topologySpreadConstraints:
        {{ tpl $.Values.global.topologySpreadConstraints . | nindent 6 | trim }}
      {{- end }}
      hostname: {{ $.Values.name }}
      restartPolicy: {{ .Values.restartPolicy | default .Values.global.restartPolicy}}

{{ include "socialnetwork.templates.baseHPA" . }}
{{- end}}
