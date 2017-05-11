#include <curand_kernel.h>
#include <stdio.h>
texture<float, 2, cudaReadModeElementType> param_tex;
#define num_species {n_species}
#define NPARAM {n_params}
#define NREACT {n_reactions}

__constant__ int stoch_matrix[]={{
{stoch}
}};


__device__ void propensities(int *y, double *h, int tid)
{{
{hazards}
}}

extern "C"{{
__device__ void stoichiometry(int *y, int r){{
    for(int i=0; i<num_species; i++){{
        y[i]+=stoch_matrix[r*num_species+ i];
    }}
}}


__device__ int sample(int nr, double* a, float u){{
    int i = 0;
    for(;i < nr - 1 && u > a[i]; i++){{
        u -= a[i];
        }}
    return i;
}}
///*
__global__ void Gillespie_all_steps(int* species_matrix, int* result, float* time,
                                    int NRESULTS){{

    int tid = blockDim.x * blockIdx.x + threadIdx.x;
    //curandState randState;
    curandStateMRG32k3a randState;
    curand_init(clock64(), tid, 0, &randState);
    double t = time[0] ;
    double r1 = 0.0f;
    double r2 = 0.0f;
    double a0 = 0.0f;
    int k = 0;
    int y[num_species];
    double A[NREACT];

    // start first step
    for(int i=0; i<NREACT; i++){{
        A[i] = 0;
        }}

    for(int i=0; i<num_species; i++){{
        y[i] = species_matrix[i];
        }}

    propensities( y, A , tid);

    for(int i=0; i<NREACT; i++){{
        a0 += A[i];
        }}

    // beginning of loop
    for(int i=0; i<NRESULTS;){{
        if(t>=time[i]){{
            for(int j=0; j<num_species; j++){{
                result[tid*NRESULTS*num_species + i*num_species + j] = y[j];
                }}
            i++;
            }}
        else{{
            r1 =  curand_uniform(&randState);
            k = sample(NREACT, A, a0*r1);
            stoichiometry( y ,k );
            propensities( y, A, tid );
            a0 = 0;
            // summing up propensities
            for(int j=0; j<NREACT; j++)
                a0 += A[j];

            r2 =  curand_uniform(&randState);
            t += -__logf(r2)/a0;
            }}
        }}
    return;
    }}
//*/
__global__ void Gillespie_one_step(int* species_matrix, int* result, float* start_time,
                                    float end_time, float* result_time, int stride){{

    int tid = blockDim.x * blockIdx.x + threadIdx.x;
    //curandState randState;
    curandStateMRG32k3a randState;
    curand_init(clock64(), tid, 0, &randState);
    float start_t = start_time[tid] ;

    double r1 = 0.0f;
    double r2 = 0.0f;
    double dt = 0.0f;
    double a0 = 0;
    int k = 0;
    int y[num_species];
    int ylast[num_species];

    double A[NREACT];

    // start first step
    for(int i=0; i<NREACT; i++){{
        A[i] = 0;
        }}

    for(int i=0; i<num_species; i++){{
        y[i] = ((int*)( (char*) species_matrix + tid * stride))[i];
        }}

    propensities( y, A , tid);

    for(int i=0; i<NREACT; i++){{
        a0 += A[i];
        }}

    // beginning of loop
    while (start_t < end_time){{
        r1 =  curand_uniform(&randState);
        k = sample(NREACT, A, a0*r1);
        stoichiometry( y ,k );
        propensities( y, A, tid );
        a0 = 0;
        // summing up propensities
        for(int j=0; j<NREACT; j++){{
            a0 += A[j];
            }}

//        __syncthreads();

        r2 =  curand_uniform(&randState);
        dt = -__logf(r2)/a0;
        start_t += dt;
        for(int j=0; j<num_species; j++){{
            ylast[j] = y[j];
            }}
        }}

    for(int j=0; j<num_species; j++){{
//        result[tid*num_species +  j] = y[j];
        result[tid*num_species +  j] = ylast[j];
        }}
//    start_t -= dt;
    result_time[tid] = start_t;
    return;
    }}
}}