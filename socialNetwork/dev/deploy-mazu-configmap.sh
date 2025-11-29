#!/bin/bash

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

STRATEGY=$1

echo "Using strategy: $STRATEGY"

is_attestation_enabled=false
is_rbe_proof_enabled=false

if [[ "$STRATEGY" == "st5-AttUpd" ]]; then
  is_attestation_enabled=true
  is_rbe_proof_enabled=true
elif [[ "$STRATEGY" == "st4-AudUpd" ]]; then
  is_rbe_proof_enabled=true
else
  echo "Unknown strategy: $STRATEGY"
fi

# NAMESPACES=$(kubectl get ns -o jsonpath='{.items[*].metadata.name}')
NAMESPACES=(default istio-system)

for NAMESPACE in ${NAMESPACES[@]}; do
  echo "Deploying Mazu ConfigMap to namespace: $NAMESPACE"

  kubectl delete configmap mazu-config -n $NAMESPACE

  kubectl create configmap mazu-config \
    --from-literal=MAZU_ATTESTATION_ENABLED=$is_attestation_enabled \
    --from-literal=MAZU_RBE_PROOF_ENABLED=$is_rbe_proof_enabled \
    -n ${NAMESPACE} \
    --dry-run=client -o yaml | kubectl apply -f -

done

kubectl -n istio-system patch deployment istiod --type='strategic' -p='
spec:
  template:
    spec:
      volumes:
        - name: mazu-config-volume
          configMap:
            name: mazu-config
      containers:
        - name: discovery
          volumeMounts:
            - name: mazu-config-volume
              mountPath: /etc/mazu-config
              readOnly: true
'

kubectl -n istio-system patch deployment istio-ingressgateway --type='strategic' -p='
spec:
  template:
    spec:
      volumes:
        - name: mazu-config-volume
          configMap:
            name: mazu-config
      containers:
        - name: istio-proxy
          volumeMounts:
            - name: mazu-config-volume
              mountPath: /etc/mazu-config
              readOnly: true
'
