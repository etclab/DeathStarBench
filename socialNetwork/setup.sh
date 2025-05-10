- Pre-requisites
    - Docker
    - Docker-compose
    - Python 3.5+ (with asyncio and aiohttp)
        - (in repo root)
        - `python3 -m venv venv`
        - `. venv/bin/activate`
        - pip install asyncio aiohttp
    - libssl-dev (apt-get install libssl-dev)
    - libz-dev (apt-get install libz-dev)
    - luarocks (apt-get install luarocks)
    - luasocket (luarocks install luasocket)

- Check if port 8080, 8081, and 16686 are free
- Init a docker swarm with: `docker swarm init`
- Deploy the stack with: `docker stack deploy --compose-file=docker-compose-swarm.yml social-network`
- Build wrk2
    - cd ../wkr2
    - make
    - cd ../socialNetwork
- Compose posts
    - ../wrk2/wrk -D exp -t 12 -c 400 -d 300 -L -s ./wrk2/scripts/social-network/compose-post.lua http://localhost:8080/wrk2-api/post/compose -R 10

---

- k8s cluster
minikube start -p=$USER --memory=32768 --cpus=32 -c=containerd
minikube profile $USER
minikube addons enable metrics-server -p $USER

# curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.24.0 TARGET_ARCH=x86_64 sh -
istioctl install --set profile=default
kubectl label namespace default istio-injection=enabled --overwrite
kubectl apply -f scratch/yaml/mtls.yaml
helm install social-network ./helm-chart/socialnetwork/

# helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
# helm repo update
# helm install kube-prometheus-stack --create-namespace --namespace kube-prometheus-stack \
#     prometheus-community/kube-prometheus-stack

# # Get Grafana 'admin' user password by running:
# kubectl -n kube-prometheus-stack get secrets kube-prometheus-stack-grafana -o jsonpath="{.data.admin-password}" | base64 -d ; echo

# # Access Grafana local instance:
# export POD_NAME=$(kubectl -n kube-prometheus-stack get pod -l "app.kubernetes.io/name=grafana,app.kubernetes.io/instance=kube-prometheus-stack" -oname)
# kubectl -n kube-prometheus-stack port-forward $POD_NAME 3000

# kubectl apply -f ./scratch/prometheus-operator.yaml

kubectl port-forward prometheus-prometheus-0 -n istio-prometheus 9090:9090
