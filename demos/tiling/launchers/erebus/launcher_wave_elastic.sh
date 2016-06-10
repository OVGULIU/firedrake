#!/bin/bash

rm -rf /data/output/

OPTS="--output 5000 --flatten True --nocache True"
TILE_OPTS="--fusion-mode only_tile --coloring default"

export OMP_NUM_THREADS=1
export SLOPE_BACKEND=SEQUENTIAL

# The execution modes
declare -a em_all=(1 2 3 4 5 6)

# Extra options for each mode
declare -a opts_em1=("" "--glb-maps True")
declare -a opts_em2=("" "--glb-maps True")
declare -a opts_em3=("" "--glb-maps True")
declare -a opts_em4=("" "--extra-halo 1" "--glb-maps True" "--glb-maps True --extra-halo 1")
declare -a opts_em5=("" "--extra-halo 1" "--glb-maps True" "--glb-maps True --extra-halo 1")
declare -a opts_em6=("" "--extra-halo 1" "--glb-maps True" "--glb-maps True --extra-halo 1")

# Tile sizes for each poly order
declare -a ts_p1=(140 250 320 400)
declare -a ts_p2=(70 140 200 300)
declare -a ts_p3=(45 60 75)
declare -a ts_p4=(20 45 70)

# Partition modes for each poly order
declare -a part_p1=("chunk" "metis")
declare -a part_p2=("chunk" "metis")
declare -a part_p3=("chunk" "metis")
declare -a part_p4=("chunk" "metis")

# Meshes for each poly order
declare -a mesh_p1=("--mesh-size (300.0,150.0,1.0)" "--mesh-size (300.0,150.0,1.2)")
declare -a mesh_p2=("--mesh-size (300.0,150.0,1.0)" "--mesh-size (300.0,150.0,1.2)")
declare -a mesh_p3=("--mesh-size (300.0,150.0,1.0)" "--mesh-size (300.0,150.0,1.2)")
declare -a mesh_p4=("--mesh-size (300.0,150.0,1.0)" "--mesh-size (300.0,150.0,1.2)")

for poly in 1 2 3 4
do
    output_file="output_p"$poly".txt"
    rm -f $output_file
    touch $output_file
    echo "Polynomial order "$poly
    mesh_p="mesh_p$poly[@]"
    meshes=( "${!mesh_p}" )
    for mesh in "${meshes[@]}"
    do
        echo "    Running "$mesh
        echo "        Untiled ..."
        mpirun -np 4 --bind-to-core python wave_elastic.py --poly-order $poly $mesh $OPTS --num-unroll 0 1>> $output_file 2>> $output_file
        mpirun -np 4 --bind-to-core python wave_elastic.py --poly-order $poly $mesh $OPTS --num-unroll 0 1>> $output_file 2>> $output_file
        mpirun -np 4 --bind-to-core python wave_elastic.py --poly-order $poly $mesh $OPTS --num-unroll 0 1>> $output_file 2>> $output_file
        part_p="part_p$poly[*]"
        for p in ${!part_p}
        do
            for em in ${em_all[@]}
            do
                opts="opts_em$em[@]"
                opts_em=( "${!opts}" )
                for opt in "${opts_em[@]}"
                do
                    ts_p="ts_p$poly[*]"
                    for ts in ${!ts_p}
                    do
                        echo "        Tiled (pm="$p", ts="$ts", em="$em") ..."
                        mpirun -np 4 --bind-to-core python wave_elastic.py --poly-order $poly $mesh $OPTS --num-unroll 1 --tile-size $ts --part-mode $p --explicit-mode $em $TILE_OPTS $opt 1>> $output_file 2>> $output_file
                    done
                done
            done
        done
    done
done

export OMP_NUM_THREADS=4
export KMP_AFFINITY=scatter
export SLOPE_BACKEND=OMP
echo "No OMP experiments set"