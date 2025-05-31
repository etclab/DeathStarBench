# Kubernetes Deployment of Social Network

The initial Kubernetes deployment config is borrowed from [DivyanshuSaxena/DeathStarBench](https://github.com/DivyanshuSaxena/DeathStarBench)

1. Basic Single Instance Deployment
    
    Run `kubectl apply -f all.yaml`

2.  Optimized Deployment without Sharding
    
    This is an optimized deployment with only replications plus beefier resources and no clustering/sharding 
    
    Run `kubectl apply -f optimized.yaml`

3. To modify the optimization configuration

    Tune the different parameters to optimize the initial all.yaml
    
    Edit the resource values in `optimize.py` to your liking.
    
    Then run `python3 optimize.py all.yaml custom.yaml` and `kubectl apply -f custom.yaml`

    
    