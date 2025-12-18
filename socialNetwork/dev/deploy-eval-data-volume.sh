#!/bin/bash

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

echo "Deploying an emptyDir for eval data..."

kubectl -n istio-system patch deployment istiod --type='strategic' -p='
spec:
  template:
    spec:
      volumes:
        - name: eval-data-volume
          emptyDir: {}
      containers:
        - name: discovery
          volumeMounts:
            - name: eval-data-volume
              mountPath: /var/run/eval-data
'