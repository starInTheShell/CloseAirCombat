import time
import numpy as np
import torch

from .base_runner import Runner, ReplayBuffer

def _t2n(x):
    return x.detach().cpu().numpy()

class JSBSimRunner(Runner):
    def __init__(self, config):
        super(JSBSimRunner, self).__init__(config)
        self.episode_length = self.all_args.episode_length

    def load(self):
        assert len(self.envs.observation_space) == self.num_agents
        obs_space = self.envs.observation_space[0]
        act_space = self.envs.action_space[0]

        # algorithm
        if self.algorithm_name == "ppo":
            from algorithms.ppo.ppo_trainer import PPOTrainer as Trainer
            from algorithms.ppo.ppo_policy import PPOPolicy as Policy
        else:
            raise NotImplementedError
        self.policy = Policy(self.all_args, obs_space, act_space, device=self.device)
        self.trainer = Trainer(self.all_args, self.policy, device=self.device)

        # buffer
        self.buffer = ReplayBuffer(self.all_args, self.num_agents, obs_space, act_space)

        if self.model_dir is not None:
            self.restore()

    def run(self):
        self.warmup()

        start = time.time()
        episodes = int(self.num_env_steps) // self.buffer_size // self.n_rollout_threads

        for episode in range(episodes):

            for step in range(self.buffer_size):
                # Sample actions
                values, actions, action_log_probs, rnn_states_actor, rnn_states_critic = self.collect(step)

                # Obser reward and next obs
                obs, rewards, dones, infos = self.envs.step(actions)

                data = obs, actions, rewards, dones, action_log_probs, values, rnn_states_actor, rnn_states_critic

                # insert data into buffer
                self.insert(data)

            # compute return and update network
            self.compute()
            train_infos = self.train()
            
            # post process
            total_num_steps = (episode + 1) * self.buffer_size * self.n_rollout_threads

            # save model
            if (episode % self.save_interval == 0 or episode == episodes - 1):
                self.save()

            # log information
            if episode % self.log_interval == 0:
                end = time.time()
                print("\n Scenario {} Algo {} Exp {} updates {}/{} episodes, total num timesteps {}/{}, FPS {}.\n"
                        .format(self.all_args.scenario_name,
                                self.algorithm_name,
                                self.experiment_name,
                                episode,
                                episodes,
                                total_num_steps,
                                self.num_env_steps,
                                int(total_num_steps / (end - start))))

                train_infos["average_episode_rewards"] = np.mean(self.buffer.rewards) * self.episode_length
                print("average episode rewards is {}".format(train_infos["average_episode_rewards"]))
                self.log_info(train_infos, total_num_steps)

            # eval
            if episode % self.eval_interval == 0 and self.use_eval:
                self.eval(total_num_steps)

    def warmup(self):
        # reset env
        obs = self.envs.reset()
        self.buffer.obs[0] = obs.copy()

    @torch.no_grad()
    def collect(self, step):
        self.trainer.prep_rollout()
        values, actions, action_log_probs, rnn_states_actor, rnn_states_critic \
            = self.trainer.policy.get_actions(np.concatenate(self.buffer.obs[step]),
                                              np.concatenate(self.buffer.rnn_states_actor[step]),
                                              np.concatenate(self.buffer.rnn_states_critic[step]))
        # [self.envs, agents, dim]
        values = np.array(np.split(_t2n(values), self.n_rollout_threads))
        actions = np.array(np.split(_t2n(actions), self.n_rollout_threads))
        action_log_probs = np.array(np.split(_t2n(action_log_probs), self.n_rollout_threads))
        rnn_states_actor = np.array(np.split(_t2n(rnn_states_actor), self.n_rollout_threads))
        rnn_states_critic = np.array(np.split(_t2n(rnn_states_critic), self.n_rollout_threads))

        return values, actions, action_log_probs, rnn_states_actor, rnn_states_critic

    def insert(self, data):
        obs, actions, rewards, dones, action_log_probs, values, rnn_states_actor, rnn_states_critic = data

        rnn_states_actor[dones == True] = np.zeros(((dones == True).sum(), *self.buffer.rnn_states_actor.shape[3:]), dtype=np.float32)
        rnn_states_critic[dones == True] = np.zeros(((dones == True).sum(), *self.buffer.rnn_states_critic.shape[3:]), dtype=np.float32)

        self.buffer.insert(obs, actions, rewards, dones, action_log_probs, values, rnn_states_actor, rnn_states_critic)

    @torch.no_grad()
    def eval(self, total_num_steps):
        total_episodes, eval_episode_rewards = 0, []
        eval_cumulative_rewards = np.zeros(self.n_rollout_threads, *self.buffer.rewards.shape[2:], dtype=np.float32)

        eval_obs = self.envs.reset()
        eval_rnn_states = np.zeros((self.n_rollout_threads, *self.buffer.rnn_states_actor.shape[2:]), dtype=np.float32)

        while total_episodes < self.eval_episodes:
            self.trainer.prep_rollout()
            eval_actions, eval_rnn_states = self.trainer.policy.act(np.concatenate(eval_obs),
                                                                   np.concatenate(eval_rnn_states),
                                                                   deterministic=True)
            eval_actions = np.array(np.split(_t2n(eval_actions), self.n_rollout_threads))
            eval_rnn_states = np.array(np.split(_t2n(eval_rnn_states), self.n_rollout_threads))

            # Obser reward and next obs
            eval_obs, eval_rewards, eval_dones, eval_infos = self.eval_envs.step(eval_actions)
            eval_cumulative_rewards += eval_rewards

            eval_dones = np.all(eval_dones, axis=-1)
            total_episodes += np.sum(eval_dones)
            eval_episode_rewards += eval_cumulative_rewards[eval_dones == True]
            eval_cumulative_rewards[eval_dones == True] = 0
            eval_rnn_states[eval_dones == True] = np.zeros(((eval_dones == True).sum(), *self.buffer.rnn_states_actor.shape[2:]), dtype=np.float32)

        eval_infos = {}
        eval_infos['eval_average_episode_rewards'] = np.array(eval_episode_rewards).mean()
        print("eval average episode rewards of agent: " + str(np.mean(eval_infos['eval_average_episode_rewards'])))
        self.log_info(eval_infos, total_num_steps)

    @torch.no_grad()
    def render(self):
        pass