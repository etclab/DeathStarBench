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
init_packages=false
install_social_network=false
uninstall_social_network=false

for cmd in "$@"; do
    case $cmd in
        init-social-graph) init_social_graph=true ;;
        build-wrk2) build_wrk2=true ;;
        install-istio) install_istio=true ;;
        init-packages) init_packages=true ;;
        install-social-network) install_social_network=true ;;
        uninstall-social-network) uninstall_social_network=true ;;
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
    python3 $SCRIPT_DIR/scripts/init_social_graph.py --graph=socfb-Reed98
fi

if [[ "$install_social_network" == "true" ]]; then
    mazu_echo "Upgrading social network..."
    helm upgrade --install social-network $SCRIPT_DIR/helm-chart/socialnetwork/ \
        --set global.mongodb.sharding.enabled=true,global.mongodb.standalone.enabled=false \
        --timeout 10m0s --wait

#         --set global.memcached.cluster.enabled=true,global.memcached.standalone.enabled=false \
#         --set global.redis.replication.enabled=true,global.redis.standalone.enabled=false \
#         --set global.redis.cluster.enabled=true,global.redis.standalone.enabled=false \
fi

if [[ "$uninstall_social_network" == "true" ]]; then
    mazu_echo "Uninstalling social network..."
    helm uninstall social-network

    # remove pvc
    for p in $(kubectl get pvc -o name -l app.kubernetes.io/name=mongodb-sharded); do kubectl delete $p; done
    for p in $(kubectl get pvc -o name -l app.kubernetes.io/name=redis-cluster); do kubectl delete $p; done
fi