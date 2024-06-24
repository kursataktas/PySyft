#!/bin/bash

# Run the different enclave servers as follows:
# bash scripts/reloadable_run.sh --port 9081 --name "canada-domain"
# bash scripts/reloadable_run.sh --port 9082 --name "italy-domain" 
# bash scripts/reloadable_run.sh --port 9083 --name "canada-enclave" --node_type "enclave"

# And get the NodeHandlers as follows (note that the handlers point to the correct objects but are buggy in their attributes)
# canada_domain = sy.orchestra.launch(port=9081, deploy_to="remote")
# italy_domain = sy.orchestra.launch(port=9082, deploy_to="remote")
# canada_enclave = sy.orchestra.launch(port=9083, deploy_to="remote")

# Default values
PORT=9694
HOST="0.0.0.0"
FACTORY="--factory"
RELOAD="--reload"

# Parse command-line arguments
while [ "$1" != "" ]; do
    case $1 in
        --port )           shift
                           PORT=$1
                           ;;
        --name )           shift
                           NAME=$1
                           ;;
        --processes )      shift
                           PROCESSES=$1
                           ;;
        --reset )          shift
                           RESET=$1
                           ;;
        --local_db )       shift
                           LOCAL_DB=$1
                           ;;
        --node_type )      shift
                           NODE_TYPE=$1
                           ;;
        --node_side_type ) shift
                           NODE_SIDE_TYPE=$1
                           ;;
        * )                echo "Invalid option: $1"
                           exit 1
    esac
    shift
done

# Set environment variables
export NODE_NAME=${NAME:-testing-node}
export PROCESSES=${PROCESSES:-1}
export RESET=${RESET:-False}
export LOCAL_DB=${LOCAL_DB:-True}
export NODE_TYPE=${NODE_TYPE:-domain}
export NODE_SIDE_TYPE=${NODE_SIDE_TYPE:-high}

# Run uvicorn
uvicorn syft.node.server:run_reloadable_app $FACTORY --host $HOST --port $PORT $RELOAD