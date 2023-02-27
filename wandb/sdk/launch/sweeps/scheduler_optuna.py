import base64
from collections import defaultdict
import logging
import pprint
import queue
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import types
import importlib.machinery

import optuna

import wandb
from wandb.sdk.launch.sweeps import SchedulerError
from wandb.sdk.launch.sweeps.scheduler import (
    LOG_PREFIX,
    RunState,
    Scheduler,
    SweepRun,
)
from wandb.wandb_agent import _create_sweep_command_args

from wandb.apis.public import QueuedRun, Api as PublicApi
from wandb.sdk.internal.internal_api import Api as InternalApi
from wandb.sdk.wandb_run import Run

logger = logging.getLogger(__name__)

optuna.logging.set_verbosity(optuna.logging.WARNING)


@dataclass
class _Worker:
    agent_config: Dict[str, Any]
    agent_id: str


class OptunaScheduler(Scheduler):
    def __init__(
        self,
        *args: Any,
        num_workers: int = 2,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self._workers: Dict[int, _Worker] = {}
        self._num_workers: int = 2 or num_workers

        self._public_api = PublicApi()
        self._api = InternalApi()

        self.study: optuna.study.Study = None

        # user provided params
        # TODO: remove from class, combine with create_study func
        self._trial_func = None
        self._pruner = None
        self._sampler = None
        self._study = None

        self._storage_name = ""
        self._artifact_name = "optuna-scheduler"
        self._study_name = None
        self._run_trials = {}
        self._job_queue: "queue.Queue[SweepRun]" = queue.Queue()
        self._metric_history = defaultdict(int)

        self._wandb_run: Run = wandb.init(name=f"sweep-scheduler-{self._sweep_id}")
        self._load_db()
        self._load_optuna_artifact()

    def _load_optuna_artifact(self):
        if self._sweep_config.get("optuna_artifact"):
            artifact = self._wandb_run.use_artifact(
                self._sweep_config.get("optuna_artifact"), type="optuna"
            )
            if artifact:
                wandb.termlog(f"{LOG_PREFIX}Downloading optuna.py artifact: {artifact}")
                path = artifact.download()

                objective_path = f"{path}/optuna.py"
                try:
                    loader = importlib.machinery.SourceFileLoader(
                        "optuna", objective_path
                    )
                    mod = types.ModuleType(loader.name)
                    loader.exec_module(mod)
                    
                    # Set objective function
                    self._trial_func = self._make_trial_from_objective
                    self._objective_func = mod.objective
                    if mod.study:
                        wandb.termlog(f"{LOG_PREFIX}Identified user-provided study, ignoring pruner and sampler")
                        self._study = mod.study()
                    else:
                        # Set other optuna objects
                        self._pruner = mod.pruner()
                        self._sampler = mod.sampler()

                except Exception as e:
                    wandb.termwarn(f"failed to load objective function: {str(e)}")
                    raise e

            else:
                wandb.termlog(
                    f"{LOG_PREFIX}Failed to load: {self._sweep_config.get('optuna_artifact')}"
                )

                # defaults for optuna ops, NOT user provided
                self._trial_func = self._make_trial
        return None

    def _load_db(self):
        """
        Create an optuna study with a sqlite backened for loose state management
        """
        if self._study:
            self.study = self._study
            return

        # TODO(gst): add to validate function to confirm this exists, warn user
        if not self._sweep_config.get("optuna_study_name"):
            self._study_name = f"optuna-study-{self._sweep_id}"

        pruner_args = self._sweep_config.get("pruner", {})
        pruner = self._pruner or self._make_optuna_pruner(pruner_args)

        if self._wandb_run.resumed:
            # our scheduler was resumed, try to load state
            storage = self._wandb_run.use_artifact(self._artifact_name)
            storage.download()
            self._storage_name = storage
        else:
            self._storage_name = f"{self._study_name}.db"

        wandb.termlog(
            f"{LOG_PREFIX}Creating optuna study with direction: {self._sweep_config.get('metric', {}).get('goal')}"
        )
        self.study = optuna.create_study(
            study_name=self._study_name,
            storage=f"sqlite:///{self._storage_name}",
            pruner=pruner,
            sampler=self._sampler,
            load_if_exists=True,
            direction=self._sweep_config.get("metric", {}).get("goal"),
        )

    def _save_scheduler_state(self) -> None:
        scheduler_artifact = wandb.Artifact(self._artifact_name, type="scheduler")
        scheduler_artifact.add_file(f".{self._storage_name}")
        self._wandb_run.log_artifact(scheduler_artifact)

    def _start(self) -> None:
        for worker_id in range(self._num_workers):
            wandb.termlog(f"{LOG_PREFIX}Starting AgentHeartbeat worker {worker_id}\n")
            agent_config = self._api.register_agent(
                f"{socket.gethostname()}-{worker_id}",  # host
                sweep_id=self._sweep_id,
                project_name=self._project,
                entity=self._entity,
            )
            self._workers[worker_id] = _Worker(
                agent_config=agent_config,
                agent_id=agent_config["id"],
            )

    def _heartbeat(self, worker_id: int) -> None:
        # Make sure Scheduler is alive
        if not self.is_alive():
            return

        if self._job_queue.empty() and len(self._runs) < self._num_workers:
            config, trial = self._trial_func()
            run = self._api.upsert_run(
                project=self._project,
                entity=self._entity,
                sweep_name=self._sweep_id,
                config=config,
            )[0]

            run_id = (
                base64.b64decode(bytes(run["id"].encode("utf-8")))
                .decode("utf-8")
                .split(":")[2]
            )

            run = SweepRun(
                id=run_id,
                args=config,
                worker_id=worker_id,
            )
            self._run_trials[run.id] = trial
            self._runs[run.id] = run
            self._job_queue.put(run)

    def _run(self) -> None:
        """
        Poll currently known runs for new metrics
        report new metrics to optuna
        send kill signals to existing runs if pruned
        hearbeat workers with backend
        create new runs if necessary from optuna suggestions
        launch new runs
        """
        to_kill = self._poll_running_runs()
        for run_id in to_kill:
            del self._run_trials[run_id]
            self._stop_run(run_id)

        for worker_id in self._workers:
            self._heartbeat(worker_id)

        try:
            srun: SweepRun = self._job_queue.get(timeout=self._queue_timeout)
        except queue.Empty:
            wandb.termlog(f"{LOG_PREFIX}No jobs in Sweeps RunQueue, waiting...")
            time.sleep(self._queue_sleep)
            return

        # If run is already stopped just ignore the request
        if srun.state in [
            RunState.DEAD,
            RunState.UNKNOWN,
        ]:
            return

        wandb.termlog(
            f"{LOG_PREFIX}Converting Sweep Run (RunID:{srun.id}) to Launch Job"
        )

        # send to launchs
        command = _create_sweep_command_args({"args": srun.args})["args_dict"]
        self._add_to_launch_queue(
            run_id=srun.id,
            config={"overrides": {"run_config": command}},
        )

    def _get_run_history(self, run_id):
        launched_run_path = f"{self._entity}/{self._project}/{run_id}"
        if run_id in self._runs:
            # run was killed upstream
            queued_run: QueuedRun = self._runs[run_id].queued_run
            if queued_run.state == "pending":
                return [], False
            else:
                queued_run.wait_until_running()

        try:
            api_run: Run = self._public_api.run(launched_run_path)
            finished = False
        except Exception:
            finished = True

        metric_name = self._sweep_config["metric"]["name"]
        history = api_run.scan_history(keys=["_step", metric_name])
        finished = finished or api_run.state == "finished"

        return [x[metric_name] for x in history], finished

    def _poll_running_runs(self):
        wandb.termlog(f"{LOG_PREFIX}Polling runs for metrics.")
        to_kill = []
        for run_id, trial in self._run_trials.items():
            # poll metrics, feed into optuna
            metrics, run_finished = self._get_run_history(run_id)
            last_metric_idx = self._metric_history[run_id]
            for i, metric in enumerate(metrics[last_metric_idx:]):
                wandb.termlog(
                    f"{LOG_PREFIX}Run: {run_id} | logging new {metric=} (step: {i+last_metric_idx})"
                )
                trial = self._run_trials[run_id]
                trial.report(metric, last_metric_idx + i)
                self._metric_history[run_id] = len(metrics)

                # ask optuna if we should prune the run
                if trial.should_prune():
                    wandb.termlog(f"{LOG_PREFIX}Optuna pruning run: {run_id}")
                    self.study.tell(trial, state=optuna.trial.TrialState.PRUNED)
                    to_kill += [run_id]
                    break

            if run_finished:
                self.study.tell(trial, state=optuna.trial.TrialState.COMPLETE)
                wandb.termlog(
                    f"{LOG_PREFIX}Run finished. Currenty study state: {self.study.trials}"
                )
                to_kill += [run_id]
        return to_kill

    def _make_trial(self):
        trial = self.study.ask()
        config = defaultdict(dict)
        for param, extras in self._sweep_config["parameters"].items():
            if values := extras.get("values"):  # categorical
                config[param]["value"] = trial.suggest_categorical(param, values)
            elif value := extras.get("value"):
                config[param]["value"] = trial.suggest_categorical(param, [value])
            elif type(extras.get("min")) == float:
                log = "log" in param
                config[param]["value"] = trial.suggest_float(
                    param, extras.get("min"), extras.get("max"), log=log
                )
            elif type(extras.get("min")) == int:
                log = "log" in param
                config[param]["value"] = trial.suggest_int(
                    param, extras.get("min"), extras.get("max"), log=log
                )
            else:
                print(f"{LOG_PREFIX}Unknown parameter type, help! {param=}, {extras=}")
        return config, trial

    def _make_trial_from_objective(self):
        wandb.termlog(f"{LOG_PREFIX}Making trial params from objective func")
        study_copy = optuna.create_study()
        study_copy.add_trials(self.study.trials)
        try:
            study_copy.optimize(self._objective_func, n_trials=1, timeout=2)
        except TimeoutError:
            raise Exception(
                "Passed optuna objective functions cannot actually train. Must execute in 2 seconds. See docs."
            )

        temp_trial = study_copy.trials[-1]
        config = defaultdict(dict)
        for param, value in temp_trial.params.items():
            config[param]["value"] = value

        new_trial = self.study.ask(fixed_distributions=temp_trial.distributions)

        return config, new_trial

    def _make_optuna_pruner(self, pruner_args: Dict, epochs: Optional[int] = 100):
        type_ = pruner_args.get("type")
        if not type_:
            wandb.termwarn(
                f"{LOG_PREFIX}No pruner selected, using Optuna default median pruner"
            )
            return None
        elif type_ == "HyperbandPruner":
            wandb.termlog(f"{LOG_PREFIX}Using the optuna HyperbandPruner")
            return optuna.pruners.HyperbandPruner(
                min_resource=pruner_args.get("min_resource", 1),
                max_resource=epochs,
                reduction_factor=pruner_args.get("reduction_factor", 3),
            )
        elif type_ == "SuccessiveHalvingPruner":
            wandb.termlog(f"{LOG_PREFIX}Using the optuna SuccessiveHalvingPruner")
            return optuna.pruners.SuccessiveHalvingPruner(
                min_resource=pruner_args.get("min_resource", 1),
                reduction_factor=pruner_args.get("reduction_factor", 3),
            )
        else:
            wandb.termwarn(f"Pruner: {type_} not yet supported.")

    def _exit(self):
        pass
