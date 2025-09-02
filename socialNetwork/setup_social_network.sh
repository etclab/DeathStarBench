mazu_echo() {
    local input_text="$*"
    echo -e "\e[1;30;44mMazu:\e[0m $input_text."
}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

echo $SCRIPT_DIR

get_ingress_ip_port () {
    INGRESS_NAME=istio-ingressgateway
    INGRESS_NS=istio-system
    INGRESS_IP=$(kubectl -n "$INGRESS_NS" get service "$INGRESS_NAME" -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
    INGRESS_PORT=$(kubectl -n "$INGRESS_NS" get service "$INGRESS_NAME" -o jsonpath='{.spec.ports[?(@.name=="http2")].port}')
}

init_social_graph=false
build_wrk2=false
install_istio=false
install_mazu=false
init_packages=false
install_social_network=false
uninstall_social_network=false
run_mixed_load=false

for cmd in "$@"; do
    case $cmd in
        init-social-graph) init_social_graph=true ;;
        build-wrk2) build_wrk2=true ;;
        install-istio) install_istio=true ;;
        install-mazu) install_mazu=true ;;
        install-bf) install_bf=true ;;
        get-ingress) get_ingress=true ;;
        uninstall-bf) uninstall_bf=true ;;
        init-packages) init_packages=true ;;
        install-social-network) install_social_network=true ;;
        uninstall-social-network) uninstall_social_network=true ;;
        run-mixed-load) run_mixed_load=true;;
        remove-istio) remove_istio=true ;;
        *) 
            mazu_echo "Unknown command: $cmd"
            ;;
    esac
done

if [[ "$init_packages" == "true" ]]; then
    mazu_echo "Installing dependencies..."
    sudo apt update && sudo apt upgrade -y

    sudo apt install libssl-dev libz-dev luarocks -y
    sudo luarocks install luasocket

    git submodule update --init --recursive

    pip install asyncio aiohttp
fi

if [[ "$build_wrk2" == "true" ]]; then
    mazu_echo "Building wrk2..."
    cd $SCRIPT_DIR
    cd ../wrk2
    make
    cd $SCRIPT_DIR
fi

if [[ "$remove_istio" == "true" ]]; then
    "$ISTIOCTL_PATH" uninstall -y --purge
fi

if [[ "$install_mazu" == "true" ]]; then
    DOCKER_HUB=docker.io/atosh502 
    # DOCKER_TAG=st3-TokRev
    DOCKER_TAG=st2-NIChaRes

    if [ ! -x "$ISTIOCTL_PATH" ]; then
        mazu_echo "Installing istioctl..."
        cd $HOME
        curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.24.0 TARGET_ARCH=x86_64 sh -
        export PATH=$HOME/.istioctl/bin:$PATH  # apply for current shell
        cd -
    fi

    mazu_echo "Installing Istio..."
    "$SCRIPT_DIR/dev/install_etcd.sh"

    "$ISTIOCTL_PATH" install --set profile=default --set hub=$DOCKER_HUB \
        --set tag=$DOCKER_TAG --set "values.global.imagePullPolicy=Always" -y \
        --set values.global.proxy.resources.limits.memory=2Gi \

    kubectl apply -f "$SCRIPT_DIR/dev/token-review-role.yaml" 
    kubectl apply -f "$SCRIPT_DIR/dev/token-review-binding.yaml"

    kubectl label namespace default istio-injection=enabled --overwrite
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/mtls.yaml

    mazu_echo "Installing Prometheus..."
    "$SCRIPT_DIR/scratch/install-prometheus.sh"
fi

if [[ "$install_istio" == "true" ]]; then

    if [ ! -x "$ISTIOCTL_PATH" ]; then
        mazu_echo "Installing istioctl..."
        cd $HOME
        curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.24.0 TARGET_ARCH=x86_64 sh -
        export PATH=$HOME/.istioctl/bin:$PATH  # apply for current shell
        cd -
    fi

    mazu_echo "Installing Istio..."
    "$ISTIOCTL_PATH" install --set profile=default -y
    kubectl label namespace default istio-injection=enabled --overwrite
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/mtls.yaml

    mazu_echo "Installing Prometheus..."
    "$SCRIPT_DIR/scratch/install-prometheus.sh"
fi

if [[ "$init_social_graph" == "true" ]]; then
    mazu_echo "Initializing social graph..."

    get_ingress_ip_port

    python3 $SCRIPT_DIR/scripts/init_social_graph.py --graph=socfb-Reed98 \
        --ip=$INGRESS_IP --port=$INGRESS_PORT
fi

if [[ "$install_social_network" == "true" ]]; then
    mazu_echo "Configuring gateway and virtual service..."
    kubectl apply -f $SCRIPT_DIR/kubernetes/istio-gateway.yaml
    
    mazu_echo "Upgrading social network..."
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/mcrouter-role.yaml

    helm upgrade --install social-network $SCRIPT_DIR/helm-chart/socialnetwork/ \
        --timeout 10m0s --wait

fi

if [[ "$install_bf" == "true" ]]; then
    mazu_echo "Installing Bookinfo application..."
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/bookinfo.yaml
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/bf-gateway.yaml
fi

if [[ "$uninstall_bf" == "true" ]]; then
    mazu_echo "Uninstalling Bookinfo application..."
    kubectl delete -f $SCRIPT_DIR/scratch/yaml/bookinfo.yaml
    kubectl delete -f $SCRIPT_DIR/scratch/yaml/bf-gateway.yaml
fi

if [[ "$uninstall_social_network" == "true" ]]; then
    mazu_echo "Uninstalling social network..."
    helm uninstall social-network

    # remove mongodb/redis statefulsets
    kubectl delete statefulsets --all --wait=true

    # remove pvc
    for p in $(kubectl get pvc -o name -l app.kubernetes.io/name=mongodb-sharded); do kubectl delete $p; done
    for p in $(kubectl get pvc -o name -l app.kubernetes.io/name=redis-cluster); do kubectl delete $p; done

    kubectl delete pods redis-cluster-readiness-hook
    kubectl delete pods setup-collection-sharding-hook
    kubectl delete pods setup-mcrouter-configmap

    # Clean up secrets and configmaps related to mongodb
    kubectl delete secrets mongodb-sharded
    kubectl delete configmaps mongo-init-script
    kubectl delete configmaps mongodb-sharded-replicaset-entrypoint
    
    kubectl delete -f $SCRIPT_DIR/scratch/yaml/mcrouter-role.yaml

    mazu_echo "Removing gateway and virtual service..."
    kubectl delete -f $SCRIPT_DIR/kubernetes/istio-gateway.yaml
fi

if [[ "$get_ingress" == "true" ]]; then
    # fetch ingress IP and port
    get_ingress_ip_port

    echo "Ingress IP: $INGRESS_IP"
    echo "Ingress Port: $INGRESS_PORT"
fi

if [[ "$run_mixed_load" == "true" ]]; then
    mazu_echo "Running mixed workload..."

    # fetch ingress IP and port
    get_ingress_ip_port

    # mkdir -p results/mixed
    mkdir -p results/home

    # for reqs in 1000 2000 3000 4000
    for reqs in 1 
    do
        echo "Running for ${reqs} reqs"
        # ../wrk2/wrk -D exp -t 1 -c 1 -d 1 -L -s ./wrk2/scripts/social-network/mixed-workload.lua http://$INGRESS_IP:$INGRESS_PORT -R ${reqs} >> results/mixed/${reqs}.txt
        ../wrk2/wrk -D exp -t 1 -c 1 -d 1 -L -s ./wrk2/scripts/social-network/read-home-timeline.lua http://$INGRESS_IP:$INGRESS_PORT -R ${reqs} >> results/home/${reqs}.txt
    done
    echo "=== All tests completed ==="
    echo "Results saved in results/mixed/"
fi