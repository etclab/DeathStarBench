# Gnuplot script to compare Istio vs Mazu: CPU, Latency, and Memory
# Usage: gnuplot compare.gnuplot

set terminal pdfcairo enhanced font "Helvetica,12" size 8,10
set output "comparison.pdf"

set multiplot layout 3,1 title "Istio vs Mazu Comparison (Social Network)" font ",14"

# ============================================================
# 1. Latency Comparison (p50, p90, p99)
# ============================================================
set title "Tail Latency Comparison" font ",13"
set xlabel "Request Rate (RPS)"
set ylabel "Latency (ms)"
set key top left
set logscale y
set grid
set style data linespoints
set pointsize 1.2

plot \
    'istio/latency.dat'  using 1:2 title "Istio p50"  lw 2 pt 7  lc rgb "#1f77b4", \
    'istio/latency.dat'  using 1:3 title "Istio p90"  lw 2 pt 9  lc rgb "#1f77b4" dt 2, \
    'istio/latency.dat'  using 1:4 title "Istio p99"  lw 2 pt 11 lc rgb "#1f77b4" dt 3, \
    'mazu/latency.dat'   using 1:2 title "Mazu p50"   lw 2 pt 6  lc rgb "#d62728", \
    'mazu/latency.dat'   using 1:3 title "Mazu p90"   lw 2 pt 8  lc rgb "#d62728" dt 2, \
    'mazu/latency.dat'   using 1:4 title "Mazu p99"   lw 2 pt 10 lc rgb "#d62728" dt 3

unset logscale y

# ============================================================
# 2. CPU Comparison (total across application pods)
# ============================================================
set title "Total CPU Usage (Application Pods)" font ",13"
set xlabel "Request Rate (RPS)"
set ylabel "CPU (cores)"
set key top left

# Sum columns 2-7 (details, productpage, ratings, reviews-v1/v2/v3) for app pods
# Column 8 = istiod/mazu control plane
plot \
    'istio/cpu.dat' using 1:($2+$3+$4+$5+$6+$7) title "Istio - App Pods"       lw 2 pt 7  lc rgb "#1f77b4", \
    'istio/cpu.dat' using 1:8                     title "Istio - Control Plane"  lw 2 pt 9  lc rgb "#1f77b4" dt 2, \
    'mazu/cpu.dat'  using 1:($2+$3+$4+$5+$6+$7) title "Mazu - App Pods"        lw 2 pt 6  lc rgb "#d62728", \
    'mazu/cpu.dat'  using 1:8                     title "Mazu - Control Plane"   lw 2 pt 8  lc rgb "#d62728" dt 2

# ============================================================
# 3. Memory Comparison (total across application pods)
# ============================================================
set title "Total Memory Usage (Application Pods)" font ",13"
set xlabel "Request Rate (RPS)"
set ylabel "Memory (MB)"
set key top left

# Convert bytes to MB (divide by 1e6), sum columns 2-7 for app pods
plot \
    'istio/memory.dat' using 1:(($2+$3+$4+$5+$6+$7)/1e6) title "Istio - App Pods"       lw 2 pt 7  lc rgb "#1f77b4", \
    'istio/memory.dat' using 1:($8/1e6)                    title "Istio - Control Plane"  lw 2 pt 9  lc rgb "#1f77b4" dt 2, \
    'mazu/memory.dat'  using 1:(($2+$3+$4+$5+$6+$7)/1e6) title "Mazu - App Pods"        lw 2 pt 6  lc rgb "#d62728", \
    'mazu/memory.dat'  using 1:($8/1e6)                    title "Mazu - Control Plane"   lw 2 pt 8  lc rgb "#d62728" dt 2

unset multiplot
