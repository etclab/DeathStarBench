#!/bin/bash

# Namespace to look for pods
NAMESPACE=default

# Temp file to track pod name occurrences
declare -A pod_counter

# Get list of all pods in the default namespace
kubectl get pods -n $NAMESPACE --no-headers -o custom-columns=":metadata.name" | while read pod_name; do
  # Extract the prefix (e.g., "first", "second")
  prefix=$(echo "$pod_name" | cut -d'-' -f1)

  # Increment counter for this prefix
  ((pod_counter["$prefix"]++))
  count=${pod_counter["$prefix"]}

  # Construct the output log filename
  log_file="_${prefix}-proxy${count}.log"

  echo "Saving logs from pod $pod_name (container: istio-proxy) to $log_file"

  # Fetch the logs and write to file
  kubectl logs "$pod_name" -n $NAMESPACE -c istio-proxy > "$log_file"

done
