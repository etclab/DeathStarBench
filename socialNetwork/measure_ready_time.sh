#!/bin/bash

mazu_echo() {
    local input_text="$*"
    echo -e "\e[1;30;44mMazu:\e[0m $input_text."
}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

DOCKER_HUB=docker.io/atosh502 
DOCKER_TAG=atosh502

setup=false
run=false
clean=false

for cmd in "$@"; do
    case $cmd in
        setup) setup=true ;;
        run) run=true ;;
        clean) clean=true ;;
        *) 
            mazu_echo "Unknown command: $cmd"
            ;;
    esac
done

if [[ "$clean" == "true" ]]; then
    "$ISTIOCTL_PATH" uninstall -y --purge
    
    kubectl wait --for=delete pod -l app=istiod -n istio-system --timeout=300s
    
    kubectl delete lease istiod-key-curator-leader -n istio-system
    kubectl wait --for=delete leases.coordination.k8s.io istiod-key-curator-leader -n istio-system --timeout=300s
fi

if [[ "$setup" == "true" ]]; then

    if [ ! -x "$ISTIOCTL_PATH" ]; then
        mazu_echo "Installing istioctl..."
        cd $HOME
        curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.24.0 TARGET_ARCH=x86_64 sh -
        export PATH=$HOME/.istioctl/bin:$PATH  # apply for current shell
        cd -
    fi

    mazu_echo "Installing Istio..."
    "$SCRIPT_DIR/dev/install_etcd.sh"

    "$ISTIOCTL_PATH" install --set profile=minimal --set hub=$DOCKER_HUB \
        --set tag=$DOCKER_TAG --set "values.global.imagePullPolicy=Always" -y \
        --set components.pilot.k8s.hpaSpec.maxReplicas=1

    kubectl wait --for=condition=Ready pod -l app=istiod -n istio-system --timeout=300s
    
    kubectl apply -f "$SCRIPT_DIR/dev/token-review-role.yaml" 
    kubectl apply -f "$SCRIPT_DIR/dev/token-review-binding.yaml"
    kubectl apply -f "$SCRIPT_DIR/dev/lease-role.yaml"

    kubectl label namespace default istio-injection=enabled --overwrite
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/mtls.yaml
    
    "$SCRIPT_DIR/dev/deploy-eval-data-volume.sh" 

    mazu_echo "Waiting for Istiod restart..."
    kubectl rollout status deployment/istiod -n istio-system

    kubectl delete lease istiod-key-curator-leader -n istio-system --ignore-not-found
    # kubectl wait --for=delete leases.coordination.k8s.io istiod-key-curator-leader -n istio-system --timeout=300s
fi

if [[ "$run" == "true" ]]; then
    mazu_echo "Measuring ready time..."

    for pods in 5 10 25 50 75 100;
    do
        mkdir -p "$SCRIPT_DIR/ready-time-logs/pods-$pods"
        
        for trial in {1..25}
        do
            echo "Pods: $pods, Trial: $trial"

            # uninstall test application
            echo "Removing test application..."
            kubectl delete -f $SCRIPT_DIR/scratch/yaml/ready-eval-service.yaml
            kubectl wait --for=delete pod -l app=agnhost --timeout=-1s

            # remove istio
            echo "Removing Istio..."
            ${SCRIPT_DIR}/measure_ready_time.sh clean

            # setup istio
            echo "Setting up Istio..."
            ${SCRIPT_DIR}/measure_ready_time.sh setup

            # setup {pods} replicas of the test application
            echo "Deploying test application with $pods replicas..."
            kubectl apply -f $SCRIPT_DIR/scratch/yaml/ready-eval-service.yaml
            kubectl scale deployment agnhost-v1 --replicas=${pods}

            kubectl rollout status deployment/agnhost-v1 --timeout=-1s
            # kubectl wait --for=condition=Ready pod --all --timeout=-1s

            # truncate the log file
            # echo "Truncating log file..."
            # ISTIOD_POD=$(kubectl get pods -n istio-system -l app=istiod -o jsonpath="{.items[0].metadata.name}")
            # kubectl exec -n istio-system ${ISTIOD_POD} -- bash -c "echo '' > /var/run/eval-data/log.csv"

            sleep 5

            # deploy an additional replica
            echo "Deploying an additional replica..."
            new_pods=$((pods + 1))
            kubectl scale deployment agnhost-v1 --replicas=${new_pods}

            kubectl rollout status deployment/agnhost-v1 --timeout=-1s
            # kubectl wait --for=condition=Ready pod --all --timeout=-1s

            # extract the log file and rename it
            echo "Extracting log file..."
            LOG_FILE="$SCRIPT_DIR/ready-time-logs/pods-$pods/log-trial-$trial.csv"
            ISTIOD_POD=$(kubectl get pods -n istio-system -l app=istiod -o jsonpath="{.items[0].metadata.name}")

            kubectl cp -n istio-system ${ISTIOD_POD}:/var/run/eval-data/log.csv ${LOG_FILE}

            sleep 5
        done
    done
fi