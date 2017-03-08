from pysb.simulator.base import Simulator, SimulationResult
from pysb.bng import generate_equations, _parse_netfile, BngFileInterface
import numpy as np
import os


class BioNetGenSSASimulator(Simulator):
    _supports = {
        'multi_initials':     True,
        'multi_param_values': True
    }

    def __init__(self, model, tspan=None, cleanup=True, verbose=False):
        super(BioNetGenSSASimulator, self).__init__(model, tspan=tspan,
                                                    verbose=verbose)
        self.cleanup = cleanup
        self._outdir = None
        generate_equations(self._model,
                           cleanup=self.cleanup,
                           verbose=self.verbose)

    def run(self, tspan=None, initials=None, param_values=None, n_sim=1,
            output_dir=None, output_file_basename=None, cleanup=True,
            verbose=False, **additional_args):
        """
        Simulate a model with BNG's SSA simulator and return the trajectories.

        Parameters
        ----------
        tspan: vector-like
            time span of simulation
        initials: vector-like, optional
            initial condtions of model
        param_values : vector-like or dictionary, optional
                Values to use for every parameter in the model. Ordering is
                determined by the order of model.parameters.
                If not specified, parameter values will be taken directly from
                model.parameters.
        n_sim: int, optional
            number of simulations to run
        output_dir : string, optional
            Location for temporary files generated by BNG. If None (the
            default), uses a temporary directory provided by the system. A
            temporary directory with a random name is created within the
            supplied location.
        output_file_basename : string, optional
            This argument is used as a prefix for the temporary BNG
            output directory, rather than the individual files.
        cleanup : bool, optional
            If True (default), delete the temporary files after the simulation is
            finished. If False, leave them in place. Useful for debugging.
        verbose: bool, optional
            If True, print BNG screen output.
        additional_args: kwargs, optional
            Additional arguments to pass to BioNetGen

        """
        super(BioNetGenSSASimulator, self).run(tspan=tspan,
                                               initials=initials,
                                               param_values=param_values)
        if param_values is None:
            # Run simulation using same param_values
            num_particles = int(n_sim)
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

        additional_args['method'] = 'ssa'
        additional_args['t_end'] = np.max(self.tspan)
        additional_args['n_steps'] = len(self.tspan)
        additional_args['verbose'] = verbose

        # TODO set parameters and initials per simulation if not the same
        # if param_values is not None:
        #     if len(param_values) != len(self._model.parameters):
        #         raise Exception("param_values must be the same length as model.parameters")
        #     for i in range(len(param_values)):
        #         self._model.parameters[i].value = param_values[i]

        with BngFileInterface(self._model, verbose=verbose,
                              output_dir=output_dir,
                              output_prefix=output_file_basename,
                              cleanup=cleanup) as bngfile:
            bngfile.action('generate_network', overwrite=True, verbose=verbose)
            bngfile.action('saveConcentrations')
            if output_file_basename is None:
                prefix = 'pysb'
            else:
                prefix = output_file_basename
            for i in range(n_sim):
                tmp = additional_args.copy()
                tmp['prefix'] = prefix + str(i)
                bngfile.action('simulate', **tmp)
                bngfile.action('resetConcentrations')

            bngfile.execute()
            tout, yout = read_multi_simulation_results(n_sim,
                                                       bngfile.base_filename)

        return SimulationResult(self, tout=tout, trajectories=yout)


def read_multi_simulation_results(n_sims, base_filename):
    """
    Reads the results of a BNG simulation and parses them into a numpy
    array
    """
    # Read concentrations data

    trajectories = [None] * n_sims
    tout = []
    # load the data

    for n in range(n_sims):
        filename = base_filename + str(n) + '.cdat'
        if not os.path.isfile(filename):
            raise Exception("Cannot find input file " + filename)
        data = np.loadtxt(filename, skiprows=1)
        # if nfsim:
        #     gdat_arr = numpy.loadtxt(self.base_filename + str(i) + '.gdat',
        #                              skiprows=1)
        # store data
        tout.append(data[:, 0])
        trajectories[n] = data[:, 1:]
    return np.array(tout), np.array(trajectories)