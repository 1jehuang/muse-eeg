#!/bin/bash
cd /home/jeremy/muse-eeg
source .venv/bin/activate
exec python visualize.py "$@"
