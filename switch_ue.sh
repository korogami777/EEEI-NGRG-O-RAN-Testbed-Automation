#!/usr/bin/env bash
set -e

# Repository root path check
if [ ! -f "run.sh" ] || [ ! -f "generate_configurations.sh" ]; then
    echo "[ERROR] Please run this script from the root directory of O-RAN-Testbed-Automation."
    exit 1
fi

# Check if gNodeB or UE services are currently running
if ps aux | grep -E 'nr-softmodem|nr-cu|nr-du|duranta|nr-uesoftmodem|srsue' | grep -v grep > /dev/null 2>&1; then
    echo "Services are running. Please run ./stop.sh first. Exiting..."
    exit 1
fi

SELECTION=$1

if [ -z "$SELECTION" ]; then
    echo "=========================================="
    echo "          NIST O-RAN UE Selector          "
    echo "=========================================="
    echo "  [0] srsRAN_4G UE (Band 3, 15 kHz SCS)"
    echo "  [1] Duranta UE   (Band 78, 30 kHz SCS)"
    echo "=========================================="
    read -p "Select UE Type [0 or 1]: " SELECTION
fi

case "$SELECTION" in
    0)
        echo -e "\n[INFO] Selecting srsRAN_4G UE (Band 3)..."
        sed -i 's/^USE_DURANTA_UE=true$/USE_DURANTA_UE=false/' run.sh
        sed -i 's/^USE_DURANTA_UE=true$/USE_DURANTA_UE=false/' generate_configurations.sh
        ;;
    1)
        echo -e "\n[INFO] Selecting Duranta UE (Band 78)..."
        sed -i 's/^USE_DURANTA_UE=false$/USE_DURANTA_UE=true/' run.sh
        sed -i 's/^USE_DURANTA_UE=false$/USE_DURANTA_UE=true/' generate_configurations.sh
        ;;
    *)
        echo -e "\n[ERROR] Invalid choice '$SELECTION'. Please enter 0 or 1."
        exit 1
        ;;
esac

echo -e "\n[SUCCESS] Flag updated!"
grep -E "^USE_DURANTA_UE=" run.sh
echo -e "\nNext manual steps:"
echo "  1. Run './generate_configurations.sh' (modify as per specifications)"
echo "  2. Run './run.sh' (to start testbed)"
