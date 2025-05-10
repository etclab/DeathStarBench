# "DNS_DOMAIN like v104.qualistio.org"

WD=$(dirname "$0")
WD=$(cd "$WD"; pwd)

domain=${DNS_DOMAIN:-"localhost"}
kubectl create namespace istio-prometheus || true
kubectl create -f "${WD}/base/files" || true # Might fail if we already installed, so allow failures
helm template --set domain="${domain}" "${WD}/base" | kubectl apply -f -

# Check deployment
MAXRETRIES=0
until kubectl rollout status --watch --timeout=60s statefulset/prometheus-prometheus -n istio-prometheus || [ $MAXRETRIES -eq 60 ]
do
    MAXRETRIES=$((MAXRETRIES + 1))
    sleep 5
done
if [[ $MAXRETRIES -eq 60 ]]; then
    echo "prometheus were not created successfully"
    exit 1
fi

kubectl apply -f "${WD}/release/samples/addons/extras/prometheus-operator.yaml" -n istio-system
kubectl apply -f "${WD}/addons/servicemonitors.yaml"

kubectl apply -f "${WD}/release/samples/addons/grafana.yaml" -n istio-system
kubectl apply -f "${WD}/addons/grafana-cm.yaml" -n istio-system
kubectl rollout restart deployment grafana -n istio-system
