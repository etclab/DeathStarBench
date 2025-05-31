#!/usr/bin/env python3
"""
Script to automatically add resources, replicas, and HPA to DeathStarBench SocialNetwork K8s config
"""

import yaml
import sys

def is_data_store(name):
    """Check if service is a data store (MongoDB, Redis, Memcached)"""
    data_stores = ['mongodb', 'redis', 'memcached', 'rabbitmq']
    return any(store in name.lower() for store in data_stores)

def is_microservice(name):
    """Check if service is a microservice (has 'service' in name)"""
    return 'service' in name.lower()

def add_resources(deployment):
    """Add resource specifications to deployment"""
    containers = deployment['spec']['template']['spec']['containers']
    for container in containers:
        if 'resources' not in container:
            container['resources'] = {
                'requests': {
                    'cpu': '500m',
                    'memory': '512Mi'
                },
                'limits': {
                    'cpu': '1000m',
                    'memory': '1Gi'
                }
            }
            # Data stores get more memory
            if is_data_store(deployment['metadata']['name']):
                container['resources']['requests']['memory'] = '1Gi'
                container['resources']['limits']['memory'] = '2Gi'

def add_probes(deployment):
    """Add liveness and readiness probes for microservices"""
    name = deployment['metadata']['name']
    if not is_microservice(name) or is_data_store(name):
        return
    
    containers = deployment['spec']['template']['spec']['containers']
    for container in containers:
        if 'livenessProbe' not in container:
            container['livenessProbe'] = {
                'tcpSocket': {
                    'port': 9090
                },
                'initialDelaySeconds': 30,
                'periodSeconds': 10,
                'timeoutSeconds': 5,
                'failureThreshold': 3
            }
        
        if 'readinessProbe' not in container:
            container['readinessProbe'] = {
                'tcpSocket': {
                    'port': 9090
                },
                'initialDelaySeconds': 5,
                'periodSeconds': 5,
                'timeoutSeconds': 3,
                'failureThreshold': 3
            }

def adjust_replicas(deployment):
    """Adjust replica count based on service type"""
    name = deployment['metadata']['name']
    
    # Keep data stores at 1 replica (would need StatefulSet for proper clustering)
    if is_data_store(name):
        deployment['spec']['replicas'] = 1
    # Nginx and Jaeger get 2 replicas
    elif name in ['nginx-thrift', 'jaeger', 'media-frontend']:
        deployment['spec']['replicas'] = 2
    # All microservices get 3 replicas
    else:
        deployment['spec']['replicas'] = 3

def create_hpa(deployment):
    """Create HPA configuration for microservices"""
    name = deployment['metadata']['name']
    
    # Skip data stores and jaeger
    if is_data_store(name) or name == 'jaeger':
        return None
    
    # More aggressive scaling for critical services
    min_replicas = 3
    max_replicas = 10
    if name in ['nginx-thrift', 'compose-post-service', 'user-timeline-service', 'home-timeline-service']:
        min_replicas = 3
        max_replicas = 15
    elif name == 'media-frontend':
        min_replicas = 2
        max_replicas = 8
    
    return {
        'apiVersion': 'autoscaling/v2',
        'kind': 'HorizontalPodAutoscaler',
        'metadata': {
            'name': f"{name}-hpa",
            'labels': {
                'app': name
            }
        },
        'spec': {
            'scaleTargetRef': {
                'apiVersion': 'apps/v1',
                'kind': 'Deployment',
                'name': name
            },
            'minReplicas': min_replicas,
            'maxReplicas': max_replicas,
            'metrics': [
                {
                    'type': 'Resource',
                    'resource': {
                        'name': 'cpu',
                        'target': {
                            'type': 'Utilization',
                            'averageUtilization': 70
                        }
                    }
                },
                {
                    'type': 'Resource',
                    'resource': {
                        'name': 'memory',
                        'target': {
                            'type': 'Utilization',
                            'averageUtilization': 80
                        }
                    }
                }
            ],
            'behavior': {
                'scaleDown': {
                    'stabilizationWindowSeconds': 300,
                    'policies': [
                        {
                            'type': 'Percent',
                            'value': 10,
                            'periodSeconds': 60
                        }
                    ]
                },
                'scaleUp': {
                    'stabilizationWindowSeconds': 60,
                    'policies': [
                        {
                            'type': 'Percent',
                            'value': 50,
                            'periodSeconds': 60
                        }
                    ]
                }
            }
        }
    }

def process_yaml(input_file, output_file):
    """Process the YAML file and add optimizations"""
    
    with open(input_file, 'r') as f:
        # Load all documents from the YAML file
        documents = list(yaml.safe_load_all(f))
    
    # Process deployments and collect HPAs
    hpas = []
    
    for doc in documents:
        if doc and isinstance(doc, dict) and doc.get('kind') == 'Deployment':
            print(f"Processing deployment: {doc['metadata']['name']}")
            adjust_replicas(doc)
            add_resources(doc)
            add_probes(doc)
            
            # Create HPA if needed
            hpa = create_hpa(doc)
            if hpa:
                hpas.append(hpa)
    
    # Add HPAs to documents
    documents.extend(hpas)
    
    # Write the modified YAML with proper formatting
    with open(output_file, 'w') as f:
        # Custom Dumper class for better formatting
        class CustomDumper(yaml.SafeDumper):
            def increase_indent(self, flow=False, indentless=False):
                return super(CustomDumper, self).increase_indent(flow, False)
        
        # Add representer for multi-line strings
        def str_presenter(dumper, data):
            if '\n' in data:  # Multi-line strings
                return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
            return dumper.represent_scalar('tag:yaml.org,2002:str', data)
        
        CustomDumper.add_representer(str, str_presenter)
        
        # Dump all documents with proper separation
        for i, doc in enumerate(documents):
            if i > 0:
                f.write('---\n')
            yaml.dump(
                doc, 
                f, 
                Dumper=CustomDumper,
                default_flow_style=False, 
                sort_keys=False,
                width=float('inf'),  # Prevent line wrapping
                allow_unicode=True
            )
    
    print(f"\nOptimized configuration written to {output_file}")
    print(f"Added {len(hpas)} HorizontalPodAutoscalers")
    
    # Validate the output
    try:
        with open(output_file, 'r') as f:
            list(yaml.safe_load_all(f))
        print("Output YAML is valid!")
    except yaml.YAMLError as e:
        print(f"Warning: Output YAML validation failed: {e}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python optimize.py <input_yaml> <output_yaml>")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2]
    
    try:
        process_yaml(input_file, output_file)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
