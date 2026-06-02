The results section for "End-to-end application workload" uses the following metrics

- The mean total pods under istio and mazu are calculated using: `analyze_pod_totals.py`
    - pod_totals_mazu_vs_istio.pdf
    - pod_totals_summary.csv
```csv
strategy,rps,n_runs,mean_total_pods,stddev_total_pods
st5-AttUpd,400,10,18.3000,1.6364
istio,400,10,16.1000,0.9944
```

- The mean total pending pod-seconds across 10 runs is calculated using: `python3 analyze_pending_pod_seconds_totals.py`

```
Mean total pending pod-seconds across 10 runs (all services summed):
     rps        mazu       istio    delta(m-i)  ratio(m/i)
      50         5.5         4.3           1.2       1.28x
     100        14.9        11.1           3.8       1.34x
     200        24.0        22.0           2.0       1.09x
     300        37.3        30.3           7.0       1.23x
     400        58.7        33.5          25.2       1.75x
     500        64.3        42.4          21.9       1.52x
     600        78.4        50.9          27.5       1.54x
     700        76.9        58.5          18.4       1.31x
     800        70.1        57.1          13.0       1.23x
     900        66.3        55.8          10.5       1.19x
    1000        66.5        58.6           7.9       1.13x
    1100        74.6        59.4          15.2       1.26x
    1200        73.6        59.8          13.8       1.23x

Average pending pod-seconds across all 13 RPS levels vs 400 RPS:
  strategy    avg(all rps)     @400rps       delta  ratio(400/avg)
  mazu                54.7        58.7        +4.0           1.07x
  istio               41.8        33.5        -8.3           0.80x

mazu/istio (on average) = 54.7/41.8 = 1.31x
```

- `details` microservice starts scaling at 400 QPS increasing the connection setup cost for Mazu. This is shown by figures: `pod_growth_per_service_300.pdf` vs `pod_growth_per_service_400.pdf`

