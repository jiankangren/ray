from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time

import numpy as np
import pickle
import tensorflow as tf
from tensorflow.python import debug as tf_debug

import ray
from ray.tune.result import TrainingResult
from ray.tune.trial import Resources
from ray.rllib.agent import Agent
from ray.rllib.utils import FilterManager
from ray.rllib.feudal.feudal_evaluator import FeudalEvaluator
from ray.rllib.feudal.rollout import collect_samples
from ray.rllib.feudal.utils import log_histogram


DEFAULT_CONFIG = {
    # Discount factor of the MDP
    "gamma": 0.995,
    # Number of steps after which the rollout gets cut
    "horizon": 2000,
    # GAE(lambda) parameter
    "lambda": 1.0,
    # Initial coefficient for KL divergence
    "kl_coeff": 0,
    # Number of SGD iterations in each outer loop
    "num_sgd_iter": 30,
    # Stepsize of SGD
    "sgd_stepsize": 5e-5,
    # TODO(pcm): Expose the choice between gpus and cpus
    # as a command line argument.
    "devices": ["/cpu:%d" % i for i in range(4)],
    "tf_session_args": {
        "device_count": {"CPU": 4},
        "log_device_placement": False,
        "allow_soft_placement": True,
        "intra_op_parallelism_threads": 1,
        "inter_op_parallelism_threads": 2,
    },
    # Batch size for policy evaluations for rollouts
    "rollout_batchsize": 1,
    # Total SGD batch size across all devices for SGD
    "sgd_batchsize": 128,
    # Coefficient of the value function loss
    "manager_vf_loss_coeff": 1.0,
    "worker_vf_loss_coeff": 1.0,
    # Coefficient of the entropy regularizer
    "entropy_coeff": 0.0,
    # PPO clip parameter
    "clip_param": 0.3,
    # Target value for KL divergence
    "kl_target": 0.01,
    # Config params to pass to the models
    "model_manager_filter": {"conv_filters": [[16, [8, 8], 4],[32, [4, 4], 2]], "fcnet_hiddens": [256], "fcnet_activation": "relu"},
    "model_manager_VF": {"free_log_std": False, "fcnet_hiddens": [256], "fcnet_activation": "relu"},
    "model_worker_VF": {"free_log_std": False, "fcnet_hiddens": [256], "fcnet_activation": "relu"},
    "model_manager_goal": {"free_log_std": False, "fcnet_hiddens": [32, 32], "fcnet_activation": "tanh"},
    "model_worker_goal": {"free_log_std": False, "fcnet_hiddens": [32, 32], "fcnet_activation": "tanh"},
    # Which observation filter to apply to the observation
    "observation_filter": "MeanStdFilter",
    "reward_clipping": 1.0,
    # If >1, adds frameskip
    "extra_frameskip": 1,
    # Number of timesteps collected in each outer loop
    "timesteps_per_batch": 4000,
    # Each tasks performs rollouts until at least this
    # number of steps is obtained
    "min_steps_per_task": 200,
    # Number of actors used to collect the rollouts
    "num_workers": 5,
    # Whether to allocate GPUs for workers (if > 0).
    "num_gpus_per_worker": 0,
    # Whether to allocate CPUs for workers (if > 0).
    "num_cpus_per_worker": 1,
    # Dump TensorFlow timeline after this many SGD minibatches
    "full_trace_nth_sgd_batch": -1,
    # Whether to profile data loading
    "full_trace_data_load": False,
    # Outer loop iteration index when we drop into the TensorFlow debugger
    "tf_debug_iteration": -1,
    # If this is True, the TensorFlow debugger is invoked if an Inf or NaN
    # is detected
    "tf_debug_inf_or_nan": False,
    # If True, we write tensorflow logs and checkpoints
    "write_logs": True,
    # Arguments to pass to the env creator
    "env_config": {},
    # HYPERPARAMETERS OF THE HRL ARCHITECTURE:
    "g_dim": 16,
    "k_dim": 16,
    "tradeoff_coeff": 0.5,
    "gamma_internal": 0.95,
    "c": 10

}


class FeudalAgent(Agent):
    _agent_name = "Feudal"
    _allow_unknown_subkeys = ["model", "tf_session_args", "env_config"]
    _default_config = DEFAULT_CONFIG

    @classmethod
    def default_resource_request(cls, config):
        cf = dict(cls._default_config, **config)
        return Resources(
            cpu=1,
            gpu=len([d for d in cf["devices"] if "gpu" in d.lower()]),
            extra_cpu=cf["num_cpus_per_worker"] * cf["num_workers"],
            extra_gpu=cf["num_gpus_per_worker"] * cf["num_workers"])

    def _init(self):
        self.global_step = 0
        self.kl_coeff = self.config["kl_coeff"]
        self.local_evaluator = FeudalEvaluator(
            self.registry, self.env_creator, self.config, self.logdir, False)
        RemoteFeudalEvaluator = ray.remote(
            num_cpus=self.config["num_cpus_per_worker"],
            num_gpus=self.config["num_gpus_per_worker"])(FeudalEvaluator)
        self.remote_evaluators = [
            RemoteFeudalEvaluator.remote(
                self.registry, self.env_creator, self.config, self.logdir,
                True)
            for _ in range(self.config["num_workers"])]
        self.start_time = time.time()
        if self.config["write_logs"]:
            self.file_writer = tf.summary.FileWriter(
                self.logdir, self.local_evaluator.sess.graph)
        else:
            self.file_writer = None
        self.saver = tf.train.Saver(max_to_keep=None)

    def _train(self):
        agents = self.remote_evaluators
        config = self.config
        model = self.local_evaluator

        if (config["num_workers"] * config["min_steps_per_task"] >
                config["timesteps_per_batch"]):
            print(
                "WARNING: num_workers * min_steps_per_task > "
                "timesteps_per_batch. This means that the output of some "
                "tasks will be wasted. Consider decreasing "
                "min_steps_per_task or increasing timesteps_per_batch.")

        print("===> iteration", self.iteration)

        iter_start = time.time()
        weights = ray.put(model.get_weights())
        [a.set_weights.remote(weights) for a in agents]
        samples = collect_samples(agents, config, self.local_evaluator)

        def standardized(value):
            # Divide by the maximum of value.std() and 1e-4
            # to guard against the case where all values are equal
            return (value - value.mean()) / max(1e-4, value.std())

        samples.data["advantages_manager"] = standardized(samples["advantages_manager"])
        samples.data["advantages_worker"] = standardized(samples["advantages_worker"])

        rollouts_end = time.time()
        print("Computing policy (iterations=" + str(config["num_sgd_iter"]) +
              ", stepsize=" + str(config["sgd_stepsize"]) + "):")
        names = [
            "iter", "total loss", "manager policy loss", "manager vf loss",
            "worker policy loss", "worker vf loss", "kl", "entropy"]
        print(("{:>15}" * len(names)).format(*names))
        samples.shuffle()
        shuffle_end = time.time()
        tuples_per_device = model.load_data(
            samples, self.iteration == 0 and config["full_trace_data_load"])
        load_end = time.time()
        rollouts_time = rollouts_end - iter_start
        shuffle_time = shuffle_end - rollouts_end
        load_time = load_end - shuffle_end
        sgd_time = 0
        for i in range(config["num_sgd_iter"]):
            sgd_start = time.time()
            batch_index = 0
            num_batches = (
                int(tuples_per_device) // int(model.per_device_batch_size))
            loss, manager_policy_graph, manager_vf_loss, worker_policy_graph, worker_vf_loss, kl, \
            entropy = [], [], [], [], [], [], []
            permutation = np.random.permutation(num_batches)
            # Prepare to drop into the debugger
            if self.iteration == config["tf_debug_iteration"]:
                model.sess = tf_debug.LocalCLIDebugWrapperSession(model.sess)
            while batch_index < num_batches:
                full_trace = (
                    i == 0 and self.iteration == 0 and
                    batch_index == config["full_trace_nth_sgd_batch"])
                batch_loss, batch_manager_policy_graph, batch_manager_vf_loss, \
                batch_worker_policy_graph, batch_worker_vf_loss, \
                batch_kl, batch_entropy = model.run_sgd_minibatch(
                        permutation[batch_index] * model.per_device_batch_size,
                        self.kl_coeff, self.global_step, full_trace,
                        self.file_writer)
                loss.append(batch_loss)
                manager_policy_graph.append(batch_manager_policy_graph)
                manager_vf_loss.append(batch_manager_vf_loss)
                worker_policy_graph.append(batch_worker_policy_graph)
                worker_vf_loss.append(batch_worker_vf_loss)
                kl.append(batch_kl)
                entropy.append(batch_entropy)
                batch_index += 1
            loss = np.mean(loss)
            manager_policy_graph = np.mean(manager_policy_graph)
            manager_vf_loss = np.mean(manager_vf_loss)
            worker_policy_graph = np.mean(worker_policy_graph)
            worker_vf_loss = np.mean(worker_vf_loss)
            kl = np.mean(kl)
            entropy = np.mean(entropy)
            sgd_end = time.time()
            print(
                "{:>15}{:15.5e}{:15.5e}{:15.5e}{:15.5e}{:15.5e}{:15.5e}{:15.5e}".format(
                    i, loss, manager_policy_graph, manager_vf_loss, worker_policy_graph, worker_vf_loss, kl, entropy))

            values = []
            if i == config["num_sgd_iter"] - 1:
                metric_prefix = "ppo/sgd/final_iter/"
                values.append(tf.Summary.Value(
                    tag=metric_prefix + "kl_coeff",
                    simple_value=self.kl_coeff))
                values.extend([
                    tf.Summary.Value(
                        tag=metric_prefix + "mean_entropy",
                        simple_value=entropy),
                    tf.Summary.Value(
                        tag=metric_prefix + "mean_loss",
                        simple_value=loss),
                    tf.Summary.Value(
                        tag=metric_prefix + "manager_policy_graph",
                        simple_value=manager_policy_graph),
                    tf.Summary.Value(
                        tag=metric_prefix + "manager_vf_loss",
                        simple_value=manager_vf_loss),
                    tf.Summary.Value(
                        tag=metric_prefix + "worker_policy_graph",
                        simple_value=worker_policy_graph),
                    tf.Summary.Value(
                        tag=metric_prefix + "worker_vf_loss",
                        simple_value=worker_vf_loss),
                    tf.Summary.Value(
                        tag=metric_prefix + "mean_kl",
                        simple_value=kl)])
                if self.file_writer:
                    sgd_stats = tf.Summary(value=values)
                    self.file_writer.add_summary(sgd_stats, self.global_step)
                    weights = model.get_weights()
                    for key, variable in weights.items():
                        log_histogram(self.file_writer, key, variable, self.global_step)

            self.global_step += 1
            sgd_time += sgd_end - sgd_start
        if kl > 2.0 * config["kl_target"]:
            self.kl_coeff *= 1.5
        elif kl < 0.5 * config["kl_target"]:
            self.kl_coeff *= 0.5

        info = {
            "kl_divergence": kl,
            "kl_coefficient": self.kl_coeff,
            "rollouts_time": rollouts_time,
            "shuffle_time": shuffle_time,
            "load_time": load_time,
            "sgd_time": sgd_time,
            "sample_throughput": len(samples["obs"]) / sgd_time
        }

        FilterManager.synchronize(
            self.local_evaluator.filters, self.remote_evaluators)
        res = self._fetch_metrics_from_remote_evaluators()
        res = res._replace(info=info)
        return res

    def _fetch_metrics_from_remote_evaluators(self):
        episode_rewards = []
        episode_lengths = []
        metric_lists = [a.get_completed_rollout_metrics.remote()
                        for a in self.remote_evaluators]
        for metrics in metric_lists:
            for episode in ray.get(metrics):
                episode_lengths.append(episode.episode_length)
                episode_rewards.append(episode.episode_reward)
        avg_reward = (
            np.mean(episode_rewards) if episode_rewards else float('nan'))
        avg_length = (
            np.mean(episode_lengths) if episode_lengths else float('nan'))
        timesteps = np.sum(episode_lengths) if episode_lengths else 0

        result = TrainingResult(
            episode_reward_mean=avg_reward,
            episode_len_mean=avg_length,
            timesteps_this_iter=timesteps)

        return result

    def _stop(self):
        # workaround for https://github.com/ray-project/ray/issues/1516
        for ev in self.remote_evaluators:
            ev.__ray_terminate__.remote()

    def _save(self, checkpoint_dir):
        checkpoint_path = self.saver.save(
            self.local_evaluator.sess,
            os.path.join(checkpoint_dir, "checkpoint"),
            global_step=self.iteration)
        agent_state = ray.get(
            [a.save.remote() for a in self.remote_evaluators])
        extra_data = [
            self.local_evaluator.save(),
            self.global_step,
            self.kl_coeff,
            agent_state]
        pickle.dump(extra_data, open(checkpoint_path + ".extra_data", "wb"))
        return checkpoint_path

    def _restore(self, checkpoint_path):
        self.saver.restore(self.local_evaluator.sess, checkpoint_path)
        extra_data = pickle.load(open(checkpoint_path + ".extra_data", "rb"))
        self.local_evaluator.restore(extra_data[0])
        self.global_step = extra_data[1]
        self.kl_coeff = extra_data[2]
        ray.get([
            a.restore.remote(o)
                for (a, o) in zip(self.remote_evaluators, extra_data[3])])

    def compute_action(self, observation):
        observation = self.local_evaluator.obs_filter(
            observation, update=False)
        return self.local_evaluator.common_policy.compute_single_action(
            observation, [], False)[0]
