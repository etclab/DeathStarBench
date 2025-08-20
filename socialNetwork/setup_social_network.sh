mazu_echo() {
    local input_text="$*"
    echo -e "\e[1;30;44mMazu:\e[0m $input_text."
}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

echo $SCRIPT_DIR

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
    DOCKER_TAG=st3-TokRev

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
        --set tag=$DOCKER_TAG --set "values.global.imagePullPolicy=Always" -y

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
    NODE_IP=$(curl -4 -s icanhazip.com)
    NODE_PORT=$(kubectl -n istio-system get service istio-ingressgateway -o jsonpath='{.spec.ports[?(@.name=="http2")].nodePort}')
    python3 $SCRIPT_DIR/scripts/init_social_graph.py --graph=socfb-Reed98
fi

if [[ "$install_social_network" == "true" ]]; then
    mazu_echo "Installing social network..."

    kubectl apply -f $SCRIPT_DIR/kubernetes/optimized.yaml
fi

if [[ "$uninstall_social_network" == "true" ]]; then
    mazu_echo "Uninstalling social network..."
    
    kubectl delete -f $SCRIPT_DIR/kubernetes/optimized.yaml
fi

if [[ "$run_mixed_load" == "true" ]]; then
    mazu_echo "Running mixed workload..."
    NODE_IP=$(curl -4 -s icanhazip.com)
    NODE_PORT=$(kubectl -n istio-system get service istio-ingressgateway -o jsonpath='{.spec.ports[?(@.name=="http2")].nodePort}') 

    mkdir -p results/mixed

    for reqs in 1000 2000 3000 4000
    do
        echo "Running for ${reqs} reqs"
        ../wrk2/wrk -D exp -t 10 -c 10 -d 60 -L -s ./wrk2/scripts/social-network/mixed-workload.lua http://$NODE_IP:$NODE_PORT -R ${reqs} >> results/mixed/${reqs}.txt
    done
    echo "=== All tests completed ==="
    echo "Results saved in results/mixed/"
fi