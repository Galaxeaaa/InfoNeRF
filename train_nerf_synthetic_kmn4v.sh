#!/bin/bash

if [ $1 -eq 0 ]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_4v.txt --expname synthetic/infonerf/chair --datadir ./data/nerf_synthetic/chair --train_scene 55 87 94 5
elif [ $1 -eq 1 ]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_4v.txt --expname synthetic/infonerf/drums --datadir ./data/nerf_synthetic/drums --train_scene 80 65 72 66
elif [ $1 -eq 2 ]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_4v.txt --expname synthetic/infonerf/ficus --datadir ./data/nerf_synthetic/ficus --train_scene 23 52 24 91
elif [ $1 -eq 3 ]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_4v.txt --expname synthetic/infonerf/lego --datadir ./data/nerf_synthetic/lego --train_scene 16 57 28 1
elif [ $1 -eq 4 ]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_4v.txt --expname synthetic/infonerf/mic --datadir ./data/nerf_synthetic/mic --train_scene 52 37 73 64
elif [ $1 -eq 5 ]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_4v.txt --expname synthetic/infonerf/ship --datadir ./data/nerf_synthetic/ship --train_scene 50 17 9 31
elif [ $1 -eq 6 ]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_4v.txt --expname synthetic/infonerf/hotdog --datadir ./data/nerf_synthetic/hotdog --train_scene 22 62 96 23
elif [ $1 -eq 7 ]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_4v.txt --expname synthetic/infonerf/materials --datadir ./data/nerf_synthetic/materials --train_scene 33 39 36 5
fi


