from __future__ import print_function
import numpy as np
try:
    import pycuda
    import pycuda.autoinit
    import pycuda.compiler
    import pycuda.tools as tools
    import pycuda.driver as driver
    import pycuda.gpuarray as gpuarray
except ImportError:
    print("Need to install pycuda inorder to use GPUssa")
    pycuda = None

import re
import os
import sympy
from pysb.bng import generate_equations
from pysb.simulator.base import Simulator, SimulationResult
import time


class GPUSimulator(Simulator):
    """
    GPU simulator

    Parameters
    ----------
    model : pysb.Model
        Model to simulate.
    tspan : vector-like, optional
        Time values over which to simulate. The first and last values define
        the time range. Returned trajectories are sampled at every value unless
        the simulation is interrupted for some reason, e.g., due to
        satisfaction
        of a logical stopping criterion (see 'tout' below).
    initials : vector-like or dict, optional
        Values to use for the initial condition of all species. Ordering is
        determined by the order of model.species. If not specified, initial
        conditions will be taken from model.initial_conditions (with
        initial condition parameter values taken from `param_values` if
        specified).
    param_values : vector-like or dict, optional
        Values to use for every parameter in the model. Ordering is
        determined by the order of model.parameters.
        If passed as a dictionary, keys must be parameter names.
        If not specified, parameter values will be taken directly from
        model.parameters.
    verbose : bool, optional (default: False)
        Verbose output.

    Attributes
    ----------
    verbose: bool
        Verbosity flag passed to the constructor.
    model : pysb.Model
        Model passed to the constructor.
    tspan : vector-like
        Time values passed to the constructor.
    """
    _supports = {'multi_initials': True, 'multi_param_values': True}

    def __init__(self, model, verbose=False, tspan=None, **kwargs):
        super(GPUSimulator, self).__init__(model, verbose, **kwargs)
        generate_equations(self._model)

        self.tout = None
        self.tspan = tspan
        self.verbose = verbose

        # private attribute
        self._blocks = None
        self._threads = None
        self._total_threads = 0
        self._parameter_number = len(self._model.parameters)
        self._species_number = len(self._model.species)
        self._step_0 = True
        self._code = self._pysb_to_cuda()
        self._ssa_all = None
        self._kernel = None
        self._param_tex = None
        self._ssa = None
        self._ssa_all = None

        device = os.getenv("CUDA_DEVICE")
        if device is None:
            self._device = 0
        else:
            self._device = int(device)

    def _pysb_to_cuda(self):
        """ converts pysb reactions to cuda compilable code

        """
        p = re.compile('\s')
        stoich_matrix = (_rhs(self._model) + _lhs(self._model)).T
        params_names = [g.name for g in self._model.parameters]
        # params_vals = [g.value for g in self._model.parameters]
        _reaction_number = len(self._model.reactions)

        stoich_string = ''
        for i in range(0, len(stoich_matrix[0])):
            for j in range(0, len(stoich_matrix)):
                stoich_string += "\t%s" % repr(stoich_matrix[j][i])
                if not (i == (len(stoich_matrix) - 1) and (
                    j == (len(stoich_matrix[0]) - 1))):
                    stoich_string += ','
            stoich_string += '\n'
        # stoich_string += ''
        hazards_string = ''
        for n, rxn in enumerate(self._model.reactions):
            hazards_string += "\th[%s] = " % repr(n)
            rate = sympy.fcode(rxn["rate"])
            for e in self._model.expressions:
                rate = re.sub(r'\b%s\b' % e.name,
                              '(' + sympy.ccode(e.expand_expr()) + ')', rate)
            # replace observables w/ sums of species
            for obs in self._model.observables:
                obs_string = ''
                for i in range(len(obs.coefficients)):
                    if i > 0:
                        obs_string += "+"
                    if obs.coefficients[i] > 1:
                        obs_string += str(obs.coefficients[i]) + "*"
                    obs_string += "__s" + str(obs.species[i])
                if len(obs.coefficients) > 1:
                    obs_string = '(' + obs_string + ')'
                rate = re.sub(r'\b(%s)\b' % obs.name, obs_string, rate)
            # rate = str(rxn["rate"])
            pattern = "(__s\d+)\*\*(\d+)"
            matches = re.findall(pattern, rate)
            for m in matches:
                repl = m[0]
                for i in range(1, int(m[1])):
                    repl += "*(%s-%d)" % (m[0], i)
                rate = re.sub(pattern, repl, rate)

            rate = re.sub(r'_*s(\d+)', lambda m: 'y[%s]' % (int(m.group(1))),
                          rate)
            for q, prm in enumerate(params_names):
                rate = re.sub(r'\b(%s)\b' % prm, 'tex2D(param_tex,%s,tid)' % q,
                              rate)
                # rate = re.sub(r'\b(%s)\b' % prm, '%s' % str(params_vals[q]), rate)
            rate = re.sub('d0', '', rate)
            rate = p.sub('', rate)
            rate = rate.replace('pow', 'powf')
            hazards_string += rate + ";\n"
        template_code = _load_template()
        cs_string = template_code.format(n_species=self._species_number,
                                         n_params=self._parameter_number,
                                         n_reactions=_reaction_number,
                                         hazards=hazards_string,
                                         stoch=stoich_string, )

        return cs_string

    def _compile(self, code):

        if self.verbose:
            print("Output cuda file to ssa_cuda_code.cu")
            with open("ssa_cuda_code.cu", "w") as source_file:
                source_file.write(code)
        self.compile_ssa(code)

    def _run(self, start_time, end_time, params, initials):
        """

        Parameters
        ----------
        start_time : np.float
            initial time point
        end_time : np.float
            time point to finish
        params : list_like
            param_values for simulation
        initials : list_like
            initial conditions to pass to model

        Returns
        -------

        """

        if self._step_0:
            self._setup(params)

            if self.verbose:
                self._print_verbose()

        n_simulations = len(params)

        species_matrix = self._create_gpu_init(initials=initials)

        # allocate space on GPU for results
        species_matrix_gpu = gpuarray.to_gpu(species_matrix)

        result = driver.managed_zeros(
                shape=(self._total_threads, self._species_number),
                dtype=np.int32, mem_flags=driver.mem_attach_flags.GLOBAL
        )
        # place starting time on GPU
        start_time = np.array(start_time, dtype=np.float32)
        start_time_gpu = gpuarray.to_gpu(start_time)

        # allocate and upload time to GPU
        last_time = np.zeros(n_simulations, dtype=np.float32)
        last_time_gpu = gpuarray.to_gpu(last_time)

        # stride of GPU, allows us to use a 1D array and index as a 2D
        a_stride = np.int32(species_matrix.strides[0])
        # run single step
        self._ssa(species_matrix_gpu, result, start_time_gpu, end_time,
                  last_time_gpu, a_stride, block=(self._threads, 1, 1),
                  grid=(self._blocks, 1))

        # Wait for kernel completion before host access
        pycuda.autoinit.context.synchronize()

        # retrieve and store results
        result = result[:n_simulations, :]

        current_time = last_time_gpu.get()[:n_simulations]
        return result, current_time

    def _run_all(self, timepoints, params, initial_conditions):

        # compile kernel and send parameters to GPU
        if self._step_0:
            self._setup(params)

        if self.verbose:
            self._print_verbose()

        n_simulations = len(params)
        n_results = np.int32(len(timepoints))

        species_matrix = self._create_gpu_init(initials=initial_conditions)
        species_matrix_gpu = gpuarray.to_gpu(species_matrix)

        # allocate space on GPU for results
        result = driver.managed_zeros(
                shape=(self._total_threads, n_results, self._species_number),
                dtype=np.int32, mem_flags=driver.mem_attach_flags.GLOBAL
        )

        # allocate and upload time to GPU
        time_points = np.array(timepoints, dtype=np.float32)
        time_points_gpu = gpuarray.to_gpu(time_points)

        # perform simulation
        self._ssa_all(species_matrix_gpu, result, time_points_gpu, n_results,
                      block=(self._threads, 1, 1), grid=(self._blocks, 1))

        # Wait for kernel completion before host access
        pycuda.autoinit.context.synchronize()

        # retrieve and store results, only keeping n_simulations
        return result[:n_simulations, :, :]

    def run_one_step(self, tspan=None, param_values=None, initials=None,
                     number_sim=0, threads=32):

        if param_values is None:
            # Run simulation using same param_values
            num_particles = int(number_sim)
            nominal_values = np.array(
                    [p.value for p in self._model.parameters])
            param_values = np.zeros((num_particles, len(nominal_values)),
                                    dtype=np.float32)
            param_values[:, :] = nominal_values
            self.param_values = param_values

        if initials is None:
            # Run simulation using same initial conditions
            species_names = [str(s) for s in self._model.species]
            initials = np.zeros(len(species_names))
            for ic in self._model.initial_conditions:
                initials[species_names.index(str(ic[0]))] = int(ic[1].value)
            initials = np.repeat([initials], param_values.shape[0], axis=0)
            self.initials = initials

        tout = len(param_values) * [None]
        for n in range(len(param_values)):
            tout[n] = tspan

        t_out = np.array(tspan, dtype=np.float32)

        if threads is None:
            self._threads = 128
        else:
            self._threads = threads

        if len(param_values) % self._threads == 0:
            self._blocks = len(param_values) / self._threads
        else:
            self._blocks = len(param_values) / self._threads + 1

        self._total_threads = self._blocks * self._threads
        timer_start = time.time()
        result = self._run_all(t_out, param_values, initials)
        timer_end = time.time()
        print("{} simulations in {}s".format(number_sim,
                                             timer_end - timer_start))

        return SimulationResult(self, tout, result)

    def run(self, tspan=None, param_values=None, initials=None, number_sim=1,
            threads=32, verbose=False):

        if param_values is None:
            # Run simulation using same param_values
            num_particles = int(number_sim)
            nominal_values = np.array(
                    [p.value for p in self._model.parameters])
            param_values = np.zeros((num_particles, len(nominal_values)),
                                    dtype=np.float32)
            param_values[:, :] = nominal_values
            self.param_values = param_values

        if initials is None:
            # Run simulation using same initial conditions
            species_names = [str(s) for s in self._model.species]
            initials = np.zeros(len(species_names))
            for ic in self._model.initial_conditions:
                initials[species_names.index(str(ic[0]))] = int(ic[1].value)
            initials = np.repeat([initials], param_values.shape[0], axis=0)
            self.initials = initials

        tout = len(param_values) * [None]

        for n in range(len(param_values)):
            tout[n] = tspan
        t_out = np.array(tout, dtype=np.float32)
        len_time = len(tspan)

        if threads is None:
            self._threads = 128
        else:
            self._threads = threads

        if len(param_values) % self._threads == 0:
            self._blocks = len(param_values) / self._threads
        else:
            self._blocks = len(param_values) / self._threads + 1

        self._total_threads = self._blocks * self._threads

        n_simulations = len(param_values)

        final_result = np.zeros(
                (n_simulations, len_time, self._species_number),
                dtype=np.int32)
        start_array = initials
        final_result[:, 0, :] = start_array
        start_time = t_out[:, 0]
        timer_start = time.time()
        for n, i in enumerate(tspan):
            if verbose:
                print('{} out of {}'.format(n, len_time))
            if n == 0:
                continue
            end = i
            result, end_time = self._run(start_time, end, param_values,
                                         start_array)
            t_out[:, n] = i
            start_time = end_time
            # print(i, start_time)
            final_result[:, n, :] = result
            start_array = result
        timer_end = time.time()
        print("{} simulations in {}s".format(n_simulations,
                                             timer_end - timer_start))

        return SimulationResult(self, tout, final_result)

    def compile_ssa(self, code):
        """ compile the SSA code

        Parameters
        ----------
        code : str
            code to compile


        """
        self._kernel = pycuda.compiler.SourceModule(code, nvcc="nvcc",
                                                    no_extern_c=True)
        self._param_tex = self._kernel.get_texref("param_tex")
        self._ssa = self._kernel.get_function("Gillespie_one_step")
        self._ssa_all = self._kernel.get_function("Gillespie_all_steps")

    def _print_verbose(self):
        print("threads = {}\n_blocks = {}"
              "".format(self._threads, self._blocks))
        print("Kernel Memory\n\tlocal = {}  \n\tshared = {}  \n"
              "registers {} ".format(self._ssa.local_size_bytes,
                                     self._ssa.shared_size_bytes,
                                     self._ssa.num_regs))
        occ = tools.OccupancyRecord(tools.DeviceData(),
                                    threads=self._threads,
                                    shared_mem=self._ssa.shared_size_bytes,
                                    registers=self._ssa.num_regs)
        print("tb_per_mp = {} \nlimit = {} \noccupancy = {}\n"
              "warps_per_mp = {}\ntb_per_mp_limits {}"
              "".format(occ.tb_per_mp, occ.limited_by, occ.occupancy,
                        occ.warps_per_mp, occ.tb_per_mp_limits))

    def _setup(self, params):
        self._compile(self._code)
        param = np.zeros((self._total_threads, self._parameter_number),
                         dtype=np.float32)
        try:
            for i in range(len(params)):
                for j in range(self._parameter_number):
                    param[i][j] = params[i][j]
        except IndexError:
            pass
        ary = _create_2D_array(param)
        _copy2D_host_to_array(ary, param, self._parameter_number * 4,
                              self._total_threads)
        self._param_tex.set_array(ary)
        self._step_0 = False

    def _create_gpu_init(self, initials):

        # Create species matrix on GPU
        # will make according to number of total threads, not n_simulations
        species_matrix = np.zeros((self._total_threads, self._species_number),
                                  dtype=np.int32)
        # Filling species matrix
        # Note that this might not fill entire array that was created.
        # The rest of the array will be zeros to fill up GPU.
        for i in range(len(initials)):
            for j in range(self._species_number):
                species_matrix[i][j] = initials[i][j]
        return species_matrix

    @staticmethod
    def get_gpu_settings(parameters):
        """
        Gathers optimal number of _threads per block given size of parameters
        :return _blocks, _threads
        """
        max_threads = tools.DeviceData().max_threads
        max_threads = 256
        warp_size = tools.DeviceData().warp_size
        max_warps = max_threads / warp_size
        threads = max_warps * warp_size
        if len(parameters) % threads == 0:
            blocks = len(parameters) / threads
        else:
            blocks = len(parameters) / threads + 1
        return blocks, threads


def _create_2D_array(matrix):
    tmp_array = driver.ArrayDescriptor()
    tmp_array.width = matrix.shape[1]
    tmp_array.height = matrix.shape[0]
    tmp_array.format = driver.dtype_to_array_format(matrix.dtype)
    tmp_array.num_channels = 1
    tmp_array.flags = 0
    ary = driver.Array(tmp_array)
    return ary


def _copy2D_host_to_array(arr, host, width, height):
    tmp_array = driver.Memcpy2D()
    tmp_array.set_src_host(host)
    tmp_array.set_dst_array(arr)
    tmp_array.height = height
    tmp_array.width_in_bytes = tmp_array.src_pitch = width
    tmp_array.height = height
    tmp_array(aligned=True)


def _lhs(model):
    """
    Left hand side
    """
    left_side = np.zeros((len(model.reactions), len(model.species)),
                         dtype=np.int32)
    for i in range(len(model.reactions)):
        for j in range(len(model.species)):
            stoich = 0
            for k in model.reactions[i]['reactants']:
                if j == k:
                    stoich += 1
            left_side[i, j] = stoich
    return left_side * -1


def _rhs(model):
    """

    Right hand side of matrix

    """
    right_side = np.zeros((len(model.reactions), len(model.species)),
                          dtype=np.int32)
    for i in range(len(model.reactions)):
        for j in range(len(model.species)):
            stoich = 0
            for k in model.reactions[i]['products']:
                if j == k:
                    stoich += 1
            right_side[i, j] = stoich
    return right_side


def _load_template():
    with open(os.path.join(os.path.dirname(__file__),
                           'pycuda_templates',
                           'gillespie_template.cu'), 'r') as f:
        gillespie_code = f.read()
    return gillespie_code