#!/bin/bash

kubectl delete -f dev/etcd.yaml

# delete the old pvcs
kubectl delete pvc -l app=etcd -n istio-system

# create the istio-system namespace
kubectl create namespace istio-system || echo "namespace already exists"

kubectl apply -f dev/etcd.yaml