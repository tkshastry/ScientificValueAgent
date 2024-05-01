from pathlib import Path

import hydra
from hydra.utils import instantiate
from joblib import Parallel, delayed
from omegaconf import OmegaConf

from sva import __version__
from sva.utils import Timer, seed_everything


def _get_name(experiment, config, params, seed):
    experiment_name = experiment.name
    if config["use_full_hashes"]:
        parameters_name = f"{params.name}-{params.get_hash()}"
    else:
        parameters_name = params.name

    root = (
        Path(config["paths"]["output_dir"]) / experiment_name / parameters_name
    )
    return root / f"{seed}.json"


def _run_job(job):
    with Timer() as timer:
        experiment = job["experiment"]
        config = job["config"]
        params = job["parameters"]
        seed = job["seed"]
        n = config["n"]
        additional_experiments = config["additional_experiments"]
        pbar = config["pbar"]

        seed_everything(seed)
        experiment = experiment()
        experiment.run(
            n=n,
            parameters=params,
            additional_experiments=additional_experiments,
            pbar=pbar,
        )

        path = _get_name(experiment, config, params, seed)
        experiment.save(path, json_kwargs={"indent": 4, "sort_keys": True})

    print(f"done with exp: {path} in {timer.dt:.02f} s", flush=True)


def run_single_policy(config):
    """Runs a set of experiments and parameters under a single policy. In
    other words, the acquisition function used at the start of an experiment
    will not change throughout."""

    experiment = instantiate(config.experiment)

    parameters = instantiate(config.campaign_parameters, _convert_="partial")
    if not isinstance(parameters, list):
        parameters = [parameters]

    jobs = []
    config_resolved = OmegaConf.to_container(config, resolve=True)
    for params in parameters:
        for unique_seed in range(config.replicas):
            jobs.append(
                {
                    "experiment": experiment,
                    "parameters": params,
                    "config": config_resolved,
                    "seed": config.seed + unique_seed,
                }
            )
    Parallel(n_jobs=config.n_jobs)(delayed(_run_job)(job) for job in jobs)


def run_dynamic_policy(config):
    cc = "partial"
    experiment = instantiate(config.experiment)
    parameters = instantiate(config.campaign_parameters, _convert_=cc)
    ppt = instantiate(config.policy_performance_tuner)
    parameters_ppe = instantiate(config.candidate_parameters, _convert_=cc)

    root = config["paths"]["output_dir"]

    for unique_seed in range(config.replicas):
        # Seed the initial experiment and create it from the factory
        seed = config.seed + unique_seed
        seed_everything(seed)
        tmp_experiment = experiment()

        print(f":: Starting experiment at seed {seed}")

        # Here's the true experiment loop
        for ii in range(config["n"]):
            with Timer() as timer:
                # First, we figure out which acquisition function to use
                # the policy performance tuner is also a factory
                # the number of look aheads and dreams is pre-specified
                policy_evaluator = ppt(tmp_experiment, seed=seed * 1000 + ii)
                policy_evaluator.run(
                    parameter_list=parameters_ppe, n_jobs=config.n_jobs
                )
                d = policy_evaluator.get_best_policy()
                lr = d.pop("learning_rate")

                # Get the next parameters
                # The parameters() is a factory
                tmp_parameters = parameters(
                    acqf="EI" if d["kwargs"] is None else d["kwargs"]["beta"]
                )
                tmp_experiment.run(
                    n=1,
                    parameters=tmp_parameters,
                    pbar=False,
                    additional_experiments=True,
                )
                next_point = tmp_experiment.data.X[-1, :]

            print(
                f"({ii:02}) Best policy: {d} with LR={lr:.02f} "
                f"next={next_point} finished in {timer.dt:.02f} s",
                flush=True,
            )

        root = root / tmp_experiment.name / f"{seed}.json"
        tmp_experiment.save(root, json_kwargs={"indent": 4, "sort_keys": True})


@hydra.main(version_base="1.3", config_path="configs", config_name="core.yaml")
def hydra_main(config):
    """Executes training powered by Hydra, given the configuration file. Note
    that Hydra handles setting up the config.

    Parameters
    ----------
    config : omegaconf.DictConfig
    """

    print()
    print(f"Running sva version {__version__}")
    if config.name is None:
        raise ValueError("name must be set")
    print(f"name set to: {config.name}")
    print(f"seed set to: {config.seed}")
    print()

    if "policy_performance_tuner" in config.keys():
        run_dynamic_policy(config)
    else:
        run_single_policy(config)


def entrypoint():
    hydra_main()