mazu_echo() {
    local input_text="$*"
    echo -e "\e[1;30;44mMazu:\e[0m $input_text."
}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

STRAT=${STRAT:-"atosh502"}
TAG=${STRAT:-"atosh502"}

DURATION=${DURATION:-60}
RPS=${RPS:-1000}

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
RES_DIR=${RES_DIR:-"${SCRIPT_DIR}/${TIMESTAMP}/${STRAT}-${DURATION}"}
OUT_FILE="$RES_DIR/${RPS}.txt"

# mkdir -p $RES_DIR

echo "Strategy: $STRAT | Tag: $TAG | Duration: $DURATION seconds | RPS: $RPS"
echo "Results Directory: $RES_DIR"
echo "Output File: $OUT_FILE"

echo $SCRIPT_DIR

get_ingress_ip_port () {
    INGRESS_NAME=istio-ingressgateway
    INGRESS_NS=istio-system
    export INGRESS_IP=$(kubectl -n "$INGRESS_NS" get service "$INGRESS_NAME" -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
    export INGRESS_PORT=$(kubectl -n "$INGRESS_NS" get service "$INGRESS_NAME" -o jsonpath='{.spec.ports[?(@.name=="http2")].port}')
}

init_social_graph=false
build_wrk2=false
install_istio=false
install_mazu=false
init_packages=false
install_social_network=false
uninstall_social_network=false
run_mixed_load=false
keep_alive=true # by default use keep-alive

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
        disable-keep-alive) keep_alive=false ;;
        install-prometheus) install_prometheus=true ;;
        uninstall-prometheus) uninstall_prometheus=true ;;
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
    "$ISTIOCTL_PATH" uninstall -y --purge --kubeconfig ~/.kube/config
    kubectl delete lease istiod-key-curator-leader -n istio-system --ignore-not-found
    kubectl wait --for=delete leases.coordination.k8s.io istiod-key-curator-leader -n istio-system --timeout=300s 2>/dev/null || true
fi

if [[ "$install_mazu" == "true" ]]; then
    export DOCKER_HUB=docker.io/atosh502 
    export DOCKER_TAG=${TAG}

    if [ ! -x "$ISTIOCTL_PATH" ]; then
        mazu_echo "Installing istioctl..."
        cd $HOME
        curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.24.0 TARGET_ARCH=x86_64 sh -
        export PATH=$HOME/.istioctl/bin:$PATH  # apply for current shell
        cd -
    fi

    mazu_echo "Installing Istio..."
    "$SCRIPT_DIR/dev/install_etcd.sh"

    # create istio install config file using envsubst to set hub and tag
    if [[ "$STRAT" == "st5-AttUpd" ]]; then
      envsubst '$DOCKER_HUB $DOCKER_TAG' < ${SCRIPT_DIR}/scratch/yaml/istio-operator-tpm.yaml > istio-install-config.yaml
    else
      envsubst '$DOCKER_HUB $DOCKER_TAG' < ${SCRIPT_DIR}/scratch/yaml/istio-operator.yaml > istio-install-config.yaml
    fi
    
    "$ISTIOCTL_PATH" install -f istio-install-config.yaml -y --kubeconfig ~/.kube/config
    # rm istio-install-config.yaml

    kubectl apply -f "$SCRIPT_DIR/dev/token-review-role.yaml" 
    kubectl apply -f "$SCRIPT_DIR/dev/token-review-binding.yaml"
    kubectl apply -f "$SCRIPT_DIR/dev/lease-role.yaml"

    kubectl label namespace default istio-injection=enabled --overwrite
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/mtls.yaml

    kubectl wait --for=condition=Ready pod -l app=istiod -n istio-system --timeout=300s
    kubectl wait --for=condition=Ready pod -l app=istio-ingressgateway -n istio-system --timeout=300s

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
    "$ISTIOCTL_PATH" install --set profile=default -y --kubeconfig ~/.kube/config
    kubectl label namespace default istio-injection=enabled --overwrite
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/mtls.yaml

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
    if [[ "$STRAT" == "st5-AttUpd" ]]; then
      kubectl apply -f $SCRIPT_DIR/scratch/yaml/bookinfo-const-tpm.yaml
    else 
      kubectl apply -f $SCRIPT_DIR/scratch/yaml/bookinfo-const.yaml
    fi
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/bf-gateway.yaml
    kubectl apply -f $SCRIPT_DIR/scratch/yaml/bf-hpa.yaml
fi

if [[ "$uninstall_bf" == "true" ]]; then
    mazu_echo "Uninstalling Bookinfo application..."
    if [[ "$STRAT" == "st5-AttUpd" ]]; then
      kubectl delete -f $SCRIPT_DIR/scratch/yaml/bookinfo-const-tpm.yaml
    else 
      kubectl delete -f $SCRIPT_DIR/scratch/yaml/bookinfo-const.yaml
    fi
    kubectl delete -f $SCRIPT_DIR/scratch/yaml/bf-gateway.yaml
    kubectl delete -f $SCRIPT_DIR/scratch/yaml/bf-hpa.yaml
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

if [[ "$install_prometheus" == "true" ]]; then
    mazu_echo "Installing Prometheus in istio-system..."
    kubectl apply -f $SCRIPT_DIR/scratch/release/samples/addons/prometheus.yaml
    kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=prometheus -n istio-system --timeout=300s
    mazu_echo "Prometheus is ready"
fi

if [[ "$uninstall_prometheus" == "true" ]]; then
    mazu_echo "Uninstalling Prometheus from istio-system..."
    kubectl delete -f $SCRIPT_DIR/scratch/release/samples/addons/prometheus.yaml --ignore-not-found
fi

if [[ "$get_ingress" == "true" ]]; then
    # fetch ingress IP and port
    get_ingress_ip_port

    echo "Ingress IP: $INGRESS_IP"
    echo "Ingress Port: $INGRESS_PORT"
fi

if [[ "$run_mixed_load" == "true" ]]; then
    mazu_echo "Running mixed workload..."

    # delete istio and workload if exists
    ${SCRIPT_DIR}/setup_social_network.sh uninstall-bf
    kubectl wait --for=delete pod -l app=details --timeout=300s
    kubectl wait --for=delete pod -l app=productpage --timeout=300s
    kubectl wait --for=delete pod -l app=ratings --timeout=300s
    kubectl wait --for=delete pod -l app=reviews --timeout=300s

    ${SCRIPT_DIR}/setup_social_network.sh remove-istio
    kubectl wait --for=delete pod -l app=istiod -n istio-system --timeout=300s
    kubectl wait --for=delete pod -l app=istio-ingressgateway -n istio-system --timeout=300s

    # freshly create TPMs on all nodes for the new run
    mazu_echo "Creating TPMs on all nodes..."
    NODE0="apoudel@c220g1-031118.wisc.cloudlab.us"
    ssh "$NODE0" 'for node in node-0 node-1 node-2 node-3; do ssh "$node" "~/trinc/swtpm-test/setup-tpm.sh create_tpm" & done; wait'
    mazu_echo "TPMs created on all nodes"

    sleep 10s

    # reinstall istio and workload and wait until all pods are ready
    if [[ "$STRAT" == "istio" ]]; then
        ${SCRIPT_DIR}/setup_social_network.sh install-istio

    else 
        ${SCRIPT_DIR}/dev/deploy-mazu-configmap.sh $STRAT
        ${SCRIPT_DIR}/dev/deploy-rbe-pp.sh

        if [[ "$STRAT" == "st5-AttUpd" ]]; then
            ${SCRIPT_DIR}/dev/tpm/install-k8s-tpm-device.sh
            ${SCRIPT_DIR}/dev/tpm/deploy-tpm-pubkey-configmap.sh
            ${SCRIPT_DIR}/dev/tpm/deploy-tpm-secret.sh

            ${SCRIPT_DIR}/setup_social_network.sh install-mazu

        else
            # for: st2-NIChaRes, st3-TokRev, st4-AudUpd
            ${SCRIPT_DIR}/setup_social_network.sh install-mazu
        fi
    fi

    ${SCRIPT_DIR}/setup_social_network.sh install-bf
    kubectl wait --for=condition=Ready pod -l app=details --timeout=300s
    kubectl wait --for=condition=Ready pod -l app=productpage --timeout=300s
    kubectl wait --for=condition=Ready pod -l app=ratings --timeout=300s
    kubectl wait --for=condition=Ready pod -l app=reviews --timeout=300s

    kubectl wait --for=condition=Ready pod -l app=istiod -n istio-system --timeout=300s
    kubectl wait --for=condition=Ready pod -l app=istio-ingressgateway -n istio-system --timeout=300s
    
    sleep 5s

    # fetch ingress IP and port
    get_ingress_ip_port

    echo "Running for ${RPS} reqs"

    wrk_args=()
    if [[ "$keep_alive" == "false" ]]; then
        echo "Keep alive is false"
        wrk_args+=(-H "Connection: Close")
    else
        echo "Keep alive is true"
    fi

    ../wrk2/wrk -D exp -t 16 -c 128 -d ${DURATION} -L \
        "${wrk_args[@]}" \
        -s ./wrk2/scripts/social-network/read-productpage.lua \
        http://$INGRESS_IP:$INGRESS_PORT -R ${RPS} > ${OUT_FILE}

    echo "=== Results saved in ${OUT_FILE} ==="
fi