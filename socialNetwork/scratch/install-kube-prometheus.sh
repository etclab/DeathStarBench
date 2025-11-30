#!/bin/bash

helm upgrade --install kube-prometheus --namespace kube-prometheus \
    --set nodeExporter.enabled=false \
    --create-namespace oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack