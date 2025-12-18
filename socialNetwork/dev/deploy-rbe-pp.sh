#!/bin/bash

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# NAMESPACES=$(kubectl get ns -o jsonpath='{.items[*].metadata.name}')
NAMESPACES=(default istio-system)

for NAMESPACE in ${NAMESPACES[@]}; do
  echo "Deploying RBE PP ConfigMap to namespace: $NAMESPACE"

  kubectl delete configmap rbe-pp -n $NAMESPACE

  kubectl create configmap rbe-pp \
    --from-file=${SCRIPT_DIR}/rbe-pp.txt \
    -n ${NAMESPACE}
done

# echo "Waiting for istiod deployment..."
# until kubectl -n istio-system get deployment istiod > /dev/null 2>&1; do
#   sleep 2
# done

# echo "Patching istiod deployment..."
# kubectl -n istio-system patch deployment istiod --type='strategic' -p='
# spec:
#   template:
#     spec:
#       volumes:
#         - name: rbe-pp-volume
#           configMap:
#             name: rbe-pp
#       containers:
#         - name: discovery
#           volumeMounts:
#             - name: rbe-pp-volume
#               mountPath: /var/run/rbe-pp
#               readOnly: false
# '
# echo "Patching istio-ingressgateway deployment..."
# kubectl -n istio-system patch deployment istio-ingressgateway --type='strategic' -p='
# spec:
#   template:
#     spec:
#       volumes:
#         - name: rbe-pp-volume
#           configMap:
#             name: rbe-pp
#       containers:
#         - name: istio-proxy
#           volumeMounts:
#             - name: rbe-pp-volume
#               mountPath: /var/run/rbe-pp
#               readOnly: true
# '
