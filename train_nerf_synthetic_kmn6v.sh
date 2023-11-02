#!/bin/bash

if [ $1 -eq 0 ]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_6v.txt --expname synthetic/infonerf/chair --datadir ./data/nerf_synthetic/chair --train_scene 94 29 46 8 28 5
else if [ $1 -eq 1]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_6v.txt --expname synthetic/infonerf/drums --datadir ./data/nerf_synthetic/drums --train_scene 71 46 18 85 39 25
else if [ $1 -eq 2]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_6v.txt --expname synthetic/infonerf/ficus --datadir ./data/nerf_synthetic/ficus --train_scene 51 37 69 1 31 93
else if [ $1 -eq 3]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_6v.txt --expname synthetic/infonerf/lego --datadir ./data/nerf_synthetic/lego --train_scene 71 1 47 83 73 15
else if [ $1 -eq 4]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_6v.txt --expname synthetic/infonerf/mic --datadir ./data/nerf_synthetic/mic --train_scene 30 37 79 64 82 7
else if [ $1 -eq 5]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_6v.txt --expname synthetic/infonerf/ship --datadir ./data/nerf_synthetic/ship --train_scene 13 74 9 95 31 7
else if [ $1 -eq 6]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_6v.txt --expname synthetic/infonerf/hotdog --datadir ./data/nerf_synthetic/hotdog --train_scene 33 92 57 19 94 23
else if [ $1 -eq 7]
then
    python run_nerf.py --config configs/infonerf/synthetic/config_6v.txt --expname synthetic/infonerf/materials --datadir ./data/nerf_synthetic/materials --train_scene 19 22 82 52 51 53
fi