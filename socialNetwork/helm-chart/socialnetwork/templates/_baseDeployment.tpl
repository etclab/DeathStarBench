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
      {{- /*
        Gate startup on Redis Cluster formation.

        WHY: with global.redis.cluster.enabled, the services that construct a
        redis-plus-plus RedisCluster client do so EAGERLY at startup. If the
        cluster has not finished forming, CLUSTER SLOTS comes back empty and
        the client throws:

            terminate called after throwing an instance of 'sw::redis::Error'
              what():  Empty slots

        which aborts the process (exit 139). Observed on 2026-08-27: all four
        home-timeline-service pods restarted 4x each before the cluster came
        up. It self-heals, but it burns install time and CrashLoopBackOff is
        exponential, so a slow cluster formation can stall wait_ready.

        WHY POLL THE K8S API AND NOT REDIS: mtls.yaml applies a mesh-wide
        STRICT PeerAuthentication. istio-init is APPENDED after user init
        containers, so this container runs with no sidecar on an unmodified
        network path -- it therefore cannot complete an mTLS handshake with
        the redis-cluster pods and cannot speak Redis at all. The Kubernetes
        API server is not in the mesh, so it IS reachable, and
        redis-cluster-readiness-hook reaching Succeeded is the authoritative
        "every node reports cluster_state: ok" signal (see
        templates/hooks/redis-cluster/).

        NOTE: this is a LIST with a fieldSelector, not a GET on the named pod.
        scratch/yaml/mcrouter-role.yaml grants the default ServiceAccount
        pods:["list"] only -- `kubectl auth can-i get pods` returns "no" -- so
        a GET would 403, parse no phase, and silently burn the whole deadline
        before starting anyway. Do not "simplify" this back to a GET without
        also adding "get" to that Role.

        DO NOT ADD `helm --wait`: without it Helm applies the manifests and
        then runs post-install hooks, so the hook makes progress while these
        pods wait -- no deadlock. With --wait, Helm would block on these pods
        BEFORE running the hook they are waiting for, and the install would
        hang until the timeout.

        Falls through with a warning after the deadline rather than failing,
        so the worst case is the old crash-and-restart behaviour, not a
        permanently stuck pod.
      */}}
      {{- if and .Values.global.redis.cluster.enabled .Values.waitForRedisCluster }}
      initContainers:
      - name: wait-for-redis-cluster
        image: curlimages/curl:8.11.0
        command:
        - sh
        - -c
        - |
          set -u
          API=https://kubernetes.default.svc
          SA=/var/run/secrets/kubernetes.io/serviceaccount
          NS=$(cat $SA/namespace)
          HOOK=redis-cluster-readiness-hook
          DEADLINE=$(( $(date +%s) + {{ .Values.global.redisClusterWaitSeconds | default 900 }} ))
          echo "waiting for $HOOK to reach Succeeded in ns/$NS"
          while true; do
            PHASE=$(curl -sS --cacert $SA/ca.crt \
              -H "Authorization: Bearer $(cat $SA/token)" \
              "$API/api/v1/namespaces/$NS/pods?fieldSelector=metadata.name=$HOOK" 2>/dev/null \
              | tr -d ' \n' | grep -o '"phase":"[A-Za-z]*"' | head -1 | cut -d'"' -f4)
            if [ "${PHASE:-}" = "Succeeded" ]; then
              echo "redis cluster ready ($HOOK=Succeeded)"; exit 0
            fi
            if [ "$(date +%s)" -ge "$DEADLINE" ]; then
              echo "WARNING: timed out waiting for $HOOK (last phase='${PHASE:-<absent>}')"
              echo "WARNING: starting anyway -- the app may crash-loop until the cluster forms"
              exit 0
            fi
            echo "  $HOOK phase='${PHASE:-<absent>}', retrying"
            sleep 5
          done
        resources:
          requests:
            cpu: "50m"
            memory: 64Mi
          limits:
            cpu: "200m"
            memory: 128Mi
      {{- end }}
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
