from collections import defaultdict
from copy import deepcopy
from pathlib import Path

import numpy as np
from attrs import define, field, validators
from joblib import Parallel, delayed

from sva.monty.json import MSONable, load_anything
from sva.utils import seed_everything

from . import DynamicExperiment
from .base import ExperimentMixin


@define
class PolicyPerformanceEvaluator(MSONable):
    """
    Parameters
    ----------
    n_look_ahead : int
        The number of steps to take in each simulated experiments (the
        number of experiments to run/new data points to sample).
    n_dreams : int
        The number of samples from the gp fit on the original data to run
        the simulations over.
    seed : int, optional
        the seed for the campaign. this is required since multiprocessing
        does not pass the generator state to each of the downstream tasks.
        this is one of the few instances where utils.seed_everything will
        be called internally.
    """

    experiment = field()

    @experiment.validator
    def valid_experiment(self, _, value):
        if not issubclass(value.__class__, ExperimentMixin):
            raise ValueError("Provided experiment must inherit ExperimentMixin")

    history = field(factory=list)

    checkpoint_dir = field(
        default=None,
        validator=validators.optional(validators.instance_of((Path, str))),
    )

    n_look_ahead = field(default=5, validator=validators.instance_of(int))
    n_dreams = field(default=50, validator=validators.instance_of(int))
    seed = field(default=123, validator=validators.instance_of(int))

    def __attrs_post_init__(self):
        if self.checkpoint_dir is not None:
            self.checkpoint_dir = Path(self.checkpoint_dir)
            self.checkpoint_dir.mkdir(exist_ok=True, parents=True)

    @staticmethod
    def _run_job(job):
        """Helper method for multiprocessing in the main function."""

        # Check to see if the job exists already
        ckpt_name = job["checkpoint_path"]
        if ckpt_name is not None:
            if Path(ckpt_name).exists():
                return load_anything(ckpt_name)

        seed = job["job_seed"]
        if seed is not None:
            seed_everything(seed)

        experiment = job["experiment"]
        parameters = job["parameters"]
        n = job["n_look_ahead"]

        experiment.run(n, parameters, pbar=False, additional_experiments=True)
        if ckpt_name is not None:
            experiment.save(ckpt_name)

        # Required for multiprocessing
        return experiment

    def _get_name_from_job(self, job):
        parameters = job["parameters"]
        job_seed_str = str(job["job_seed"])
        hash = parameters.get_hash()
        return f"{hash}-seed-{job_seed_str}"

    def process_results(self):
        """Processes the saved history (or provided results) into a
        statistical analysis. Results are grouped by the combination of
        acquisition function signature and its keyword arguments (all json-
        serialized).

        Parameters
        ----------
        results : list
            The results to process. If None, uses the results saved in the
            history. Generally, leaving this as None is probably the correct
            way to go.

        Returns
        -------
        dict
        """

        tmp0 = defaultdict(list)
        for exp in self.history:
            # Get some string representation of the combination of the
            # acquisition function and its keyword arguments for grouping
            # the results together
            parameters = exp.metadata["runtime_properties"][-1]
            tmp0[parameters.acqf_key].append(exp)

        results_dict = {}
        for key, policy_results in tmp0.items():
            tmp2 = []
            for exp in policy_results:
                # Find the x coordinate corresponding to the maximum y-value
                # in the experiment
                x_star = exp.metadata["optima"]["next_points"].numpy()

                # Take that x coordinate and find its corresponding value.
                # Note that we should not take the result directly from the
                # optima "value" key, since this is likely scaled by some
                # output scaler. The following line gets the unscaled value
                # directly.
                y_star, _ = exp(x_star)
                y_star = y_star.squeeze()

                tmp = []

                # For each step in that experiment's history
                for step in exp.history:
                    # Find the x coordinate of the next point, which will
                    # depend on the acquisition function of the experiment
                    x_step_star = step["optimize_gp"]["next_points"].numpy()

                    # Get the corresponding y value
                    y_step_star, _ = exp(x_step_star)
                    y_step_star = y_step_star.squeeze()

                    # Calculate the normalized opportunity cost of this value
                    cost = np.abs(y_star - y_step_star) / np.abs(y_star)
                    tmp.append(cost)
                tmp2.append(tmp)

            res = np.array(tmp2)
            results_dict[key] = {"results": res}

            # Get the learning rate of an exponential fit to the opportunity
            # cost as a function of iteration
            x = np.arange(res.shape[1])
            y = np.median(res, axis=0)
            p = np.polyfit(x, np.log10(y), deg=1)
            learning_rate = p[0]
            results_dict[key]["learning_rate"] = learning_rate

        return results_dict

    def get_best_policy(self):
        policy_results = self.process_results()

        # results_dict[key]["learning_rate"] = learning_rate
        r = [
            (key, value["learning_rate"])
            for key, value in policy_results.items()
        ]
        r.sort(key=lambda x: x[1])
        best_policy_method = r[0]
        policy = best_policy_method[0]
        learning_rate = best_policy_method[1]
        if policy == "EI":
            kwargs = None
        else:
            policy, beta = policy.split("-")
            beta = float(beta)
            kwargs = {"beta": beta}
        d = {
            "method": policy,
            "kwargs": kwargs,
            "learning_rate": learning_rate,
        }
        return d, r

    def run(self, parameter_list, n_jobs=12):
        """Runs a policy performance evaluation on the experiment provided
        at initialization. Results are saved in the history attribute.

        Parameters
        ----------
        parameter_list : list
            A list of CampaignParameter objects used for the policy performance
            evaluation.
        n_jobs : int
            number of parallel multiprocessing jobs to use at a time.
        """

        jobs = []
        existing_names = [job["name"] for job in self.history]
        experiment = deepcopy(self.experiment)

        for parameters in parameter_list:
            for dream_index in range(self.n_dreams):
                # Get the current seed and seed the current dreamed experiment
                # if the seed is provided
                job_seed = None
                if self.seed is not None:
                    job_seed = self.seed + dream_index
                    seed_everything(job_seed)

                # Get the dreamed experiment
                dream_experiment = DynamicExperiment.from_data(
                    deepcopy(experiment.data.X),
                    deepcopy(experiment.data.Y),
                    experiment.properties.experimental_domain,
                )

                # This is required for policy performance evaluation
                if parameters.optimize_gp is None:
                    parameters.set_optimize_gp_default()

                # Setup the job payload
                job = {
                    "job_seed": job_seed,
                    "dream_index": dream_index,
                    "experiment": dream_experiment,
                    "parameters": parameters,
                    "n_look_ahead": self.n_look_ahead,
                }
                name = self._get_name_from_job(job)
                job["name"] = name
                job["checkpoint_path"] = None
                if self.checkpoint_dir is not None:
                    ckpt_name = f"{name}.json"
                    job["checkpoint_path"] = self.checkpoint_dir / Path(
                        ckpt_name
                    )

                # We don't want to repeat already completed jobs
                if job["name"] not in existing_names:
                    jobs.append(job)

        results = Parallel(n_jobs=n_jobs)(
            delayed(self._run_job)(job) for job in jobs
        )
        self.history.extend(results)
