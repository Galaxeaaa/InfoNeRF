#!/bin/bash

if [ $1 -eq 0 ]
then
    python run_nerf.py --config configs/infonerf/oppo/config_6v.txt --expname oppo/infonerf/chair --datadir ./data/nerf_oppo/chair
elif [ $1 -eq 1 ]
then
    python run_nerf.py --config configs/infonerf/oppo/config_6v.txt --expname oppo/infonerf/drums --datadir ./data/nerf_oppo/drums
elif [ $1 -eq 2 ]
then
    python run_nerf.py --config configs/infonerf/oppo/config_6v.txt --expname oppo/infonerf/ficus --datadir ./data/nerf_oppo/ficus
elif [ $1 -eq 3 ]
then
    python run_nerf.py --config configs/infonerf/oppo/config_6v.txt --expname oppo/infonerf/lego --datadir ./data/nerf_oppo/lego
elif [ $1 -eq 4 ]
then
    python run_nerf.py --config configs/infonerf/oppo/config_6v.txt --expname oppo/infonerf/mic --datadir ./data/nerf_oppo/mic
elif [ $1 -eq 5 ]
then
    python run_nerf.py --config configs/infonerf/oppo/config_6v.txt --expname oppo/infonerf/ship --datadir ./data/nerf_oppo/ship
elif [ $1 -eq 6 ]
then
    python run_nerf.py --config configs/infonerf/oppo/config_6v.txt --expname oppo/infonerf/hotdog --datadir ./data/nerf_oppo/hotdog
elif [ $1 -eq 7 ]
then
    python run_nerf.py --config configs/infonerf/oppo/config_6v.txt --expname oppo/infonerf/materials --datadir ./data/nerf_oppo/materials
fi
